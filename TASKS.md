# TASKS

The planned work for MAK, in priority order.

---

## Wave index (priority order)

| Wave | Title | Review items | Depends on | Branch |
|:-:|---|---|---|---|
| [**7**](#wave-7--retrieval-based-graph-aware-planner) | Retrieval-based, graph-aware planner | S2, B2 | 27 recommended | `feat/7-planner-retrieval` |
| [**28**](#wave-28--write-sets-that-can-grow-safely) | Write sets that can grow safely | S3, B3, Q4 (headers) | 27 | `feat/28-growable-write-sets` |
| [**29**](#wave-29--agents-that-can-look-and-test) | Agents that can look and test | S4, B4, Q5 | 27, 28 | `feat/29-agent-tools` |
| [**30**](#wave-30--respect-the-users-repository) | Respect the user's repository | S5, S9, S10, S16 (clean tree), B5, B10, B11 | 27 recommended | `feat/30-repository-respect` |
| [**R**](#wave-r--first-public-release) | First public release | S16 (spend cap), Q6 | 30 | `feat/R-release-prep` |
| [**31**](#wave-31--sqlite-state-store) | SQLite state store | S6, B6 | 27 | `feat/31-sqlite-state` |
| [**32**](#wave-32--scheduler-fairness-and-plan-review-previews) | Scheduler fairness and plan-review previews | S8, S16 (previews), B9 | 27 | `feat/32-scheduler-fairness` |
| [**33**](#wave-33--evaluate-what-can-actually-fail) | Evaluate what can actually fail | S11, Q2 | 7, 28 (for meaningful numbers) | `feat/33-honest-evaluation` |
| [**8**](#wave-8--language-boundary-and-structured-non-python-resources) | Language boundary and structured non-Python resources | S12, Q3, Q12 | 27, 30 | `feat/8-language-boundary` |
| [**34**](#wave-34--the-kernel-as-a-coordination-service-library--mcp) | The kernel as a coordination service (library + MCP) | S15, Q1 | 27, 28 | `feat/34-kernel-service` |


---

## Wave 7 — Retrieval-based, graph-aware planner

### Status and branch

- **Planned.** Implement on **`feat/7-planner-retrieval`** (no branch exists).
- **Merges the original Wave 7 (planner token efficiency, 7.1–7.4) with review
  items S2 and B2.** The original wave treated the problem as cost; the review
  adds that it is also quality — the planner guesses from names what the kernel
  already knows from its dependency graph.
- Builds on the one planning entry point, `Session.propose_plan` (shipped in
  0.9.3b). Easier after Wave 27 but not blocked by it.

### Goal

The planner's input grows **sub-linearly** with repository size, never exceeds
a configured budget without saying so, reuses a cached prefix across retries,
and is given the real call graph instead of being asked to guess callers.

### Evidence and root cause

- **Everything, every time.** `Session.plan` passes
  `self._node_store.list_nodes()` — the entire inventory — to
  `Planner.decompose` (`session.py:1020-1022`); the app does the same
  (`cli/runner.py:210`). `Planner._build_prompt` (`planner.py:600-617`) renders
  every id as a bullet line. For MAK's own tree: **146 files → 1,829 nodes →
  ~104 K characters ≈ 26 K tokens**, ids only. A 1 M-line repository would be
  several hundred thousand tokens.
- **Retries resend all of it.** `_complete_with_retries` (`planner.py:662-705`)
  re-sends the full prompt plus a note on every attempt; the optional critique
  pass sends the plan again.
- **Outline mode is still O(repo).** `_build_outline_prompt`
  (`planner.py:730-745`) lists every file with every symbol name, then runs one
  detail call per step.
- **The model guesses callers.** The "CASCADE PREVENTION" instruction
  (`planner.py:75-83`) tells the model to *"search the inventory for any node
  whose name suggests it calls a symbol you are changing"*. The kernel builds
  the real reference graph (`dep_graph_from_store`) only afterwards, in
  `install_plan` (`session.py:1123`), and uses it only to repair edges.
- **Names without shapes.** The inventory has no signatures, so the model
  cannot tell whether a change is body-only or which callers a signature change
  would break — while `mak/node_store/api_digest.py::public_api_digest` already
  renders exactly that.
- **Measurement is partial.** `Planner.token_usage` accumulates totals
  (`planner.py:592-598`), but nothing logs per-call prompt size, inventory size,
  or cached tokens, so "did this get cheaper" is not answerable from a log.

### Design decisions

#### D7.1 — Measure first

A `PLANNER_CALL` event per call: `phase` (`plan`, `outline`, `detail`,
`critique`, `expand`), `attempt`, `prompt_chars`, `inventory_files`,
`inventory_nodes`, `inventory_chars`, `input_tokens`, `output_tokens`,
`cached_input_tokens`. Summed into `SessionResult.metrics` as
`planner_calls`, `planner_input_tokens`, `planner_cached_tokens`.

#### D7.2 — A hierarchical inventory view

`mak/planner/inventory.py::InventoryView`, built from the store and `DepGraph`
and cached per store `generation`:

- **Level 0 — tree:** directories and files with node counts.
- **Level 1 — file summary:** per symbol, one line: kind, qualified name,
  signature (from `api_digest`), and `called by: <n> (top 3 files)` from the
  graph.
- **Level 2 — node ids** for an expanded file, exactly as today.

#### D7.3 — The planner may ask to expand

A planner reply may be `{"expand": ["pkg/a.py", "pkg/b/"]}` instead of a plan.
The kernel replies with level-1 detail for those paths and asks again. Bounded
by `planner.max_expansions` (default 3) and `planner.inventory_token_budget`.
Plan validation treats a target in an **unexpanded existing file** exactly like
a hallucinated id (grounding may correct it; otherwise `unknown_node`).

#### D7.4 — Strategy selection

`planner.strategy` gains `retrieval` and a new default, `auto`: `oneshot` with
the flat listing when the estimated inventory is ≤ `planner.inventory_token_budget`
(default 12,000 estimated tokens, `len/4`), otherwise `retrieval`. `oneshot` and
`outline` keep working when named explicitly.

#### D7.5 — Seed the retrieval deterministically

Before the first call, pre-expand the files most likely to matter: identifier
and path matches between the task text and symbol names (camel/snake-split,
case-folded), plus their 1-hop `DepGraph` neighbourhood, up to half the budget.
No embeddings in this wave (no new dependency); a pluggable `Retriever` protocol
leaves room for them.

#### D7.6 — The kernel supplies callers

- Level-1 summaries already show caller counts (D7.2).
- After the plan parses, for each existing function target with
  `changes_api` true or undeclared, compute its callers from the graph. A caller
  not covered by any task becomes a `missing_caller` finding and — when
  `planner.auto_caller_tasks` (default `true`) — a **proposed** task
  ("update calls to X in Y", target = the caller node, `changes_api: false`,
  `depends_on` the changing task), shown in review as proposed, removable like
  any task.
- Replace the "guess from names" instruction with: "MAK will add caller updates
  for signature changes it can see; list callers it cannot see (dynamic calls,
  new code) yourself."

#### D7.7 — Cacheable prompts

Extend `PlannerLLM` with an optional `complete_parts(prefix, suffix)`. The
stable part (instructions, level-0 tree, agent list) is the prefix; the task,
expansions and retry notes are the suffix. Anthropic: `cache_control` on the
prefix block. OpenAI: rely on automatic prefix caching by keeping the prefix
byte-stable. Gemini and Ollama: fall back to `complete(prefix + suffix)`. Retries
change only the suffix. `cached_input_tokens` is read from each provider's usage.

#### D7.8 — Truncation is measured, never silent

If even level 0 exceeds the budget, deeper directories collapse to counts and
the prompt says so (`N directories collapsed; expand to see them`). The count
is logged (`inventory_collapsed`).

### Implementation plan

- **7.1 Instrumentation (D7.1)** in `mak/planner/planner.py`, `llm.py`,
  `mak/core/logging.py`; metrics fields; a synthetic inventory generator for
  tests (10, 100, 1,000 files).
- **7.2 `InventoryView` (D7.2)** with golden renderings for a fixture store.
- **7.3 Expansion protocol (D7.3)** in `parse_plan`'s caller; `max_expansions`
  and budget config; validation rule for unexpanded targets.
- **7.4 `auto`/`retrieval` strategies (D7.4) and seeding (D7.5).**
- **7.5 Caller completion (D7.6)** in `mak/planner/validation.py` (new finding
  kind `missing_caller`), review rendering of proposed tasks in both front ends,
  prompt text change.
- **7.6 Cacheable prompts (D7.7)** for Anthropic and OpenAI; usage parsing.
- **7.7 Config** — `planner.strategy: auto|oneshot|outline|retrieval`,
  `inventory_token_budget`, `max_expansions`, `auto_caller_tasks`; validation;
  `mak/config.yaml` and examples.
- **7.8 Benchmark hook** — `benchmark/run_benchmark.py` and the sweep record
  planner input tokens per run; a table in `benchmark/README.md` before/after
  on Template 4 (recorded manually with a real planner; not in CI).
- **7.9 Gates and docs** — CONTRIBUTING §9, §12; README if user-visible
  strategy changes; CHANGELOG.

### Required test matrix

| Case | Expected |
|---|---|
| inventory ≤ budget, `auto` | oneshot, flat listing, one call |
| inventory > budget, `auto` | retrieval; first prompt ≤ budget for 100 and 1,000-file synthetic repos |
| planner asks to expand twice, then plans | three calls; expansions logged; plan validated |
| expansions exceed `max_expansions` | final call is told to plan now; no further expansion |
| target in an unexpanded existing file | corrected or `unknown_node`, never silently accepted |
| signature change with 3 graph callers, plan covers 1 | 2 `missing_caller` findings, 2 proposed tasks |
| `auto_caller_tasks: false` | findings only |
| retry after a malformed plan | prefix byte-identical to first attempt |
| Anthropic backend | `cache_control` on the prefix block |
| every call | one `PLANNER_CALL` event with sizes and usage |

### Acceptance criteria

- First-call planner input for synthetic repos of 10 / 100 / 1,000 files stays
  under `inventory_token_budget` (flat growth above the threshold).
- Retries reuse a byte-stable prefix; Anthropic reports cached tokens.
- A signature change's graph-visible callers are always covered by a task or a
  finding.
- The benchmark records planner tokens before and after.

### Deliberately out of scope

- Embedding-based retrieval (a later `Retriever`).
- A template bypass for fixed task shapes (original 7.4, optional) — revisit
  after Wave 33 shows which shapes recur.
- Changing agent bundle budgets (CONTRIBUTING §3.3); coordinate, don't merge.

---

## Wave 28 — Write sets that can grow safely

### Status and branch

- **Planned.** Implement on **`feat/28-growable-write-sets`** after Wave 27
  (each mechanism below is a `CommitCheck` plug-in or verdict handler).
- **Review items S3, B3, and the header part of Q4.**

### Goal

Let an agent do the three things real edits routinely need but a fixed write
set forbids — add an import, add a helper next to its target, and touch a node
the planner did not predict — without serializing on the module header and
without giving up conservative two-phase locking's deadlock freedom.

### Evidence and root cause

- **Out-of-grant output is dropped.** `protocol.map_returned_sources`
  (`mak/agent_runner/protocol.py:108-159`) accepts granted ids, folds symbols
  into a whole-file grant, and drops everything else with "returned node id is
  outside the task's granted nodes".
- **Imports serialize on one node.** Ingestion puts all imports and leading
  constants in one `module_header` node per file
  (`<file>::module_header::__header__`). Every task that needs an import must
  WRITE-lock it; the planner prompt (`planner.py:32-83`) never mentions headers
  or imports, so a forgotten header means a dropped import, then a failed or
  retried task. The practical result is planners over-claiming (whole files,
  extra headers), which destroys the parallelism MAK exists for.
- **New nodes land at the end of the file.** A node the store has never seen
  gets `order = len(self._metadata)` (`mak/node_store/store.py:619`) — larger
  than every per-file order, so it is emitted last in its file. Fine for a
  helper function, wrong for a module-level constant used at import time.
- **The pieces already exist.** `import_check.check_import_conflicts` detects
  same-name/different-target imports; `name_collision_check` detects duplicate
  new symbols; `mak/semantic/registry_merge.py::plan_merge` is a working model
  of a commutative textual merge with key-level locks.

### Design decisions

#### D28.1 — An `imports` channel

`TaskResult.imports: list[str]` — each entry one import statement.
`decode_task_result` validates that each parses to exactly one module-level
`ast.Import`/`ast.ImportFrom`. All four schema dialects in
`result_schema.py` gain the field; a new prompt contract `IMPORTS_CONTRACT`
("never edit the module header to add an import — list it in `imports`") joins
the other three in every adapter prompt and the CLI bridge prompt.

#### D28.2 — Imports merge commutatively

- Resources: `<file>::module_header::__header__#imports` (INTENT_WRITE) and
  `…#import=<bound name>` (WRITE) per bound name, derived in
  `mak/lock_manager/resources.py`.
- Acquired **at commit time** with a non-blocking `try_acquire_all` for the
  task. Busy → the commit is deferred (existing parking) and retried on the next
  batch completion. A task holding plain WRITE on the header excludes all
  importers, as it should.
- Merge (`mak/semantic/import_merge.py`): parse the current header; drop
  entries already imported identically; a bound name mapped to a different
  target is a conflict (reuse `import_check`); insert the rest after the last
  import statement, `from __future__` first, preserving comments and the
  header's other content. A file with no header gets one created before its
  first node (D28.5).
- The staged header is re-validated by the normal pipeline (it is just another
  staged node).

#### D28.3 — Additive new symbols

A returned id that is **not** in the grant is accepted as additive when all
hold: its file contains one of the task's fragment targets; it names a
`function` or `class`; it does **not** exist in the store (live or retired);
`semantic.intention_locks` is on (the task already holds INTENT_WRITE on the
file); the task's additive count ≤ `session.max_additive_symbols` (default 5).
The kernel takes WRITE on the new id at commit (it can only be busy if another
task creates the same id — a name collision). Placement: immediately after the
task's last target fragment in that file, or after an explicit
`"insert_after": "<id>"` the agent supplies. Logged as `ADDITIVE_SYMBOL`.
Anything else out of grant goes to D28.4.

#### D28.4 — Lock escalation without hold-and-wait

An out-of-grant **existing** node in the reply becomes an escalation request
instead of a silent drop (the returned source is the proposal); an agent may
also declare `needs_nodes: [{node_id, mode, reason}]` without source.

1. Compute the policy-derived resources for the extra nodes (`lock_requests`
   with `changes_api=None`, i.e. conservative `#api` WRITE).
2. Try to acquire them **immediately** for the task.
3. **Granted:** extend `WaveState.granted`. If every escalated node is in the
   task's read set at its current digest, stage its source and continue the
   pipeline. Otherwise resend with the widened bundle (the agent never saw the
   current source).
4. **Not granted:** release **all** of the task's locks, add the nodes to the
   task's `target_nodes`, and re-queue it. Its next dispatch acquires the whole
   set atomically, as conservative 2PL requires. The previous reply goes into
   the retry note.

Never wait while holding locks for an escalation — that is the hold-and-wait
condition atomic pre-allocation exists to rule out. Whole-file and class-shell
escalations are refused (they conflict with everything below them); the task is
re-queued with that target instead. Bounded by `session.max_escalations`
(default 2) per task; escalated targets are persisted in scheduler annotations
so `--recover` keeps them.

#### D28.5 — Store support for placed insertion

`NodeStore.insert_node(node_id, kind, source, *, after: NodeId | None)`
renumbers the file's `order` values inside the open transaction (orders stay
per-file integers). `put_node` for an unknown id keeps today's append behaviour.

#### D28.6 — Planner prompt and accounting

The prompt explains that imports and new helpers are handled by the kernel and
that the header should be targeted only for non-import changes. Metrics:
`import_merges`, `additive_symbols`, `escalations_requested`,
`escalations_granted`, `escalation_restarts`.

### Implementation plan

- **28.1** Freeze today's behaviour as tests: three tasks needing one new import
  each in the same file serialize (or drop the import); a helper returned beside
  a target is dropped.
- **28.2** Protocol and schemas (D28.1) — `TaskResult`, decoder, four dialects,
  CLI bridge prompt, adapter prompts.
- **28.3** Resources and import merge (D28.2) as a `CommitCheck` placed after
  `RegistrarMerge`.
- **28.4** `insert_node` (D28.5) with transaction and recovery tests.
- **28.5** Additive symbols (D28.3) in `map_returned_sources` (a new `additive`
  bucket returned alongside `accepted`/`dropped`) and a check.
- **28.6** Escalation (D28.4): a verdict kind `escalate` handled by a new
  handler; scheduler `requeue_with_targets`; annotations for recovery.
- **28.7** Planner prompt and metrics (D28.6).
- **28.8** Semantic corpus: add scenarios "two tasks add the same import",
  "two tasks add conflicting imports for one name", "two tasks add the same
  helper name"; the false-positive corpus must stay at zero.
- **28.9** Benchmark: a synthetic workload where every task needs an import in
  a shared file; record realized concurrency before/after.
- **28.10** Gates and docs (CONTRIBUTING §3.4, §4.5, §5, §7.6; CHANGELOG).

### Required test matrix

| Case | Expected |
|---|---|
| 3 tasks, same file, 3 different new imports | all commit concurrently; header has all three once |
| 2 tasks import the same name identically | one entry |
| 2 tasks bind one name to different targets | second rejected as an import conflict |
| a task holds WRITE on the header | importers wait (parked), then merge |
| file with no header gets an import | header created before the first node |
| `from __future__` import via the channel | placed first |
| additive helper next to target | committed right after the target fragment |
| additive id already exists / is retired | treated as escalation, not additive |
| 6th additive symbol with cap 5 | refused with a reason |
| escalation, locks free, node in read set | committed in the same attempt |
| escalation, locks free, node not in read set | resent with widened bundle |
| escalation, locks busy | all locks released; re-queued with widened targets; no deadlock under the concurrency stress test |
| whole-file escalation | refused; re-queued with that target |
| `--recover` after an escalation restart | widened targets restored |

### Acceptance criteria

- On the import-heavy synthetic workload, mean realized concurrency improves
  and no import is lost.
- No agent output is dropped silently: every out-of-grant id is accepted,
  additive, escalated, or refused with a logged reason.
- The deadlock-freedom invariant holds: no task ever waits for a lock while
  holding one (asserted by the watchdog under the stress test).
- Semantic corpus results unchanged; zero false positives.

### Deliberately out of scope

- Removing unused imports.
- Additive module-level constants and assignments (placement semantics differ;
  revisit after this wave).
- Splitting `module_body` or nested functions into finer nodes (rest of Q4).

---

## Wave 29 — Agents that can look and test

### Status and branch

- **Planned.** Implement on **`feat/29-agent-tools`** after Waves 27 and 28
  (tools read through `DispatchEnricher`'s read-set API; an agent that finds it
  needs another node uses Wave 28's escalation).
- **Review items S4, B4, Q5.**

### Goal

Keep the rule that the kernel owns every write, but let an agent **read** code
the enrichment heuristics did not include and **run tests** against the
repository as it would be if its change landed — and run agents in child
processes so a hung call can actually be stopped.

### Evidence and root cause

- **Single shot by interface.** `AgentAdapter` is `format_task → send →
  parse_result` (`adapters/base_adapter.py`). The Anthropic adapter pins
  `tool_choice` to `submit_task_result` (`anthropic_api_adapter.py:144-145`), so
  the model's only action is to answer.
- **Blind and untested.** Agents see only the bundle (CONTRIBUTING §3.3) and
  cannot import the module they edit or run a test. Retry notes replace
  feedback after the fact, at the cost of a whole attempt.
- **The machinery for "the repo if this landed" exists.** The prospective view
  used before repair commits (`_prospective_semantic_reasons`,
  `session.py:2722`) and `mak/semantic/overlay.py` (materializes the work dir
  with chosen files substituted, used by the `impact_tests` gate, which already
  runs pytest in a subprocess via `gate_types.ProcessRunner`).
- **Hung calls cannot be stopped (Q5).** Agent calls run on
  `ThreadPoolExecutor` threads (`Session._runner`); Python cannot kill a thread,
  so a wedged SDK call is bounded only by the SDK's own timeout (documented as an
  accepted limitation).

### Design decisions

#### D29.1 — Opt-in per agent

`agents[].tools: none | read | read_test`, default `none`. `none` is
byte-identical to today. `tool_turns` (default 8) and `tool_output_bytes`
(default 64 KB total) bound a dispatch.

#### D29.2 — Read tools over a pinned snapshot, and every read is a read

Served by the kernel from the committed store **at the dispatch's generation**:

| Tool | Returns |
|---|---|
| `get_node(node_id)` | current committed source |
| `list_file(path)` | node ids with signatures (level-1 view from Wave 7 when present) |
| `find_symbol(name)` | defining node ids (`DepGraph.definers`) |
| `callers_of(node_id)` | referencing node ids (`DepGraph.references`, inverted) |
| `search(pattern, max_results=20)` | node ids and matching lines over committed sources (regex with a time limit) |

**Every node returned by a tool is added to the task's read set** with its
version and digest. This is the invariant that keeps stale-read validation
sound: an agent must not be able to build on something the kernel does not know
it read. Paths are containment-checked; tools never read the filesystem outside
the store.

#### D29.3 — `run_tests` against a prospective overlay

`run_tests(selector, candidate_sources)` materializes committed state + the
agent's candidate sources into a temp overlay (`overlay.py`), runs
`session.test_command` restricted by `selector` (pytest `-k` expression or test
node ids under the project's test paths), with `agents[].test_timeout_s`
(default 120), inside the Docker sandbox when `--sandbox` is on, and returns
exit status plus the last 4 KB of output. The work dir is never touched. Results
are advisory; the commit pipeline still validates the final answer.

#### D29.4 — One tool-loop driver

`mak/agent_runner/tool_loop.py`, mirroring `repair_loop`: provider-neutral turn
loop; `submit_task_result` stays the terminal tool; per-provider encodings for
Anthropic tools (`tool_choice: auto`), OpenAI function tools, Gemini function
declarations. Ollama and OpenAI-compatible endpoints use tools only when the
endpoint/model reports tool support (catalog `supported_parameters` includes
`tools`); otherwise the agent is downgraded to `none` with a startup warning.
Usage is summed across turns (the spend ceiling sees every token). CLI agents
are out of scope (they have their own tools).

#### D29.5 — Process isolation for agent calls

`session.agent_isolation: thread | process`, default `thread` in this wave.
`process` runs each dispatch in `python -m mak.agent_runner.worker`, which
receives the bundle and the agent's resolved config over stdin; tool calls are
proxied back to the parent over the pipe (the snapshot lives in the parent).
Only the one API key that agent needs is placed in the child's environment. On
timeout the parent sends SIGTERM, then SIGKILL after 5 s — real preemption,
which retires the "cooperative abandonment" limitation for `process` mode.

#### D29.6 — Observability

`AGENT_TOOL_CALL` events (tool, argument summary, result bytes, duration);
`TaskResult.tool_calls`; metrics `tool_calls`, `test_runs`,
`test_run_seconds`; `TASK_DISPATCHED` records the tool mode.

### Implementation plan

- **29.1** Tool protocol types and the snapshot server (read tools, D29.2) with
  read-set recording; unit tests with a fake store.
- **29.2** `tool_loop.py` (D29.4) with a scripted fake provider; Anthropic and
  OpenAI encodings; Gemini; capability gating for compatible endpoints.
- **29.3** `run_tests` (D29.3) on `overlay.py`; sandbox wiring.
- **29.4** Config (`tools`, `tool_turns`, `tool_output_bytes`,
  `test_timeout_s`, `agent_isolation`) and validation.
- **29.5** Worker process (D29.5) with the pipe protocol and kill path.
- **29.6** Observability (D29.6).
- **29.7** Benchmark: Template 4 with `tools: none` vs `read_test` on one model
  (recorded, not CI); a keyless mock run in CI.
- **29.8** Gates and docs (CONTRIBUTING §7, §12, known limitations; README
  agent section; CHANGELOG).

### Required test matrix

| Case | Expected |
|---|---|
| `tools: none` | requests byte-identical to today |
| agent reads a node via `get_node`, node changes before commit | stale read detected exactly as for bundle context |
| tool reads beyond `tool_turns` | loop ends; agent told to submit |
| `search` with a catastrophic regex | time-limited, error returned to the agent |
| `run_tests` with a failing candidate | output returned; work dir unchanged |
| `run_tests` timeout | killed at timeout; reported |
| endpoint without tool support | downgraded to `none`, warning at startup |
| `agent_isolation: process`, adapter hangs | dispatch killed within timeout + 5 s; task failed retryably |
| child environment | contains only that agent's key |
| usage across 5 turns | summed into `TaskResult.usage` and the ceiling |

### Acceptance criteria

- With tools on, every node an agent saw is in its read set.
- `run_tests` never modifies the work dir.
- In `process` mode a hung call is stopped within its timeout plus grace.
- The benchmark reports Template 4 accuracy and tokens for `none` vs
  `read_test`.

### Deliberately out of scope

- Write tools. The kernel still owns every write.
- Changing the default `tools` / `agent_isolation` (a later decision, informed
  by Wave 33).
- Tool loops for CLI agents.

---

## Wave 30 — Respect the user's repository

### Status and branch

- **Planned.** Implement on **`feat/30-repository-respect`**.
- **Review items S5, S9, S10 and the clean-tree part of S16 (B5, B10, B11).**
  Bundled because all three are about MAK changing user state it should not:
  the bytes of files, the user's branch, and edits made during a run. They touch
  the same path — reconstruction, `install_files`, the git helper — and one set
  of end-to-end tests covers them. Absorbs Wave R's R.7.2.
- Easier after Wave 27 (`CommitApplier`), not blocked by it.

### Goal

1. Editing one function changes only that function's bytes on disk unless the
   project asks for formatting.
2. MAK never moves the user's branch; its audit trail lives in its own ref.
3. A file a person edits during a run is never overwritten silently.

### Evidence and root cause

- **Forced formatting (B5).** `render_file(fragments, use_ruff=True)`
  (`mak/node_store/reconstruction.py:72-91`) is called from the commit path
  with no option (`transaction.py:75`); nothing in config controls it. In a
  project not formatted with ruff, editing one function reformats the whole
  file.
- **Formatting is currently load-bearing.** Ingestion drops whitespace-only gaps
  between fragments (`ingestion.py:96-109`: "`ruff format` re-establishes
  blank-line spacing on reconstruction") and `assemble_fragments` joins with a
  single blank line (`reconstruction.py:25-35`). Simply turning ruff off would
  change blank-line layout everywhere; faithful bytes need the gaps recorded.
- **Store and disk diverge.** The store keeps unformatted fragments while disk
  holds the formatted file, so agents are shown source that differs from what
  the user sees. Each commit also costs a `ruff` subprocess per file.
- **Commits land on the user's branch (B10).** `GitHelper.commit_task`
  (`git.py:135-194`) commits onto whatever branch HEAD names, one commit per
  task; the README has to tell users to create a separate branch.
- **Edits during a run are lost (B11).** External-edit detection runs only in
  `_reconcile_work_dir` during `initialize()` (`session.py:820-872`).
  `install_files` (`transaction.py:79-133`) writes each destination from the
  store's view without comparing it to what was there; the journal backup is
  discarded on success. The project lease stops a second MAK, not an editor.
- **Files MAK has never written have no reference digest.** `_is_external_edit`
  returns `False` when `materialized_digest` is `None` (`session.py:874-883`), so
  even a startup-time comparison has nothing to compare against on a first run.

### Design decisions

#### D30.1 — Byte-faithful reconstruction

Ingestion records, for every fragment, the exact whitespace that followed it
(`sep_after`, in node metadata; the last fragment records the file's trailing
newline state). Reconstruction uses the recorded separator for every boundary
between two **unchanged** fragments, and a PEP 8 default (two blank lines at top
level, one inside a class) for any boundary next to a changed or new fragment.
New property test: with `formatter: none`, `ingest → reconstruct` is
**byte-identical** to the original for the round-trip corpus and MAK's own
source.

#### D30.2 — Formatting is a project decision

`reconstruction.formatter: none | ruff | black | "<command>"`, default **`none`**.

#### D30.3 — Format the fragment, never the file

When a formatter is configured, it runs on each **changed** fragment at staging
time (after the agent returns, before the pipeline), with the file's path as
context (`ruff format --stdin-filename <path> -`, `black -q --stdin-filename
<path> -`, or the custom command reading stdin, writing stdout), so the
project's formatter config applies. Unchanged fragments — including ones other
tasks hold locks on — are never rewritten. The store then equals the disk by
construction. A formatter failure keeps the raw fragment and logs a warning, as
today.

#### D30.4 — Detect edits at write time

`_reconcile_work_dir` records an `observed` digest for every file it ingests
(new field in `file_state`). `install_files` gains
`expected: Callable[[str], str | None]` returning, per destination, the digest
MAK believes is on disk (`materialized`, else `observed`, else "file must not
exist"). After rendering everything and before writing anything, it compares;
any mismatch raises `ExternalEditError(files)` — nothing is written and the
store transaction rolls back. The session then applies
`session.on_external_edit`:

- `adopt` (default): sync the edited file into the store (the edit becomes new
  node versions; the task's read set is now stale), and resend the task with a
  note naming the file.
- `conflict`: fail the task with a non-retryable reason naming the file, mark
  the file as externally modified for the rest of the session (later tasks
  targeting it fail fast), and report it in the run summary.

The window between the check and the atomic rename is documented as the
remaining race.

#### D30.5 — MAK-owned audit ref

`git.branch_mode: refs | session | current`, default **`refs`**:

- `refs`: audit commits are built from the private index with plumbing
  (`write-tree`, `commit-tree -p <previous>`, `update-ref
  refs/mak/<session-id> <new> <old>` as a compare-and-swap). The first parent is
  HEAD at session start. HEAD and the user's branch never move; MAK's changes
  are on disk as ordinary uncommitted modifications, with the full per-task
  audit in the ref.
- `session`: create and switch to `mak/<session-id>` at start (`git switch -c`,
  which keeps uncommitted changes), commit there.
- `current`: today's behaviour.

At the end of a satisfied run the app asks, and `mak run --land squash|none`
decides, whether to create **one** commit on the user's branch containing MAK's
files with a trailer naming the audit ref. `auto_push` pushes only a landed
commit. Recovery's re-audit (`_reaudit`) works on the ref idempotently
(compares trees).

#### D30.6 — Clean-tree policy (moved from R.7.2)

In `current` mode, `require_clean_tree` defaults to `true` when `auto_commit` is
on. In every mode, a non-git work dir or a dirty tree prints one clear warning
at start listing what MAK will and will not commit. `refs` mode is safe on a
dirty tree because audit commits contain only MAK's files on top of HEAD.

### Implementation plan

- **30.1** `sep_after` recording in ingestion, metadata, `sync_file`; assembly
  using it (D30.1); byte-identical property test.
- **30.2** `reconstruction.formatter` config and fragment-level formatting at
  staging (D30.2, D30.3); remove `use_ruff=True` from the commit path.
- **30.3** `observed` digests and `install_files(expected=…)` (D30.4);
  `ExternalEditError`; both policies in the session; a test hook that edits a
  file between dispatch and commit.
- **30.4** Git plumbing in `GitHelper` for `refs` and `session` modes (D30.5);
  landing; `--land`; push gate; recovery.
- **30.5** Clean-tree policy and warnings (D30.6).
- **30.6** Migration: existing stores have no `sep_after`; missing values fall
  back to the PEP 8 default, and the first sync of each file records them.
- **30.7** Gates; manual check on a Black-formatted repo and a hand-formatted
  one; docs (README safety section loses the separate-branch warning;
  CONTRIBUTING §3.5, §10, §11, §12; CHANGELOG with an upgrade note that the
  default formatter and branch mode changed).

### Required test matrix

| Case | Expected |
|---|---|
| `formatter: none`, ingest → reconstruct | byte-identical |
| `formatter: none`, edit one function | diff touches only that function (plus separators next to it) |
| `formatter: ruff` with project config | only changed fragments formatted; store == disk |
| formatter missing or failing | raw fragment written, warning logged |
| human edits a target file mid-run, `adopt` | nothing overwritten; store synced; task resent |
| same, `conflict` | task failed non-retryably; file protected for the session |
| human edits a file MAK never wrote before | detected via the `observed` digest |
| `branch_mode: refs` | HEAD and branch unchanged; `refs/mak/<id>` has one commit per task |
| `refs`, concurrent `update-ref` mismatch | CAS failure reported, no lost audit |
| `--land squash` | one commit on the user's branch with the trailer |
| `branch_mode: session` | new branch created and checked out; uncommitted changes preserved |
| crash during audit, `--recover` | ref re-audit idempotent |
| `current` + dirty tree + `auto_commit` | refused (default `require_clean_tree: true`) |

### Acceptance criteria

- With defaults, a MAK run on a non-ruff project changes only the bytes it
  edited.
- With defaults, `git rev-parse HEAD` and the checked-out branch are the same
  before and after a run.
- No human edit made during a run is overwritten without a logged decision.
- The README no longer needs the "create a separate branch" warning.

### Deliberately out of scope

- Formatting non-Python files (Wave 8).
- Interactive three-way merge of a mid-run human edit with an agent's change.

---

## Wave R — First public release

> **Status (2026-09-23).** Planned, partly overtaken by events. The version is
> now `0.9.2b0`, not `0.7.1b0`; `CHANGELOG.md` exists with entries back to
> 0.1.5b (R.9.1 and R.9.5 are moot); CONTRIBUTING was rewritten as a
> reference-only document of 142 KB (R.6.5, R.10.4, R.10.6 are done). R.7.2 moved
> to Wave 30 (D30.6). The review's default spend cap (S16) is merged into R.7.1.
> Everything else below is still open and was re-verified against `2d89f69`.
>
> **Input.** Derived from `release.md` (an external release review of commit
> `18e9ca2`) plus a read of the tree. The review's verdict — *ship a genuine
> pre-release, not a final release* — is adopted. Items marked ⚠️ **NOT IN
> release.md** are release-blocking gaps the review missed.
>
> **Already shipped:** the console entry point
> (`[project.scripts] mak = "cli.__main__:main"`) and config discovery
> (`./mak.yaml`, `~/.config/mak/config.yaml`, packaged default).

### R.0 Decisions to make before any code changes

Record each answer in `CHANGELOG.md` or CONTRIBUTING before starting.

- **R.0.1 Version number.** Currently `0.9.2b0` (`mak/_version.py:14`). Decide
  the first PyPI version (e.g. `0.10.0b1` or `1.0.0b1`). Keep the `bN` suffix:
  under PEP 440 a plain version is a final release regardless of being below
  1.0, and the suffix is what makes `pip install` skip it without `--pre`.
- **R.0.2 Distribution channel.** Resolve as "both, PyPI canonical" — anything
  else leaves `mak update` broken (R.4).
- **R.0.3 Supported platforms.** CI runs Ubuntu only. `project_lease.py:183-195`
  has a real `msvcrt` branch marked `# pragma: no cover - POSIX CI`. Either test
  it (R.6) or declare POSIX-only in metadata and README. **Do not ship untested
  Windows code as supported.**
- **R.0.4 Supported Python versions.** `pyproject.toml:10` claims `>=3.11`,
  unbounded. CI runs 3.11 and 3.13 (since 0.9.3b). Pick the matrix and make metadata, CI and
  README agree.
- **R.0.5 Base install and SDKs.** See R.3.1.
- **R.0.6 Default spend cap.** See R.7.1.
- **R.0.7 Does Wave 31 (SQLite state) land before the first PyPI release?** It
  changes the on-disk `.mak/` format. Landing it first means released users
  never migrate; landing it after means shipping a one-way migration to them.

### R.1 Version and identity

- **R.1.1 Bump the version** per R.0.1 in `mak/_version.py:14-15` and README
  line 6.
- **R.1.2 Audit every consumer of the version string.** `mak/__init__.py`
  re-exports both; `cli/__main__.py:286-289` prints `__version__`; `cli/ui.py`
  renders `__version_display__` lowercased in the banner. Confirm the banner
  reads well.
- **R.1.3 Version-consistency test** asserting
  `importlib.metadata.version("multi-agent-kernel") == mak.__version__`, that
  the display string describes the same release, and that README line 6
  matches.

### R.2 Package metadata (`pyproject.toml`)

`pyproject.toml:5-10` has **no authors, license, project URLs, classifiers or
keywords**. On PyPI that renders as an anonymous, unlicensed package.

- **R.2.1 `authors`** — Seungjoon Cha, matching `LICENSE`.
- **R.2.2 `license`** — `license = "MIT"` plus `license-files = ["LICENSE"]`,
  which requires `setuptools>=77`; bump `[build-system] requires`
  (`pyproject.toml:2`, currently `>=69`).
- **R.2.3 `[project.urls]`** — Homepage, Repository, Issues, Changelog,
  Documentation (CONTRIBUTING until real docs exist).
- **R.2.4 `classifiers`** — Development Status :: 4 - Beta; License :: OSI
  Approved :: MIT License; the Python versions from R.0.4; Operating System per
  R.0.3; Intended Audience :: Developers; Topic :: Software Development :: Code
  Generators; Environment :: Console.
- **R.2.5 `keywords`** — llm, agents, multi-agent, codegen, ast, orchestration.
- **R.2.6 sdist contents.** No `MANIFEST.in` exists. Add one pruning
  `benchmark/`, `research/contention_study/`, `graphics/`, `screenshots/`, `diagram/`,
  `demo/`. Keep `LICENSE` and `README.md`.
- **R.2.7 `package-data` survives the wheel** — `config.yaml`, `.env.example`,
  `models/seed.json`, `examples/*.yaml`. Config discovery falls back to the
  packaged config; R.12 smoke-tests it from the built wheel.
- **R.2.8 Claim the PyPI name** on PyPI and TestPyPI before anything else in
  R.2–R.5.

### R.3 The dependency contract

- **R.3.1 Move provider SDKs to extras — together with `mak update`.**
  `pyproject.toml:27-33` documents why the SDKs are in base dependencies:
  `mak update` reinstalls from git and resolves fresh, so moving them alone
  strips them from every existing install on its next update. Ship both halves
  in one release: SDKs in `[anthropic]`/`[openai]`/`[gemini]`/`[all]`, and
  `mak update` installing with the user's extras (or `[all]` for the
  transition, R.4.2). Document `uv tool install "multi-agent-kernel[all]"` as
  the default and `[local]` as the zero-SDK path.
- **R.3.2 `ruff`'s status.** A runtime dependency (reconstruction formats with
  it) with no upper bound (`>=0.5`). After Wave 30 (`formatter: none` default)
  decide whether ruff becomes an optional extra; until then add an upper bound,
  because a new ruff default changes the bytes MAK writes.
- **R.3.3 Upper bounds on SDK pins** (`anthropic>=0.107.1`, `openai>=2.41.0`,
  `google-genai>=2.8.0`).
- **R.3.4 Record a known-good resolution** (`uv.lock` or a constraints file).
- **R.3.5 Vulnerability scan** on the final resolved set.

### R.4 ⚠️ `mak update` vs. the distribution channel — NOT IN release.md

- **R.4.1 A PyPI install is silently replaced by a git install.**
  `_is_uv_tool_install()` (`cli/__main__.py:23-30`) is true for a PyPI
  `uv tool install` (it only checks for `/uv/tools/` in the interpreter path);
  `_update()` (`:157`) then installs `git+https://…@<tag>`, converting the user
  to a git install. `_installed_commit()` (`:33-47`) returns `None` for a PyPI
  install, so every update reinstalls. Detect the origin from `direct_url.json`
  and update along it (PyPI → `uv tool upgrade multi-agent-kernel --prerelease
  allow`; git → today's path).
- **R.4.2 Carry the extras through `update`.**
- **R.4.3 The first tag is a one-way door.** `_resolve_update_target()`
  (`:136`) falls back to `main` only while no version tags exist. Once a tag is
  pushed, every `mak update` pins users to tags. Push it only after the release
  commit is final; a deleted tag strands users. Put this in the runbook (R.13).
- **R.4.4 Pre-releases must be reachable, and users on `main` must not move
  backwards** when the newest tag is a pre-release. Define and test it.
- **R.4.5 Pre-release ordering.** `_version_key` (`cli/__main__.py:80-94`)
  compares suffixes lexically, so `b10 < b2`. Parse the numeric tail.
- **R.4.6 Test `mak update` against a real published tag** — every branch is
  currently covered only by mocks.

### R.5 Release automation

`.github/workflows/ci.yml` is the only workflow: Ubuntu, one Python, editable
install, ruff + mypy + pytest.

- **R.5.1 `release.yml`, tag-triggered**: build sdist + wheel →
  `twine check --strict` → install the **built wheel** in a clean venv → smoke
  tests (R.12) → publish.
- **R.5.2 PyPI Trusted Publishing (OIDC)** behind a GitHub Environment with
  required reviewers. No long-lived token.
- **R.5.3 TestPyPI dry run first**, installed on a clean machine.
- **R.5.4 Attach sdist + wheel to the GitHub Release.**
- **R.5.5 Mark the GitHub Release as a pre-release.**
- **R.5.6 Build provenance / attestations.**
- **R.5.7 Harden CI**: pin actions to SHAs, minimal `permissions:`,
  `concurrency:` to cancel superseded runs.

### R.6 Test matrix and platform claims

- **R.6.1 Expand `ci.yml`** to every Python and OS claimed (3.11 and 3.13 run
  since 0.9.3b).
- **R.6.2 Test the non-editable install path.**
- **R.6.3 Windows: test it or drop it.** The project lease — the "one MAK per
  project" safety property — depends on the `msvcrt` branch.
- **R.6.4 Sync `.pre-commit-config.yaml` with CI.** It runs `ruff check mak
  tests` (CI: `mak cli tests`) and `mypy --strict mak` (CI: `mak cli`).
- ~~R.6.5 Reconcile the test count~~ — done: CONTRIBUTING no longer prints one.

### R.7 ⚠️ Release-grade safety defaults — NOT IN release.md (as a decision)

- **R.7.1 Default spend cap (merged with review S16, Q6).**
  `mak/config.yaml:22` leaves `max_total_tokens` commented out — unbounded by
  default — and retries, cascades and fix-up waves multiply spend. Options in
  order of preference: (a) ship a finite default (e.g. 2,000,000) with a clear
  "raise it with `session.max_total_tokens`" message on breach; (b) keep
  unbounded but require acknowledgement on first run; (c) document only.
  **(a) or (b) is the defensible choice for a public beta.** The pre-dispatch
  cost estimate that makes the cap usable is Wave 32 (D32.4).
- ~~R.7.2 Working-tree safety~~ — moved to Wave 30 (D30.5, D30.6).
- **R.7.3 Disclose the scheduled network calls.** `mak/models/manifest.py` and
  `mak/config.yaml:83`: MAK contacts provider model-list APIs on the 1st and
  15th of each month. Say so plainly, with the opt-out (`models.auto_refresh:
  false`, `MAK_NO_MODEL_REFRESH`), in README and SECURITY.md.
- **R.7.4 State what leaves the machine** — source code goes to the configured
  providers — once, where a user reads it before the first run.

### R.8 Security and privacy documentation

- **R.8.1 `SECURITY.md`** — private vulnerability reporting, supported
  versions, response time, lower beta expectations.
- **R.8.2 Secret handling** — keys in `~/.config/mak/.env` (`0600`) or the
  environment; never in config files; how to remove them.
- **R.8.3 `.mak/` contents** — plaintext fragments of the user's source; add it
  to the project's `.gitignore`.
- **R.8.4 Run dependency and secret scanners** on the release commit.
- **R.8.5 Remove the legacy in-package `mak/.env`.** Still read with a
  deprecation warning by `cli/core/api_keys.py:40` and `mak/__main__.py:79`;
  the warning promises removal in the next release.

### R.9 CHANGELOG and release notes

- ~~R.9.1 Create `CHANGELOG.md`~~ — exists.
- **R.9.2 Release entry** summarizing everything since the last tagged state,
  grouped Added / Changed / Fixed / Known issues.
- **R.9.3 "Known limitations" block**: Python-only; Windows status per R.0.3;
  spend default per R.7.1; scheduled catalog calls; MAK slower than worktrees
  under single-hot-symbol contention (keep it — it is credibility).
- **R.9.4 Upgrade notes** — the extras change and what a git-installed user
  runs; Wave 30's default changes if it lands first.
- ~~R.9.5 Backfill nothing~~ — moot.

### R.10 Documentation reconciliation

- **R.10.1 Replace the fake badges.** `README.md:5-8` are static images; "CI
  Passing" stays green when CI is red. Use the workflow badge and a PyPI badge.
- **R.10.2 Install section** — the chosen channel(s), extras, pinning for git.
- **R.10.3 Update section** — the "falls back to `main`" sentence becomes false
  at R.13.
- ~~R.10.4, R.10.6~~ — done by the CONTRIBUTING rewrite.

### R.11 ⚠️ Community and triage infrastructure — NOT IN release.md

- **R.11.1 Issue templates** — the bug form requires `mak --version`, OS,
  Python, provider/model, install method, and whether the target repo is
  Python-only.
- **R.11.2 Feature request template + `config.yml`** pointing questions at
  Discussions.
- **R.11.3 PR template** referencing the quality gates.
- **R.11.4 `mak doctor`** — version, install channel, Python, OS, resolved
  config path, importable SDKs, which keys are *present* (never values), local
  runtime status.
- **R.11.5 Link `CODE_OF_CONDUCT.md`** from the README.
- **R.11.6 Repo settings** — Discussions, private vulnerability reporting,
  topics, About links matching R.2.3.

### R.12 Pre-flight validation (on the release commit)

Against the **built wheel in a clean environment**, not the checkout; record
the output for the release notes.

- **R.12.1** `git status` clean; on `feat/R-release-prep`.
- **R.12.2** Remove stale `build/` and `dist/` (both exist in the tree now).
- **R.12.3** `ruff check mak cli tests`, `mypy --strict mak cli`, `pytest -q` on
  every matrix cell.
- **R.12.4** `python -m build` → `twine check --strict dist/*`.
- **R.12.5** Fresh venv, install the wheel, outside any checkout:
  `mak --version`, `mak --help`, `mak examples local-ollama`, every packaged
  example config loads, the packaged `config.yaml` resolves.
- **R.12.6** A real run on a scratch repo with a cheap model, and the keyless
  `python3 benchmark/sweep.py --config benchmark/sweeps/smoke.yaml`.
- **R.12.7** The wheel contains no `.mak/`, `benchmark/`, `research/contention_study/`,
  test fixtures, or `graphify-out/`.
- **R.12.8** CI green on the exact release commit.
- **R.12.9** First-run setup on a machine with no `~/.config/mak/`.

### R.13 Publication runbook (strict order — R.4.3 makes the tag irreversible)

1. Merge the release branch; CI green on the merge commit.
2. Publish to TestPyPI from a throwaway tag; install on a clean machine; R.12.5.
3. Create and push the annotated release tag.
4. Watch `release.yml`; check the PyPI page's metadata.
5. Create the GitHub Release from the tag, marked pre-release, with notes and
   artifacts.
6. `uv tool install "multi-agent-kernel[all]" --prerelease allow` on a clean
   machine; `mak --version` matches.
7. From a machine on the previous git-installed version, `mak update` lands on
   the new release **with provider SDKs intact** (R.4.2).

### R.14 Rollback plan

- **R.14.1** A PyPI version cannot be re-uploaded, only yanked; the next fix is
  the next `bN`.
- **R.14.2** Yank criteria written in advance (data loss in a user repo, secret
  leakage, broken install on a supported platform) and who decides.
- **R.14.3** Deleting a tag strands `mak update` users; ship a higher tag
  instead.
- **R.14.4** Hotfix branches start from the tag, not `main`.

### R.15 Post-release verification

- **R.15.1** Within 24 h: PyPI page, badges, cold installs per OS, `mak update`
  from the previous version.
- **R.15.2** Watch for missing SDKs after the extras move, Windows lease
  failures, unexpected spend.
- **R.15.3** Tracked issues for everything deferred.
- **R.15.4** Exit criteria for leaving beta.

### Deliberately deferred

- Docker / GHCR image, Homebrew, signed tags / full SLSA beyond attestations,
  splitting CONTRIBUTING into `docs/`.

### Acceptance

- Version consistent across `mak --version`, `importlib.metadata`, banner and
  README, enforced by R.1.3.
- `twine check --strict` passes; the PyPI page shows author, license, URLs,
  classifiers, keywords.
- The CI-built wheel installs and passes R.12.5 on every supported cell.
- `[local]` installs zero provider SDKs and a local run works.
- `mak update` works along both channels, covered by tests.
- `SECURITY.md` exists; badges are live; no doc claims a Python, OS or install
  command CI does not verify.
- `.pre-commit-config.yaml` and `ci.yml` check the same paths.
- The GitHub Release is a pre-release with artifacts; R.0 decisions recorded.

---

## Wave 31 — SQLite state store

### Status and branch

- **Planned.** Implement on **`feat/31-sqlite-state`** after Wave 27. See R.0.7
  for whether it precedes the first PyPI release.
- **Review items S6, B6.**

### Goal

Persistent kernel state is written incrementally inside real database
transactions, in one file per project, with commit cost proportional to what
changed rather than to the repository.

### Evidence and root cause

- **Whole-index rewrites.** `NodeStore._save_metadata`
  (`mak/node_store/store.py:327-331`) serializes the **entire** metadata dict
  with `indent=2` and writes it atomically after every commit. In this
  repository's own `.mak/` (from a July 2026 run), `metadata.json` is
  **713 KB** — rewritten in full for a one-node commit.
- **Many small files.** Every node version is its own file under a mirrored
  tree: **4,904 files** in that same store.
- **Other state files follow the same pattern:** `lock_table.json` after every
  lock mutation, `task_graph.json` after every scheduler transition,
  `file_state.json`, and the `journal/` directory.
- **The transaction is hand-rolled.** `NodeStore.transaction()` snapshots the
  in-memory index and defers deletions so a single metadata save can serve as
  the commit point. A database transaction provides exactly that natively.
- **Precedent in the repo.** `research/contention_study/` already uses SQLite caches with
  a migration layer.

### Design decisions

#### D31.1 — One database, WAL mode

`.mak/state.db` (SQLite, `journal_mode=WAL`, `synchronous=FULL`,
`foreign_keys=ON`). Schema v1:

| Table | Columns |
|---|---|
| `meta` | `key`, `value` (schema version, created_at) |
| `nodes` | `node_id` PK, `file_path`, `kind`, `order_idx`, `version`, `retired`, `sep_after` |
| `versions` | `node_id`, `version`, `blob_sha` — PK (`node_id`, `version`) |
| `blobs` | `sha` PK, `content` — content-addressed, so identical versions share storage |
| `pending` | staged fragments of the current transaction |
| `file_state` | `file_path` PK, `materialized_sha`, `observed_sha`, `at` |
| `locks` | `resource`, `holder`, `mode`, `acquired_at`, `timeout_s` |
| `task_graph` | `key` PK, `json` |
| `commit_journal` | the journal record (file backups stay on disk; see D31.3) |

Indexes on `nodes(file_path, order_idx)`, and a `symbols(name, node_id)` table
maintained at commit for Wave 7's retrieval and the dispatch symbol index.

#### D31.2 — A backend boundary

A `StoreBackend` Protocol with `FilesBackend` (today's layout) and
`SqliteBackend`. `NodeStore`, `LockTable`, `Scheduler` persistence and
`file_state` talk to the protocol. `node_store.backend: sqlite | files`,
default `sqlite` for new projects.

#### D31.3 — The commit point becomes the database commit

`NodeStore.transaction()` maps to `BEGIN IMMEDIATE … COMMIT`; the COMMIT is the
commit point. Working-tree files are outside the database, so the file journal
remains, but its record moves into `commit_journal` inside the **same**
transaction as the node versions it describes. Recovery keeps its rule (compare
recorded versions with the store) and becomes a query.

#### D31.4 — One-way migration with a backup

Opening a `files` store with backend `sqlite` migrates it inside one database
transaction, verifies the node count and every blob digest, then renames the old
tree to `.mak/node_store.migrated-<timestamp>` (never deleted automatically).
`mak gc` gains `--drop-migrated`.

#### D31.5 — Concurrency unchanged

One connection per process, used under the store's existing `RLock`; the
project lease still guarantees a single process.

### Implementation plan

- **31.1** Benchmarks first: a synthetic 10,000-node store; measure bytes
  written and wall time per commit, startup load time, `gc` time on `files`.
- **31.2** `StoreBackend` protocol and `FilesBackend` extracted (pure refactor).
- **31.3** `SqliteBackend` for nodes/versions/blobs/pending/file_state.
- **31.4** Transaction mapping and the journal-record move (D31.3); crash tests
  (kill between phases), reusing the existing crash-recovery suite parametrized
  over both backends.
- **31.5** Lock table and task graph on the backend.
- **31.6** Migration (D31.4) with verification and `gc --drop-migrated`.
- **31.7** The `symbols` table and a switch of the dispatch symbol index to it.
- **31.8** Gates, benchmarks recorded in `benchmark/README.md`, docs
  (CONTRIBUTING §2 layout and API, §4.2, §11; CHANGELOG with the migration note).

### Required test matrix

| Case | Expected |
|---|---|
| whole node-store suite | passes on both backends (parametrized) |
| crash-recovery suite | passes on both backends |
| kill during commit, before COMMIT | nothing visible after reopen; files restored from journal |
| kill after COMMIT, before file install finished | roll forward on reopen |
| migration of a real `files` store | same nodes, versions, order, retired flags; digests verified; old tree renamed |
| migration interrupted | old store untouched; retry succeeds |
| identical content in two versions | one blob |
| `gc` retention | same results as the `files` backend |

### Acceptance criteria

- Bytes written per single-node commit are independent of store size (10 ×
  larger store → < 1.5 × bytes).
- Startup on the 10,000-node synthetic store is not slower than `files`.
- `.mak/` holds one database plus the journal backups instead of thousands of
  files.

### Deliberately out of scope

- Replacing the working-tree file journal (files are not in the database).
- Multi-process access to one project (the lease forbids it by design).

---

## Wave 32 — Scheduler fairness and plan-review previews

### Status and branch

- **Planned.** Implement on **`feat/32-scheduler-fairness`** after Wave 27.
- **Review items S8, B9, and S16's cost estimate and contention preview.** Both
  halves read the same thing — the plan's lock requests and DAG — to either
  schedule it or predict how it will schedule, so they share code.

### Goal

No ready task starves; long dependency chains start first; and before approving
a plan, a human sees its predicted contention, serialization and token cost.

### Evidence and root cause

- **Greedy FIFO.** `Scheduler.tick()` (`mak/scheduler/scheduler.py:133-153`)
  walks `ready_queue` in insertion order and dispatches every task whose
  all-or-nothing acquisition succeeds. No priority, aging or reservation.
- **Wide locks can starve.** A whole-file or whole-class WRITE conflicts with
  every INTENT_WRITE below it (CONTRIBUTING §4.5). A steady stream of narrow
  fragment writers of that file keeps at least one INTENT_WRITE held, so the wide
  task can be skipped on every tick indefinitely.
- **No critical path.** Ready tasks are not ordered by how much work depends on
  them, which matters for makespan on deep DAGs.
- **The watchdog cannot fire.** Atomic pre-allocation makes the wait graph
  acyclic by construction, yet `_check_deadlocks` runs every
  `deadlock_check_interval_s` (default 5 s).
- **Review shows no cost or contention.** `display_plan_for_review`
  (`mak/planner/review.py`) shows tasks, edges and findings; nothing tells the
  reviewer that ten tasks all WRITE one node, or roughly what the plan will spend
  against `max_total_tokens`.

### Design decisions

#### D32.1 — Priority

Ready tasks are ordered by (1) critical-path length — the longest chain of
dependents below the task, unit weight per task — descending; (2) ticks waited,
descending; (3) task id.

#### D32.2 — Reservation after waiting

A ready task whose acquisition has failed for `scheduler.reserve_after_ticks`
(default 5) consecutive ticks becomes **reserved**. While reserved, no other
task is dispatched whose lock request conflicts with the reserved task's
request (checked with the canonical conflict matrix, without acquiring). Current
holders drain; the reserved task then acquires. Reservations are served oldest
first, one per conflicting resource set. Deadlock-free: a reservation only
blocks *new* grants, never a holder, and a ready task's dependencies are already
complete.

#### D32.3 — The watchdog becomes an assertion

Default `deadlock_check_interval_s` rises to 60 and a final scan runs at wave
end. A cycle, if ever found, is logged as `DEADLOCK_INVARIANT_VIOLATED` at error
level and still resolved as today.

#### D32.4 — `PlanPreview`

`mak/scheduler/preview.py::preview_plan(subtasks, policy, store, config) ->
PlanPreview`:

- **Hot resources** — resources requested by ≥ 2 tasks in conflicting modes,
  with the tasks involved (from `lock_requests`).
- **Predicted schedule** — simulate D32.1/D32.2 with unit durations and
  `max_concurrent_agents`: predicted rounds, critical-path length, peak and mean
  concurrency.
- **Over-claim hints** — whole-file targets on files with many nodes, header
  targets (fewer after Wave 28), tasks whose targets span many files.
- **Cost estimate** — per task, input ≈ (write sources + context layers bounded
  by the configured budgets) / 4, output ≈ target source size × 1.3 capped by the
  agent budget, times `1 + expected retries` (from `max_attempts`, as a low/high
  band); planner cost already spent is added. Reported as a token range and
  compared with the remaining `session.max_total_tokens`.

#### D32.5 — Shown before approval, in both front ends

`display_plan_for_review` and the app's plan view render a short preview block;
a `PLAN_PREVIEW` event records it; when the high estimate exceeds the remaining
budget, the reviewer is warned before approving (`--no-review` prints the
warning).

### Implementation plan

- **32.1** Critical-path computation on `DAG` (cached; recomputed when cascade
  waves install).
- **32.2** Priority ordering and reservation in `Scheduler.tick` (D32.1, D32.2);
  config `scheduler.reserve_after_ticks`; persistence of wait counters.
- **32.3** Watchdog cadence (D32.3).
- **32.4** `preview_plan` (D32.4) with a deterministic simulator shared with 32.2.
- **32.5** Review rendering and event (D32.5).
- **32.6** Benchmark: a synthetic workload with one whole-file task and 20
  fragment writers of that file; record the wide task's wait before/after; a
  deep-DAG workload for makespan.
- **32.7** Gates and docs (CONTRIBUTING §6, §9; CHANGELOG).

### Required test matrix

| Case | Expected |
|---|---|
| wide task vs. a stream of 20 narrow writers | wide task dispatched within `reserve_after_ticks` + drain time |
| two reservations on overlapping resources | served oldest first; no deadlock |
| reservation vs. unrelated tasks | unrelated tasks unaffected |
| critical-path order | longest chain dispatched first when locks allow |
| recovery with waiting tasks | wait counters restored |
| preview of a plan where 10 tasks WRITE one node | that node listed hot with 10 tasks; predicted rounds ≥ 10 |
| estimate > remaining budget | warning before approval |
| preview determinism | same plan → same preview |

### Acceptance criteria

- No task in the starvation benchmark waits more than `reserve_after_ticks`
  plus the longest in-flight task.
- Makespan on the deep-DAG benchmark does not regress and improves where the
  critical path was previously scheduled late.
- Every plan shown for review includes contention and a token range.

### Deliberately out of scope

- Duration-weighted critical paths (unit weights until Wave 33 provides data).
- Currency pricing (tokens only, unless the catalog gains price data).

---

## Wave 33 — Evaluate what can actually fail

### Status and branch

- **Planned.** Implement on **`feat/33-honest-evaluation`**. The harness can be
  built at any time; numbers are meaningful after Waves 7 and 28 land.
- **Review items S11, Q2.** Also CONTRIBUTING's open issue "extend the
  benchmark".

### Goal

Measure the components most likely to fail in real use — the planner,
write-set prediction and agent quality — against a real baseline, on real
tasks, with several model families, and report the results with the same
honesty as the contention study.

### Evidence and root cause

- **Oracle plans.** `benchmark/harness/mak_runner.py:186-240` builds MAK's plan
  by hand: exact `target_nodes`, `changes_api=False`, precomputed
  `registry_keys`. The registry line is applied by a deterministic helper, so the
  model writes only one function body.
- **Simulated baseline.** The "traditional" side
  (`benchmark/harness/traditional.py`) makes sequential model calls, models
  parallel time as `max` over agents, and resolves conflicts with one model call
  — not a real agentic tool in real worktrees with test loops.
- **One model family, project-authored workloads** — all four templates are
  generated by the project around registries; recorded runs use
  `claude-sonnet-4-6`.
- **Template 4 is the exception** — a real planner — but it is the benchmark's
  own planner (`benchmark/harness/planner.py`), not MAK's `Planner` +
  validation.
- **The non-Python share is unreported**, although the contention study shows
  non-Python files saturate first.

### Design decisions

#### D33.1 — An end-to-end MAK arm

`--arm mak-e2e`: MAK's own `Planner` and validation, no declarations supplied,
agents write registry lines themselves. Reports plan-quality metrics from the
session log: corrected and unknown node findings, dropped sources, escalations
(Wave 28), re-dispatches per task, stale reads, cascade waves, planner tokens,
final oracle pass rate. The existing oracle-plan arm stays, labelled
**"kernel-only (oracle plan)"**, and the two are never averaged together.

#### D33.2 — A real worktree baseline

`--arm worktree-agents`: one git worktree per agent, each running a real agentic
CLI (Claude Code, Codex) non-interactively with the same task split, allowed to
run the project's tests; branches merged with real `git merge`, conflicts
resolved by an agent in a worktree; wall clock measured with real concurrent
processes; tokens from each CLI's usage output where available (reported as
unavailable otherwise, never estimated silently).

#### D33.3 — Real open-source tasks

`benchmark/realworld/`: 6–10 tasks pinned to exact upstream commits, cloned to a
cache outside the repository (`$MAK_BENCH_CACHE`), each with a written task
statement, a reference diff, and an oracle (the project's relevant tests plus
added tests). Targets: registry-heavy Python projects — e.g. a Django app (N
views + URLs + admin registrations), a plugin-style package (N plugins
registering into a table), an API-change-plus-callers refactor, a
test-generation fan-out. Licences recorded; no upstream code committed.

#### D33.4 — Several model families

At least three: Anthropic, OpenAI, and one open-weights model via Ollama or an
OpenAI-compatible endpoint. The same model on both arms per run.

#### D33.5 — Report the non-Python share

From each reference diff: files and lines outside `.py`, reported next to every
result.

#### D33.6 — Reporting and cost rules

Raw per-run JSONL committed; `benchmark/README.md` gains a results section per
arm with explicit labels; negative results kept; every real run requires
`--max-tokens` and refuses to start without it. CI runs keyless mock versions of
every new arm.

### Implementation plan

- **33.1** `mak-e2e` arm and plan-quality extraction from the session log.
- **33.2** `worktree-agents` arm with a CLI driver per tool; mock drivers for CI.
- **33.3** `realworld/` task format, fetch/cache tooling, the first three tasks,
  then the rest.
- **33.4** Non-Python share computation.
- **33.5** Report generation (`--render-only` support) and README/STATS sections.
- **33.6** A recorded campaign: every arm × every task × three models, with
  budgets agreed before running.
- **33.7** Docs: CONTRIBUTING Part III rewritten around arms and labels.

### Required test matrix

| Case | Expected |
|---|---|
| mock `mak-e2e` on Template 4 | metrics present; oracle computed |
| mock `worktree-agents` | real worktrees and merges; conflicts counted |
| real-world task fetch | pinned commit verified; cache reused |
| real mode without `--max-tokens` | refused |
| report rendering | oracle-plan and e2e numbers in separate, labelled tables |

### Acceptance criteria

- Published results for both MAK arms and the real baseline on at least six real
  tasks and three model families, with the non-Python share per task.
- Plan-quality metrics published for the e2e arm.
- CI exercises every arm keylessly.

### Deliberately out of scope

- Changing MAK to improve the numbers inside this wave — findings become input
  for later waves.

---

## Wave 8 — Language boundary and structured non-Python resources

### Status and branch

- **Planned.** Implement on **`feat/8-language-boundary`** after Waves 27 and 30
  (the formatter configuration is Wave 30's).
- **Merges the original Wave 8 (multi-language, 8.1–8.5) with review items S12,
  Q3 and Q12.** Phase A (8.1–8.3) is this wave's required scope. Phase B
  (8.4–8.6, other languages) may be split into its own wave when Phase A lands
  if it is too large for one branch; decide at planning time.

### Goal

Define the one boundary every language-specific operation goes through, make
Python its first implementation, and — following the contention study's own
conclusion — make the append-oriented text files that saturate first
(changelogs, dependency lists, YAML/JSON maps and arrays) lockable and mergeable
at key level. Then add a TypeScript backend.

### Evidence and root cause

- **Python is spread across subsystems (Q12).** `compile()` gates
  (`reconstruction.render_file`, `Session._preview_is_valid`,
  `_file_is_syntactically_valid`, the conflict detector's parse gate), the node
  kinds in ingestion, `api_digest`, `registrar.py` detection, signature and
  import checks, `depgraph`, and the planner's `is_python_target`
  (`planner.py:188`). No boundary names them.
- **Non-Python work is left undone (Q3).** The planner prompt says to leave
  non-Python artifacts out of the plan (`planner.py:62-66`); migrations,
  settings, YAML, JSON, docs and data files are silently not done or need a
  second tool.
- **Those files are where contention is.** The contention study found that at
  k = 16, 78.9–99.4% of windows have a path collision while only 32.9–66.5% have
  a Python-node collision; build/CI config, dependency lists, docs/changelogs and
  registries saturate first, and it names structured operations for
  append-oriented text as the next source of concurrency.

### Design decisions

#### D8.1 — `LanguageBackend`

```python
class LanguageBackend(Protocol):
    name: str
    extensions: tuple[str, ...]
    def split(self, path: str, source: str) -> list[NodeFragment]: ...
    def validate(self, path: str, source: str) -> list[str]: ...        # [] = valid
    def api_fingerprint(self, fragment: NodeFragment) -> str | None: ...
    def symbols(self, fragment: NodeFragment) -> list[str]: ...
    def registrars(self, fragment: NodeFragment) -> RegistrarShape | None: ...
    def import_edits(self) -> ImportEditor | None: ...                  # Wave 28
```

Formatting uses Wave 30's formatter configuration per extension. Backends are
selected by extension through a `LanguageRegistry` instance built at the
composition root. Structural conflict checks and `depgraph` stay Python-only and
are skipped (parse gate only) for other backends.

#### D8.2 — Structured text resources

`mak/resources/` handlers, each a backend variant whose "nodes" are keyed
entries:

| Resource | Node / key |
|---|---|
| `CHANGELOG.md`-style markdown | a bullet under a section heading; key = heading + normalized bullet |
| `requirements*.txt`, `pyproject.toml` dependency arrays | key = normalized package name |
| YAML / JSON / TOML maps | key = the map path (`a.b.c`) |
| YAML / JSON arrays of scalars or keyed objects | key = element value, or its `id`/`name` field |

Appends use the registrar pattern: INTENT_WRITE on the file, WRITE on
`<path>#key=<k>`, a commutative textual splice that preserves formatting and
comments. Edits to an existing key take that key's WRITE. Files that match no
handler become whole-file text nodes, validated by parsing (JSON/YAML/TOML) or
not at all (plain text). The planner may target `path#key=<k>` ids; the "leave
non-Python out" instruction is replaced by a description of what is supported.

#### D8.3 — Phase B: other languages

TypeScript on tree-sitter (its node ranges fit span tiling), gated by the
round-trip property test; then Go and Rust; mixed repositories routed by
extension. Dependencies (`tree-sitter`, grammars) go in an optional extra.

### Implementation plan

- **8.1 `LanguageBackend` + `PythonBackend`** — pure refactor routing every call
  site listed under Evidence through the boundary; Wave 27's goldens must
  reproduce.
- **8.2 Structured text resources** (D8.2) — handlers, key resources in
  `resources.py`, merge, planner prompt and validation changes, a fan-out
  benchmark (N tasks each adding a dependency and a changelog entry).
- **8.3 Formatter per extension** — reuse Wave 30's configuration.
- **8.4 (Phase B) TypeScript backend** + round-trip test; deps in
  `[typescript]`.
- **8.5 (Phase B) Non-Python conflict checks** — parse gate and name collisions
  first.
- **8.6 (Phase B) Go / Rust; mixed-language routing.**
- **8.7 Gates and docs** for each phase.

### Required test matrix

| Case | Expected |
|---|---|
| Python through the boundary | goldens reproduce |
| 5 tasks each add a dependency to `requirements.txt` | all commit concurrently; file valid; no duplicates |
| 2 tasks add the same dependency with different versions | conflict reported |
| 5 tasks add changelog bullets under one heading | all present; comments and formatting preserved |
| YAML map key added concurrently by 3 tasks | merged; comments preserved |
| invalid JSON produced | rejected by `validate` |
| (B) TS round trip | byte-identical under `formatter: none` |
| (B) `mak run` edits a TS project end to end | succeeds |

### Acceptance criteria

- Phase A: every Python-specific call site goes through `LanguageBackend`; the
  structured-resource fan-out benchmark runs concurrently with zero lost
  entries.
- Phase B: a TypeScript backend passes the round-trip property test and an
  end-to-end run.

### Deliberately out of scope

- Semantic checks for non-Python languages beyond the parse gate and name
  collisions.
- Arbitrary prose files (whole-file only).

---

## Wave 34 — The kernel as a coordination service (library + MCP)

### Status and branch

- **Planned.** Implement on **`feat/34-kernel-service`** after Waves 27 and 28
  (it exposes the commit pipeline and escalation).
- **Review items S15, Q1.**

### Goal

Let other agent frameworks — including agentic CLIs with their own planning and
tool loops — **commit through** MAK and get node locks, transactional commits,
stale-read detection and semantic checks without adopting MAK's planner or
agent model. Reposition the project around that strength.

### Evidence and root cause

- **Only one way in.** The kernel's guarantees are reachable only through
  `Session` (`install_plan` + `run`) driving MAK's own adapters. An external
  agent cannot take a lock, stage a fragment, or ask for a commit.
- **The evidence says this is the product.** In the review's terms, bet A
  (scheduling-time coordination with database-grade mechanics) is strong and bet
  B (single-shot agents) is the weakest. A service interface keeps A and C and
  routes around B.
- **Positioning (Q1).** The contention study found zero change-versus-change
  textual conflicts across 124,473 concurrent human PR pairs, so "no merge
  conflicts" is a weak lead. README and CONTRIBUTING's "Why not Git worktrees?"
  still lead with it. The strong claims are safe fan-out into shared hot spots
  and commit-time semantic checking.

### Design decisions

#### D34.1 — A stable library facade: `mak.kernel`

```python
project = mak.kernel.open_project(work_dir, config)      # takes the lease, reconciles
lease = project.begin_task(task_id, targets, context=..., declarations=...)
#   -> TaskLease, or raises WouldBlock (non-blocking, atomic, via lock_requests)
src = lease.read(node_id)                # recorded in the read set
lease.stage(node_id, new_source)         # also imports / additive symbols (Wave 28)
lease.request_nodes([...])               # escalation (Wave 28)
report = lease.commit()                  # CommitPipeline + transaction + audit
lease.abort()
project.post_wave_check() -> list[Defect]
project.close()
```

Versioned separately from internals, documented in CONTRIBUTING, with its own
test suite. `Session` is rebuilt on top of the facade (D34.3).

#### D34.2 — An MCP server

`mak mcp serve --work-dir <dir>` (stdio), in an optional `[mcp]` extra. Tools:
`mak_begin_task`, `mak_read_node`, `mak_list_file`, `mak_find_symbol`,
`mak_stage`, `mak_commit`, `mak_abort`, `mak_status`, `mak_post_wave_check`.
Resource: the inventory (Wave 7's view). Leases expire through the existing lock
timeouts, so a client that disappears loses its locks.

#### D34.3 — `Session` becomes a client of the facade

MAK's own planner-and-agents mode uses the same calls external clients do,
proving parity and preventing two commit paths from drifting.

#### D34.4 — Reposition the docs

README "What/Why" leads with safe, validated fan-out of many small agent edits
into shared hot spots, and with commit-time semantic checking; it cites the
contention study honestly, including its negative results. "Why not Git
worktrees?" in CONTRIBUTING is rewritten on the same basis.

#### D34.5 — A worked integration

`demo/mcp/`: configuration for Claude Code and Codex to use the MCP server, and a
scripted fan-out demo.

### Implementation plan

- **34.1** Facade types and `open_project`/`begin_task`/`read`/`stage`/`commit`/
  `abort` over the Wave 27 collaborators; facade test suite.
- **34.2** Escalation and additive/import staging through the facade.
- **34.3** `Session` on the facade; goldens reproduce.
- **34.4** MCP server and the `[mcp]` extra; protocol tests with an in-process
  client.
- **34.5** Lease expiry for vanished clients.
- **34.6** Demo and docs (D34.4, D34.5); CHANGELOG.

### Required test matrix

| Case | Expected |
|---|---|
| two facade clients, disjoint nodes | both commit concurrently |
| two clients, same node | second gets `WouldBlock` |
| client reads X, another commits X, first commits | stale-read verdict as in a session |
| client vanishes | its locks expire; others proceed |
| MCP round trip begin → stage → commit | audit commit made; events logged |
| `Session` on the facade | goldens reproduce |

### Acceptance criteria

- An external agent (via MCP) can complete a fan-out task with MAK providing
  locks, commit validation and audit, and no MAK planner or adapter involved.
- `Session` and the facade share one commit path.
- The README leads with the fan-out and semantic-checking claims.

### Deliberately out of scope

- Network transports beyond stdio.
- Multi-project servers.

---

## Follow-ups from Wave 27 (hotfix-sized)

Spotted during the session decomposition and deliberately left out of a
behaviour-preserving wave:

- **History narration without a wave number.** No `Wave N` remains in `mak/` or
  `cli/`, but many docstrings still say "used to" / "before this"; rewrite them
  as the current contract. `pyproject.toml` comments still cite "Wave 15, D8"
  and "Wave 9.3".
- **Make `CommitContext` read-only in fact.** Checks can still mutate
  `ctx.wave`, and `ReadSetCurrent` bumps the `stale_reads` /
  `stale_redispatches` / `adjudicated_accepts` counters itself. A frozen wave
  view plus counters carried on `Verdict` would make the contract enforceable.
- **Registry protocol parameter name.** Pyright reports `AdapterRegistry.get
  (agent_id)` vs `AdapterRegistryLike.get(agent_type)` at the scheduler call
  sites (mypy is clean); align the names.
- **Split `mak/config.py`** (1,000+ lines) — the next consolidation candidate.

---

## Deferred ideas (no wave yet)

- **Finer granularity (rest of Q4):** split `module_body` by statement groups;
  nested functions and inner classes as nodes. Revisit after Wave 28 shows
  whether headers were the dominant hot spot.
- **Embedding retrieval for the planner** (after Wave 7's `Retriever`).
- **Default `tools: read` for agents** (after Wave 33's data).
- **Splitting `mak/config.py`** (next consolidation wave).
- **Stronger semantic gates:** a differential property-test gate for behaviour
  changes behind an unchanged signature; coverage-driven test selection for
  `impact_tests` instead of the static import closure; an optional "revert
  instead of fix-up" resolution offered to the reviewer (`revert_node` already
  supports it).
- **Retired-node metadata sweep:** a retired node's metadata entry is kept
  forever, even after retention prunes its last version file; add a `gc` pass
  that removes entries whose versions are all gone, without letting `gc` treat
  live directories as orphans.
