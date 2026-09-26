# TASKS

The planned work for MAK, in priority order.

---

## Wave index (priority order)

| Wave | Title | Review items | Depends on | Branch |
|:-:|---|---|---|---|
| [**7**](#wave-7--retrieval-based-graph-aware-planner) | Retrieval-based, graph-aware planner | S2, B2 | - | `feat/7-planner-retrieval` |
| [**28**](#wave-28--write-sets-that-can-grow-safely) | Write sets that can grow safely | S3, B3, Q4 (headers) | - | `feat/28-growable-write-sets` |
| [**29**](#wave-29--agents-that-can-look-and-test) | Agents that can look and test | S4, B4, Q5 | 28 | `feat/29-agent-tools` |
| [**30**](#wave-30--respect-the-users-repository) | Respect the user's repository | S5, S9, S10, S16 (clean tree), B5, B10, B11 | - | `feat/30-repository-respect` |
| [**R**](#wave-r--first-public-release) | First public release | S16 (spend cap), Q6 | 30 | `feat/R-release-prep` |
| [**31**](#wave-31--sqlite-state-store) | SQLite state store | S6, B6 | - | `feat/31-sqlite-state` |
| [**32**](#wave-32--scheduler-fairness-and-plan-review-previews) | Scheduler fairness and plan-review previews | S8, S16 (previews), B9 | - | `feat/32-scheduler-fairness` |
| [**33**](#wave-33--evaluate-what-can-actually-fail) | Evaluate what can actually fail | S11, Q2 | 7, 28 (for meaningful numbers) | `feat/33-honest-evaluation` |
| [**8**](#wave-8--language-boundary-and-structured-non-python-resources) | Language boundary and structured non-Python resources | S12, Q3, Q12 | 30 | `feat/8-language-boundary` |
| [**34**](#wave-34--the-kernel-as-a-coordination-service-library--mcp) | The kernel as a coordination service (library + MCP) | S15, Q1 | 28 | `feat/34-kernel-service` |


---

## Wave 7 — Retrieval-based, graph-aware planner

### Status and branch

- **Planned.** Implement on **`feat/7-planner-retrieval`** (no branch exists).
- **Merges the original Wave 7 (planner token efficiency, 7.1–7.4) with review
  items S2 and B2.** The original wave treated the problem as cost; the review
  adds that it is also quality — the planner guesses from names what the kernel
  already knows from its dependency graph.
- Builds on the one planning entry point, `Session.propose_plan` (shipped in
  0.9.3b). **Wave 27 has landed**, so the session side of this wave lives in
  `mak/session/planning.py` (`PlanPreparer`) and `mak/session/types.py`
  (`PlanProposal`). `mak/session/core.py` is at **588 / 600** lines
  (`tests/test_module_budgets.py`): every session-side addition in this wave
  goes into `PlanPreparer`, and `core.py` may grow by at most ~10 lines.
- **Hot path.** Every run that plans goes through this code, and the plan
  decides everything after it: write sets, locks, parallelism, cascades. Keep
  the default behaviour for small repositories as close to today as possible,
  and make every behaviour change visible in the log and in plan review.
- **Coordinates with:** Wave 28 (D28.6 also edits `_PLAN_INSTRUCTIONS`: land
  this wave first; Wave 28 adds its text inside the stable prompt prefix
  defined in D7.7), Wave 32 (plan-review cost previews can read D7.1's
  metrics), and Wave 33 (D33.1 reports `planner_*` metrics from D7.1).

### Goal

The planner's input grows **sub-linearly** with repository size, never exceeds
a configured budget without saying so, reuses a cached prefix across retries
and expansion rounds, and gets the real call graph and real signatures instead
of being asked to guess callers from names. When a plan changes a signature,
every caller the graph can see is covered by a task or reported as a finding.
No caller is left out silently.

### Evidence and root cause

Line references are for `main` @ `efd498d` (after Wave 27).

- **Everything, every time.** `Session.propose_plan` passes
  `self._node_store.list_nodes()` — the entire inventory — to
  `Planner.decompose` (`mak/session/core.py:314-316`). The app goes through the
  same call (`cli/runner.py:110`, `plan_in_thread`). `Planner._build_prompt`
  (`mak/planner/planner.py:600-617`) renders every id as a bullet line. For
  MAK's own tree: **146 files → 1,829 nodes → ~104 K characters ≈ 26 K
  tokens**, ids only. A 1 M-line repository would need several hundred thousand
  tokens.
- **Retries resend all of it.** `_complete_with_retries` (`planner.py:662-705`)
  re-sends the full prompt plus a note on every attempt, and appends the note at
  the *end* of one string. The optional critique pass (`planner.py:638-660`)
  sends the plan again.
- **Outline mode is still O(repo).** `_build_outline_prompt`
  (`planner.py:730-745`) lists every file with every symbol name, then runs one
  detail call per step.
- **The model guesses callers.** The "CASCADE PREVENTION" instruction
  (`planner.py:75-83`) tells the model to *"search the inventory for any node
  whose name suggests it calls a symbol you are changing"*. The kernel builds
  the real reference graph (`dep_graph_from_store`) only afterwards: once in
  `PlanPreparer.validate` (`mak/session/planning.py:51-52`) and again in
  `install_plan` (`core.py:361`). Each build parses every node in the store,
  so one planned wave builds the graph **twice**. The graph is used only to
  repair edges.
- **The graph has no reverse index.** `DepGraph` (`mak/planner/depgraph.py:40-51`)
  stores `references` (node → what it references) and `definers`. "Who calls X"
  means scanning every entry. The resolver is conservative: `self.method()`
  calls, dynamic dispatch and callbacks give no edge. So a graph-derived caller
  list is a lower bound, never the full set.
- **Names without shapes.** The inventory has no signatures, so the model
  cannot tell whether a change is body-only, or which callers a signature change
  would break. `mak/node_store/api_digest.py` can already render declarations,
  but `public_api_digest` hides private names and works on whole sources, and
  the per-function renderer `_signature` is private.
- **Local planners hit a wall early.** `OllamaPlannerLLM._effective_num_ctx`
  (`mak/planner/llm.py:362-381`) refuses when the inventory does not fit the
  model's window, which is correct but makes a local planner unusable on
  mid-size repositories. Its own docstring says "Bounding the inventory itself
  is the real fix".
- **Measurement is partial, and caching would break it.** `Planner.token_usage`
  accumulates totals (`planner.py:587-598`), but nothing logs per-call prompt
  size, inventory size, or cached tokens. `extract_usage`
  (`mak/agent_runner/stop_signals.py:35-49`) maps no cache fields. Anthropic's
  `input_tokens` **excludes** cache reads and cache writes, so turning on
  caching without changing the accounting would make both planner spend and
  `session.max_total_tokens` under-count. OpenAI's `prompt_tokens` and
  Gemini's `prompt_token_count` already *include* their cached subset.
- **Review cannot remove a task in the app.** The app keeps only `.subtasks`
  from the proposal (`cli/runner.py:110`). `show_plan` (`cli/ui.py:169`) shows
  no findings, and `_confirm_plan` (`cli/app.py:312`) is a y/N prompt. "Shown
  as proposed, removable like any task" (original D7.6) is therefore not true
  in the app today.
- **The original benchmark hook would not measure this wave.** Template 4's
  planner is the benchmark's own (`benchmark/harness/planner.py`; see
  `benchmark/README.md`, "Measurement scope"), not MAK's `Planner` + validation.

### Design decisions

#### D7.1 — Measure first

- **`PlannerCall` record** (`mak/planner/telemetry.py`, frozen dataclass), one
  per LLM call. Fields:
  - `phase`: `plan` | `outline` | `detail` | `critique` | `expand` | `verify`
  - `round`: expansion round, starting at 0
  - `attempt`: retry index within the round, starting at 1
  - `strategy`: `oneshot` | `outline` | `full` | `retrieval`
  - `prompt_chars`, `stable_chars` (the cacheable prefix, D7.7)
  - `inventory_files_total`, `inventory_files_shown` (level 1),
    `inventory_nodes_shown`, `inventory_chars`, `collapsed_dirs`,
    `symbols_truncated`
  - `seed_files`, `expanded_paths`
  - `input_tokens`, `output_tokens`, `cached_input_tokens`,
    `cache_write_tokens`
  - `outcome`: `plan` | `expand` | `rejected` | `call_failed` | `truncated`
  - `duration_ms`
- **Emitted live, not after the fact.** `Planner.plan(...)` takes an
  `observer: Callable[[PlannerCall], None] | None`. `PlanPreparer` passes one
  that logs `EventType.PLANNER_CALL`. A run that ends in `PlannerFailedError`
  therefore still logs every call it paid for. **No prompt text and no source
  go into the event**, only sizes and counts; the event log is not a second
  copy of the repository.
- **Usage normalization** (`mak/planner/llm.py`), per backend, so that
  `input_tokens` always means *every prompt token the provider processed* and
  `cached_input_tokens` is a subset of it:

  | Backend | `input_tokens` | `cached_input_tokens` | `cache_write_tokens` |
  |---|---|---|---|
  | Anthropic | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` |
  | OpenAI | `prompt_tokens` | `prompt_tokens_details.cached_tokens` (nested; `0` when absent) | — |
  | Gemini | `prompt_token_count` | `cached_content_token_count` | — |
  | Ollama | `prompt_eval_count` | — (not reported) | — |

  Only the planner backends change. The agent adapters' `extract_usage` stays
  as it is: agents send no `cache_control`, so their cache fields are zero, and
  changing a shared helper is out of scope. `session.max_total_tokens` keeps
  summing `input_tokens + output_tokens`, which is now conservative by
  construction: cached tokens are counted, never dropped.
- **Metrics.** `plan_metrics` (`mak/session/results.py`) gains
  `planner_calls`, `planner_rounds`, `planner_input_tokens`,
  `planner_cached_tokens` and `planner_output_tokens`. Planning happens
  *before* `install_plan` replaces the `WaveState`, so the summary has to be
  carried across. `PlanPreparer` holds the last proposal's `PlanningSummary`,
  and `install_plan` hands it to `WaveState.start(..., planning=...)` through
  `PlanPreparer.take_planning_summary()`, which returns it once and clears it.
  Cascade waves and directly installed plans get zeros.

#### D7.2 — A hierarchical inventory view

`mak/planner/inventory.py::InventoryView`, built from the store, the
`DepGraph` and a reverse index. It is cached per store `generation` (D7.9) and
renders three things:

- **Level 0 — tree.** Directories and files with node counts, depth-first,
  sorted by path:
  ```
  mak/ (118 files, 1,502 nodes)
    planner/ (8 files, 164 nodes)
      depgraph.py 31
      planner.py 58
  ```
- **Level 1 — file detail.** A header line, then one line per node with its
  **id suffix**, **shape**, and **incoming references**. The full node id is
  the file path plus the suffix (the instructions say so). Writing the suffix
  instead of the full id saves ~40% of the characters. A wrong id this causes
  is exactly what grounding already corrects (`_tier_wrong_kind`,
  `_tier_missing_class`).
  ```
  mak/planner/validation.py  (32 nodes · imported by 3 files)
    ::module_header::__header__
    ::class::PlanFinding  class PlanFinding  ← 41 refs · 9 files (session/planning.py, planner/review.py, +7)
    ::function::validate_plan  def validate_plan(plan: list[SubTask], graph: DepGraph, inventory: list[NodeId], *, semantic: PlanSemantics | None=...) -> ValidationResult  ← 3 refs · 2 files (session/planning.py, +1)
    ::function::_ground_ids  def _ground_ids(ids, known, inventory, task_id, *, is_context) -> ...  ← 1 ref · this file
  ```
  - The shape comes from a new public `api_digest.node_signature(source, kind)`
    built on the existing `_signature` / `_parse_lenient`:
    - `def`/`async def` lines with **default values elided to `=...`**. A
      default can carry a literal secret or an internal URL, and the planner
      only needs to know that a parameter is optional.
    - `class Name(bases)` headers; decorator **names** only, without
      arguments.
    - No bodies and no docstrings.
    - One line, capped at 160 characters with a trailing `…`.
    - Private names are included: the planner may target them.
    - `module_header` / `module_body` lines carry no shape.
  - `← n refs · m files (top 3 files by reference count)` comes from the
    reverse index; `this file` when every referrer is local.
  - **Level 2 is not a separate rendering.** The original plan listed "node ids
    for an expanded file" as level 2. Level-1 lines are keyed by exact id
    suffixes, so level 2 would repeat the same ids without the useful columns.
- **Flat rendering.** Today's `  - <id>` listing, kept for
  `strategy: oneshot` and `outline` so they stay byte-identical.
- **Sizes are estimated with `estimate_tokens`** (the existing `len/4` in
  `ollama_api_adapter.py`). Import it; do not add a second estimator.
- **Deterministic.** The same store generation and the same requests give
  byte-identical renderings. There are no sets in the output order, no
  timestamps, and no absolute paths. D7.7's caching depends on this.

#### D7.3 — The planner may ask to expand

- **Reply shapes.** One reply is either a plan (array, or `{"subtasks": …}`)
  **or** `{"expand": ["pkg/a.py", "pkg/b/"], "why": "<optional>"}`. A reply with
  both is a `ValueError`: a normal retry with a note. The parser is
  `mak/planner/expansion.py::parse_reply(raw) -> PlanReply | ExpandRequest`. It
  reuses `loads_json` and `parse_plan`.
- **What an expansion returns:**
  - a **file** path → its level-1 detail;
  - a **directory** path (trailing `/` optional) → its subtree at level 0, one
    level deeper than currently shown, **not** level-1 detail for every file
    (that would spend the budget on one request);
  - an already-shown path → listed as "already shown" (costs nothing);
  - an unknown path → listed as "not found", with up to 3 close file paths
    from `difflib`;
  - a non-`.py` path → "not a Python file".
  - None of these is an error or a retry.
- **Rounds.** The expansion loop sits **outside** `_complete_with_retries`:
  each round is one `_complete_with_retries(prompt_parts, parse_reply)`, so a
  malformed reply inside a round is retried as today. An expansion is progress,
  not a failure.
  - Every round counts toward `planner.max_expansions` (default 3), including
    rounds that only asked for already-shown or unknown paths. This stops
    loops.
  - When the rounds or the budget run out, the next prompt ends with "Do not
    ask to expand further. Return the plan now." A further expand reply is
    then a `ValueError` (retry note: "expansion is closed; return the plan").
- **Budget.** `planner.inventory_token_budget` bounds the **inventory section
  cumulatively** (tree + seeds + every expansion) for one `plan` call, not per
  round. Expansions are added in request order until the next one would not
  fit. The rest are listed as "not expanded: inventory budget reached", and
  their count goes into `symbols_truncated` / `PlannerCall`.
- **Unseen-target verification (`phase: verify`).** When a plan parses, list
  its targets that are **not in the inventory**, whose **file is in the
  inventory**, and whose file was **never shown at level 1**. In other words,
  the model named a symbol in a file it never saw.
  - If rounds remain, spend one round showing those files at level 1 with the
    note "your plan targets ids in files you had not seen; they are shown
    below; return the corrected plan".
  - Otherwise return the plan and let validation flag the targets (next
    bullet).
  - A target whose exact id **is** in the inventory is real: it is accepted
    whether or not its file was shown.
  - A target in a file that does not exist is a legitimate new file, as today.
- **Validation rule.** `PlanSemantics` gains `seen_files: frozenset[str] | None`
  (`None` = no retrieval, today's behaviour). With it set, the "genuinely new
  symbol, silent" branch of `_ground_ids` (`validation.py:223-224`) does not
  apply to ids in existing, unseen files. Such an id gets a correction on one
  confident match, or a new advisory finding `unseen_target` with same-file
  suggestions. It is kept in the plan, like `unknown_node`, but never silently
  accepted.

#### D7.4 — Strategy selection

`planner.strategy` gains `full`, `retrieval` and a new default, `auto`:

| Strategy | Inventory sent | Calls | When `auto` picks it |
|---|---|---|---|
| `oneshot` | flat id listing (today, byte-identical) | 1 (+ retries) | never; only when named |
| `outline` | today's outline + detail passes, unchanged | 1 + steps | never; only when named |
| `full` | level 1 for **every** file (ids + shapes + refs), no tree | 1 (+ retries; every file is shown, so no `verify` round) | estimated full level-1 view ≤ `inventory_token_budget` |
| `retrieval` | tree + seeds + expansions (D7.3, D7.5) | 1 + ≤ `max_expansions` | otherwise |

- `auto` measures the **level-1** size, not the flat-id size. A repository
  small enough to show whole is shown *with* shapes and callers, so the quality
  gain from this wave reaches small repositories too, not only large ones.
- Default `inventory_token_budget` is **12,000** estimated tokens (~48 K
  characters). MAK's own tree is ~46 K tokens at level 1, so it plans with
  `retrieval`. The benchmark templates and golden scenarios are far below the
  budget, and none of them calls the planner (they install plans directly), so
  goldens must not change.
- `oneshot` and `outline` keep working when named explicitly. `outline` is not
  reworked in this wave (see out of scope).
- The Ollama refusal message (`llm.py:370-378`) additionally suggests lowering
  `planner.inventory_token_budget` or using `strategy: auto`.

#### D7.5 — Seed the retrieval deterministically

Before the first `retrieval` call, pre-expand the files most likely to matter.
`mak/planner/retrieval.py`:

- **`Retriever` protocol:**
  `seed(task: str, view: InventoryView, budget_tokens: int) -> SeedResult`,
  where `SeedResult` holds the ordered file list, the matched terms, and a score
  per file. The only implementation in this wave is `LexicalRetriever`.
  Embedding retrievers plug in later without touching the planner.
- **Term extraction from the task text:**
  - backticked spans, dotted names (`pkg.mod.func`) and `/`-paths are kept
    whole and also split;
  - identifiers are split on camelCase, snake_case and digits, then
    case-folded;
  - drop tokens shorter than 3 characters and a small fixed English and Python
    stop-list (`the`, `add`, `get`, `set`, `self`, `init`, `test`, …).
- **Scoring, per node:**
  - an exact symbol-name or exact file-path match scores **10**;
  - a match on a whole path segment scores **4**;
  - each sub-token match scores **IDF × 1**, where IDF comes from sub-token
    frequencies over all symbol names in the view. `get` matching 2,000 names
    is worth almost nothing, `invoice` matching 6 is worth a lot.
- A file scores the sum of its best 5 node scores.
- **1-hop neighbourhood.** For the top 5 files, the files of their nodes'
  referrers and referents get **0.5 ×** the parent's score.
- **Selection.** Rank by score descending, then path ascending (deterministic).
  Take files while their level-1 renderings fit the **seed share** of the
  budget. No match at all → no seeds; the tree alone plus the model's own
  expansions.
- **Budget shares (module constants, not config):** tree ≤ **25%**, seeds ≤
  **35%**, planner-requested expansions get the rest (≥ 40%). This is less than
  the original "up to half" for seeds, on purpose: what the model asks for
  after reading the tree is a better signal than a lexical guess.

#### D7.6 — The kernel supplies callers

- **Reverse index.** `mak/planner/depgraph.py::referrers(graph) ->
  dict[NodeId, frozenset[NodeId]]`, a pure inversion of `references`, cached
  with the graph (D7.9). Level-1 lines show its counts (D7.2).
- **Finding missing callers.** `mak/planner/callers.py::find_missing_callers(plan,
  graph, referrers) -> list[MissingCaller]` runs inside `validate_plan` when
  `semantic` is supplied.
  - **Changing targets:** `function` and `method` nodes in
    `api_write_targets(task)` of a task with `changes_api is True`.
  - **Callers:** `referrers[target]`, minus nodes that are *covered*. A node is
    covered when some task targets it, or targets its file as a whole-file
    node. Callers inside the changing task's own targets are covered by
    definition.
  - **Each uncovered caller → finding `missing_caller`** (advisory):
    "'<caller>' references '<target>', whose API '<task>' changes; no task
    updates it".
  - **`changes_api is None` (undeclared)** on a function or method with ≥1
    uncovered referrer → **one** finding per task, `undeclared_api_callers`:
    "N graph callers of these targets are outside the plan; declare
    `changes_api` so MAK can check them". No tasks are proposed for undeclared
    changes: most undeclared tasks are body edits, and proposing a task per
    caller would flood the plan.
  - **`class` targets** get findings only. References to a class include type
    annotations that never need updating, so proposing tasks for them would
    mostly create no-op tasks.
- **Proposing caller tasks** (`propose_caller_tasks`), when
  `planner.auto_caller_tasks` is on (default `true`):
  - **One task per caller *file***, targeting that file's uncovered caller
    nodes. The task `depends_on` **every** changing task whose target those
    nodes reference, so two tasks never write the same node unordered (the
    `parse_plan` rule "only if one depends on the other").
  - `changes_api: false`; `context_nodes` = the changed targets; `agent_type`
    empty, so round-robin assignment applies.
  - `task_id` = `mak.callers.<n>`, numbered in caller-file path order. On a
    clash with a planner id, add a suffix.
  - Description template: "Update the references to `<symbol>`
    (`<target id>`) in these nodes to match its new signature: `<contract>`.
    Change only call sites; keep these nodes' own signatures." When there is no
    contract: "…to match the new signature committed by `<task>`". The
    dispatch enricher already ships dependency outputs.
  - **Capped by `planner.max_caller_tasks`** (default 25). Above the cap,
    findings only, plus one `caller_tasks_capped` finding with the count.
  - **Invariants.** The augmented plan must re-pass `parse_plan`'s invariants
    (whole-file ownership, one granularity per file). A proposed task that
    would violate one is not added; it becomes a finding naming the rule.
  - Proposed tasks are **leaves**: nothing depends on them. Dropping them is
    always safe (asserted in `drop_tasks`).
- **Only on the planner path.** `propose_caller_tasks` runs in
  `PlanPreparer.propose` (the `propose_plan` path) **only**, never inside
  `validate_plan`. `install_plan` re-validates every plan (app installs,
  cascade waves, user edits). If proposing were part of validation, a reviewer
  who removed a proposed task would get it back at install. With this split,
  install re-emits the `missing_caller` finding for it, and the finding is
  logged but the task is not re-added.
- **`PlanProposal`** (`mak/session/types.py`) gains
  `proposed_task_ids: frozenset[str]` and `planning: PlanningSummary`.
  `SubTask` gets **no** new field, so there is no codec, recovery, or scheduler
  annotation change.
- **Prompt text.** Replace CASCADE PREVENTION (`planner.py:75-83`).
  - With `auto_caller_tasks` on:
    > CALLERS: MAK knows the static call graph (the "← refs" column). When a
    > task changes a function's or method's signature, set "changes_api": true
    > and give its "contract"; MAK then adds caller-update tasks for every
    > caller it can see, so do not add those yourself. Add caller tasks only
    > for callers MAK cannot see: calls through self or an instance, dynamic
    > dispatch (getattr, registries, callbacks), and code that other tasks in
    > this plan create. When a task only changes function bodies, set
    > "changes_api": false.
  - With it off, the model must add every caller itself. The paragraph says
    to use the "← refs" column (`full` / `retrieval`) or the inventory
    (`oneshot`), and still asks for `changes_api` / `contract`.
- **Post-wave cascade is unchanged.** It is still the safety net for callers
  that nobody covered: declined proposed tasks, invisible calls. D7.10's
  benchmark reports cascade waves before and after.

#### D7.7 — Cacheable prompts

- **Protocol.** `PlannerLLM` stays `complete(prompt) -> str`. A new optional
  protocol, `CachingPlannerLLM`, adds
  `complete_parts(stable: Sequence[str], volatile: str) -> str`. The planner
  checks with `isinstance` / `getattr` and otherwise calls
  `complete("".join(stable) + volatile)`, so test stubs and third-party LLMs
  keep working unchanged.
- **Prompt layout.** Stable blocks are append-only across the rounds and
  retries of one plan:

  | # | Block | Changes when |
  |---|---|---|
  | S1 | instructions (strategy-specific) + configured agents + level-0 tree (or the full level-1 view) | store generation, config, roster |
  | S2 | user task + seed expansions | per plan |
  | S3… | each completed expansion round's reply material | appended per round |
  | V | round directive ("plan now" / "verify") + retry note | every attempt |

  This moves the task from before the inventory (today) to after it. The
  instructions and the tree then form a prefix that is identical across
  *different* tasks in one session (the interactive app plans many tasks), not
  only across retries. `oneshot` keeps today's order and bytes (one stable
  block + note), because it is the byte-compatibility mode.
- **Anthropic:** content blocks in the single user message. `cache_control:
  {"type": "ephemeral"}` goes on S1 and on the **last** stable block. That is
  2 breakpoints, below the API's 4. Prefixes below the model's minimum
  cacheable length (512–4,096 tokens depending on the model) silently do not
  cache; this is expected and not an error. The default 5-minute TTL covers
  retries and rounds.
- **OpenAI** (cloud and OpenAI-compatible): send the concatenation. Automatic
  prefix caching needs only byte stability. **Gemini:** concatenation; implicit
  caching where the model supports it. **Ollama:** concatenation. Ollama
  reuses its KV cache for an identical prefix *only if the model is not
  reloaded*, and it reloads when `num_ctx` changes. `complete_parts` therefore
  sizes `num_ctx` once per plan, from the budget ceiling (instructions +
  `inventory_token_budget` + output), rounded up to a multiple of 8,192, so
  that rounds do not force a reload.
- **Opt-out.** `planner.prompt_cache: true` (default). `false` sends no
  `cache_control`, as an escape hatch for gateways that reject the field. It
  does not change the prompt layout.
- `cached_input_tokens` / `cache_write_tokens` come from D7.1's normalization.

#### D7.8 — Truncation is measured, never silent

- **Tree.** If the full level-0 tree exceeds the tree share (25%), expand it
  breadth-first from the root while it fits, in path order. Every directory
  not expanded renders as `pkg/sub/ (37 files, 412 nodes) [collapsed — expand
  to see]`, and the header says `N directories collapsed`.
  `collapsed_dirs` is logged.
- **Large files.** A file whose level-1 rendering exceeds **25% of the budget**
  shows its first nodes in source order up to that cap, then `… k more nodes
  not shown (budget); their ids are still valid targets`. The count goes into
  `symbols_truncated`.
- **Expansions over budget** are listed as not expanded (D7.3) and counted.
- **Nothing is dropped without a line in the prompt *and* a count in
  `PLANNER_CALL`.**

#### D7.9 — One graph per store generation

`PlanPreparer` owns a `PlanningIndex` (frozen dataclass: `generation`,
`graph`, `referrers`, `view`). It is rebuilt only when
`NodeStore.generation` moves, the same pattern as `cross_file.py:134-141`.
`propose`, `validate` and `install_plan` all use it (install passes
`index.graph` where it builds a fresh `dep_graph_from_store` today). That
takes one planned wave from 2 graph builds to 1, and adds only one parse for
signatures. `DepGraph` is treated as read-only. A test asserts that the cached
graph equals a fresh build after a commit changes the generation.
`index_build_ms` goes on the first `PLANNER_CALL`.

### Implementation plan

Each step ends with `pytest -q`, `mypy --strict mak cli` and
`ruff check mak cli tests` green. Steps 7.1–7.3 change no prompt and no
behaviour; they can be merged or reviewed on their own.

**Module map (new files):** `mak/planner/telemetry.py` (`PlannerCall`,
`PlanningSummary`), `mak/planner/inventory.py` (`InventoryView`, renderers,
collapse), `mak/planner/retrieval.py` (`Retriever`, `LexicalRetriever`),
`mak/planner/expansion.py` (`parse_reply`, `ExpandRequest`, the round loop's
prompt assembly), `mak/planner/callers.py` (`find_missing_callers`,
`propose_caller_tasks`, `drop_tasks`), and `tests/planner/synthetic_repo.py`
(the generator). `planner.py` is already 745 lines: it keeps `Planner`,
`parse_plan` and the prompts, and delegates everything new.

- **7.1 Instrumentation and usage normalization (D7.1).**
  - Add `PlannerCall`, `PlanningSummary` and `EventType.PLANNER_CALL`.
  - Add `Planner.plan(user_task, node_inventory, *, view=None, observer=None)
    -> PlanOutcome` (plan, strategy used, `seen_files`, calls, summary).
    `decompose(...)` becomes a wrapper returning `outcome.plan`, so existing
    tests, the benchmark and `outline` keep their API.
  - Normalize usage per backend (table in D7.1), with a fake-usage test per
    backend.
  - Carry the summary: `PlanPreparer.take_planning_summary()` →
    `WaveState.start(planning=...)` → `plan_metrics`.
- **7.2 Synthetic repositories.** `tests/planner/synthetic_repo.py`:
  `build_synthetic_store(tmp_path, files=N, seed=0)`.
  - It writes real `.py` files (packages 3 levels deep; ~12 nodes per file:
    functions, a class with methods, a header), with **real cross-file calls**
    through imports, so `DepGraph` and `referrers` have edges.
  - It ingests them into a `NodeStore`.
  - Sizes 10 / 100 / 1,000 files. The 1,000-file store is built once per
    session (a `scope="session"` fixture).
- **7.3 Graph index and reverse index (D7.9, the reverse index of D7.6).**
  - Add `referrers()` and `PlanningIndex` in `PlanPreparer`.
  - Switch `validate` and `install_plan` to the cached graph.
  - Differential test: cached vs fresh graph across a commit.
- **7.4 Signatures and `InventoryView` (D7.2, D7.8).**
  - `api_digest.node_signature`, with tests for: async, decorated, method
    fragment, class shell (`_parse_lenient`), defaults elided, 160-character
    cap, unparseable → `None` (line rendered without a shape).
  - `InventoryView` with **golden renderings** (checked-in text) for a small
    fixture store: tree, collapsed tree, level-1 file, large-file truncation,
    and full view.
  - Determinism test: two builds, byte-equal output.
- **7.5 Prompt parts and caching (D7.7).**
  - `CachingPlannerLLM` and `complete_parts` for Anthropic, OpenAI, Gemini and
    Ollama.
  - Anthropic block construction and the `prompt_cache: false` path.
  - Ollama's stable `num_ctx`.
  - `_complete_with_retries` accepts `(stable, volatile)` and appends the
    retry note to `volatile` only.
  - Tests use fake clients that record the request body. No network.
- **7.6 Expansion protocol and strategies (D7.3, D7.4).**
  - `parse_reply` and the round loop.
  - `full` / `retrieval` / `auto`.
  - "Plan now" closure, cumulative budget, not-found / already-shown handling.
  - `verify` round.
  - `PlanSemantics.seen_files` and the `unseen_target` finding in
    `validation.py`.
- **7.7 Seeding (D7.5).** `LexicalRetriever`: term extraction, IDF,
  neighbourhood, shares. Table-driven tests on the synthetic repo: a task naming
  `invoice_total` seeds `billing/invoice.py` first; a task with only stop-words
  seeds nothing.
- **7.8 Caller completion (D7.6).**
  - `find_missing_callers` wired into `validate_plan` (findings
    `missing_caller`, `undeclared_api_callers`).
  - `propose_caller_tasks` wired into `PlanPreparer.propose` (findings
    `caller_tasks_capped` and the invariant-refusal finding).
  - `PlanProposal.proposed_task_ids`.
  - New prompt paragraph, chosen by `auto_caller_tasks`.
  - `mak/planner/review.py`: the new kinds join `_APPLIED_KINDS` where they
    change the plan.
- **7.9 Front ends.**
  - **`mak run`** (`review.py`): `render_plan` marks proposed tasks
    `[proposed by MAK]`, and `display_plan_for_review` gains
    `proposed: frozenset[str]` and a **`[d]rop MAK-proposed tasks`** choice
    (only shown when there are proposed tasks). The choice re-renders the plan
    and asks again.
  - **App.** `plan_in_thread` returns the `PlanProposal`. `show_plan` marks
    proposed tasks, and prints one summary line ("MAK added 3 caller-update
    tasks for 2 signature changes") plus the count of advisory findings.
    `_confirm_plan` accepts **`y`** (run all), **`o`** (run without MAK-proposed
    tasks) and N when there are proposed tasks; otherwise it stays y/N as
    today.
  - `core.py` changes are limited to passing `proposed` and
    `take_planning_summary()`; check `tests/test_module_budgets.py`.
- **7.10 Config (D7.3–D7.7).** `PlannerConfig` fields and `_parse_planner`
  validation:

  | Key | Default | Range |
  |---|---|---|
  | `strategy` | `auto` | `auto` \| `oneshot` \| `outline` \| `full` \| `retrieval` |
  | `inventory_token_budget` | 12000 | int, 2,000–200,000 |
  | `max_expansions` | 3 | int, 0–10 (0 = seeds + tree, one round, then plan) |
  | `auto_caller_tasks` | true | bool |
  | `max_caller_tasks` | 25 | int, 0–200 |
  | `prompt_cache` | true | bool |

  Also: wiring in `mak/application/session.py:107-121`, commented entries in
  `mak/config.yaml`, `mak/examples`, and `tests/test_example_configs.py`.
- **7.11 Offline planner-input benchmark (replaces the original Template 4
  hook).**
  - `benchmark/tools/planner_input.py` runs MAK's real `Planner` with a
    **recording fake LLM** that returns a fixed small plan (or one scripted
    expansion, then a plan). No model calls, no cost.
  - Inputs: MAK's own `mak/`, the four `benchmark/project_template*`
    directories, and synthetic 10 / 100 / 1,000.
  - For each input and each strategy (`oneshot`, `auto`) it reports: strategy
    chosen, first-call `prompt_chars` / estimated tokens, inventory tokens,
    rounds, `stable_chars` share, collapsed dirs, and index build time.
  - Output: a JSON file under `benchmark/results/` plus a Markdown table for
    the documenting step. A smoke test runs it on synthetic-10 in CI.
  - Quality with real models (plan correctness, caller coverage, cascade
    waves) is measured by Wave 33's `mak-e2e` arm, not here.
- **7.12 Gates and doc hand-off.**
  - `python -m tests.golden.golden compare` must pass *without* re-recording
    (no golden scenario calls the planner). If one does change, stop and
    explain before re-recording.
  - For the documenting step, list what changed: CONTRIBUTING §9 (planner:
    strategies, view, expansion, callers, caching), §11 (propose → install
    carries the planning summary; one graph per generation), §12 (config
    keys), §14 (app review choices), §1 (`PLANNER_CALL`); README (the new
    default strategy, if it is described there); `benchmark/README.md` (the
    planner-input table).

### Required test matrix

| # | Case | Expected |
|---|---|---|
| 1 | level-1 view ≤ budget, `auto` | `full`; one call; every file at level 1; no tree, no expand instructions |
| 2 | level-1 view > budget, `auto` | `retrieval`; first-call inventory ≤ budget for synthetic 100 and 1,000 |
| 3 | `oneshot` named explicitly | prompt byte-identical to today's `_build_prompt` output (golden string) |
| 4 | `outline` named explicitly | unchanged behaviour; existing outline tests pass untouched |
| 5 | planner expands twice, then plans | 3 rounds; 3 `PLANNER_CALL` events (`expand`, `expand`, `plan`); plan validated |
| 6 | expansions reach `max_expansions` | final prompt says "plan now"; a further expand reply is retried with the closure note; never a 5th round |
| 7 | expand an unknown / already-shown / non-`.py` path | reported in the next prompt; counts as a round; no retry consumed |
| 8 | expand a directory | its subtree one level deeper at level 0, not level 1 for every file |
| 9 | expansions exceed the budget | as many as fit, in request order; the rest listed "not expanded"; counted in `PLANNER_CALL` |
| 10 | plan targets a non-existent id in an unseen existing file, rounds left | one `verify` round showing that file; corrected plan accepted |
| 11 | same, no rounds left | `unseen_target` finding (or a correction); never the silent "new symbol" path |
| 12 | exact existing id in an unseen file | accepted, no finding |
| 13 | reply containing both `expand` and a plan | `ValueError` → normal retry note |
| 14 | tree larger than its share | breadth-first collapse; `[collapsed]` lines; `collapsed_dirs` > 0 |
| 15 | a file larger than 25% of the budget | truncated with "k more nodes" line; `symbols_truncated` > 0 |
| 16 | task naming a rare symbol | its file seeded first; 1-hop neighbours after; deterministic order |
| 17 | task of stop-words only | no seeds; tree only |
| 18 | `changes_api: true` on a function with 3 graph callers in 2 files, plan covers 1 | 2 `missing_caller` findings; 1 proposed task (the uncovered file), `depends_on` the changer, `changes_api: false` |
| 19 | two changing tasks share a caller node | one proposed task for that file, depending on both |
| 20 | `changes_api` undeclared on a function with uncovered callers | one `undeclared_api_callers` finding; no proposed task |
| 21 | class target with `changes_api: true` | findings only |
| 22 | caller file is a whole-file target of another task | covered; nothing proposed |
| 23 | proposed task would break a `parse_plan` invariant | not added; finding names the rule |
| 24 | more uncovered caller files than `max_caller_tasks` | cap respected; `caller_tasks_capped` finding |
| 25 | `auto_caller_tasks: false` | findings only; prompt carries the "add callers yourself" paragraph |
| 26 | reviewer drops proposed tasks (`d` in `mak run`, `o` in the app) | plan installed without them; `install_plan` re-validation logs `missing_caller`, does not re-add |
| 27 | retry after a malformed plan | every stable block byte-identical to the first attempt; the note only in the volatile part |
| 28 | expansion round N+1 | stable blocks of round N are a byte prefix of round N+1's |
| 29 | Anthropic backend | `cache_control` on S1 and the last stable block (≤ 4 breakpoints); none when `prompt_cache: false` |
| 30 | Anthropic usage with cache read/write | `input_tokens` = uncached + read + write; `cached_input_tokens` = read |
| 31 | OpenAI / Gemini usage with cached subset | `input_tokens` unchanged; cached subset read from the nested / Gemini field |
| 32 | Ollama over 3 rounds | the same `num_ctx` on every call |
| 33 | stub LLM with only `complete` | works under every strategy (concatenated parts) |
| 34 | every call, including a run ending in `PlannerFailedError` | one `PLANNER_CALL` per call with sizes and usage; no prompt text in the event |
| 35 | `propose_plan` → `install_plan` → `run` | `SessionResult.metrics` carries `planner_*`; a following cascade wave carries zeros |
| 36 | store generation unchanged between propose and install | one graph build in total; cached graph == fresh build after a commit |
| 37 | two builds of the same view | byte-identical renderings |
| 38 | signature with a string default | rendered `=...`; the literal is absent from the prompt |
| 39 | config: bad strategy / budget below 2,000 / negative expansions | `ConfigError` naming the key |

### Acceptance criteria

- **Flat growth.** With `auto`, the first-call inventory section for synthetic
  10 / 100 / 1,000-file repositories is ≤ `inventory_token_budget`. Above the
  threshold, the first-call prompt size varies by less than 10% between 100
  and 1,000 files. The inventory section never exceeds the budget in any round.
- **MAK's own tree** (offline benchmark, 7.11): the first retrieval call is at
  most **50%** of today's `oneshot` prompt (~26 K tokens), and every call's
  inventory section is ≤ 12,000 estimated tokens.
- **Nothing silent.** Every collapsed directory, truncated file, unexpanded
  request and unseen target shows up both in the prompt or review *and* in a
  logged count or finding.
- **Callers.** For every `function` / `method` target of a task declaring
  `changes_api: true`, every graph-visible caller is covered by a task or by a
  `missing_caller` finding. A reviewer can remove proposed tasks in both front
  ends, and they are not re-added.
- **Caching.** Retries and rounds reuse a byte-stable prefix. The Anthropic
  request carries the breakpoints. Cached tokens are counted in
  `input_tokens`, so `session.max_total_tokens` never under-counts.
- **Compatibility.** `oneshot` prompts are byte-identical to today's. Goldens
  pass without re-recording. `Planner.decompose` keeps its signature. A
  `PlannerLLM` with only `complete` still works.
- **Budgets.** `mak/session/core.py` stays ≤ 600 lines. Every new module and
  function follows AGENTS.md (typed signatures, docstrings, no function over
  ~40 lines without a reason).
- The offline planner-input benchmark runs in CI on synthetic-10, and its full
  table is recorded for the documenting step.

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| Retrieval hides the file the plan needed, and the plan is worse than today's | seeding + expansion + `verify` round + `unseen_target` finding; `oneshot` remains one config line away; Wave 33 measures plan quality with real models |
| Proposed caller tasks flood small plans | only declared `changes_api: true` function/method targets; one task per caller file; `max_caller_tasks`; one-key opt-out; removable at review |
| Signatures leak literals to the provider | defaults elided; no bodies or docstrings; no class attribute values (the same exposure as today's ids, plus parameter names and annotations) |
| Compact `::kind::name` suffixes cause more id errors | existing grounding tiers correct kind and `Class.` slips; the golden renderings (7.4) pin the format; switching back to full ids is a renderer constant |
| Extra rounds cost more than the flat listing on mid-size repos | `auto` uses `full` whenever everything fits; caching makes later rounds mostly cache reads; D7.1 metrics make this measurable |
| `core.py` line budget | all logic in `PlanPreparer`; `core.py` only forwards |

### Deliberately out of scope

- Embedding-based retrieval (a later `Retriever`).
- Letting the planner request node **source**; it sees shapes only. That is a
  tool-like capability and belongs with Wave 29.
- Reworking `outline` onto the view. It stays as it is and may be deprecated
  once D7.1's numbers show `retrieval` is better.
- Resolving `self.method()` and instance calls in `DepGraph`. That improves
  caller coverage, but changes validation edges and needs its own
  differential tests.
- Caller-task proposals for class API changes (constructor call sites).
- Persisting the inventory index across sessions (belongs with Wave 31's
  SQLite store).
- A template bypass for fixed task shapes (original 7.4, optional). Revisit
  after Wave 33 shows which shapes recur.
- Changing agent bundle budgets (CONTRIBUTING §3.3). Coordinate, don't merge.
- Caching for agent adapters' prompts.

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
