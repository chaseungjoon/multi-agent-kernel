# OPUS_REPORT — Architectural Review of Multi Agent Kernel (MAK)

| | |
|---|---|
| **Reviewer** | Claude Opus 5.5 |
| **Date** | 2026-09-23 |
| **Version reviewed** | 0.9.2b (`main` @ `2d89f69`) |
| **Scope** | Architecture and structure. Beta-stage incompleteness is deliberately **not** penalised. |

**How this review was done.** I read the README and CONTRIBUTING (Part I; §3 node
store; §4 locks; §5 conflict detector; §7 adapters; §8 planner; §12 CLI; Known
limitations; the benchmark and contention-study sections). I then read the code
that carries the architecture: `mak/session.py`, `mak/scheduler/scheduler.py`,
`mak/node_store/{ingestion,store,transaction,reconstruction}.py`,
`mak/planner/{planner,validation}.py`, `mak/agent_runner/protocol.py`, the CLI
adapters, `mak/bootstrap.py`, `mak/__main__.py`, `cli/runner.py`, and the
benchmark's MAK runner (`benchmark/harness/mak_runner.py`). I also took
measurements, listed below. I did **not** run live, paid model sessions, so
behaviour claims about real models come from the code and the project's own
recorded results.

**Measurements taken for this report**

| Measurement | Value |
|---|---|
| Kernel (`mak/`) | ~29,200 lines of Python |
| Interactive app (`cli/`) | ~5,800 lines |
| Tests (`tests/`) | ~27,700 lines; **2,432 pass / 4 fail** in ~56 s |
| The 4 failures | 3 × `TestIterSourceFiles` (documented as long-standing) + 1 test that reads the developer's real `~/.config/mak` model cache (a test-isolation leak) |
| `mak/session.py` | **4,617 lines, ~180 methods**, ~60 instance attributes |
| Docs | CONTRIBUTING 5,826 lines, PLANS 1,485, TASKS 803, README 395 |
| Planner inventory for MAK's *own* code | 146 files → **1,829 nodes → ~104 K chars ≈ 26 K tokens**, IDs only |

---

## 1) Overview

### What MAK is

MAK is a **transactional coordination kernel for concurrent LLM coding agents**.
It works on one working directory with no branches or worktrees. MAK parses the
codebase into position-independent AST nodes (`file::kind::qualified_name`) and
keeps them in a versioned **node store**, which is treated as the source of truth.
It then:

1. asks one LLM (the **planner**) to break a task into a DAG of subtasks, each of
   which declares its write targets and read context up front;
2. grounds and repairs that plan deterministically against a static dependency
   graph;
3. schedules subtasks with **atomic, all-or-nothing lock pre-allocation**
   (conservative two-phase locking) over a reader/writer/intent lock table;
4. sends each subtask to an agent as a **single-shot "fragment transform"**:
   node sources go in, rewritten node sources come out;
5. runs every result through a layered validation pipeline, commits it in a
   **real transaction** (store snapshot + file journal + commit point +
   restart recovery), reconstructs the affected files, and records a Git audit
   commit;
6. re-checks the wave's result for cross-module and semantic defects, and offers
   the fixes as a new, reviewable wave.

### The three bets MAK makes

MAK is really three coupled architectural bets. Each should be judged on its
own, because they succeed to very different degrees.

| # | Bet | How it is holding up |
|---|---|---|
| A | **Coordinate at scheduling time, at AST-node granularity, over shared state**, instead of isolating in branches and reconciling at merge time. | **Strong.** The mechanics (locks, transactions, validation, recovery, semantic checks) are database-grade and the best part of the project. |
| B | **Agents are pure, single-shot fragment transforms.** No tools, no exploration, no test runs. The kernel owns every write. | **Weakest bet.** It is what makes A tractable, but it caps agent quality below what current agentic tools achieve, and it pushes all foresight onto the planner. |
| C | **Confine the LLM to the planner; everything downstream is deterministic.** | **Half right.** Keeping the downstream deterministic is excellent. But the planner has to predict exact write sets from a flat list of node IDs, which is the system's main weak point for scaling and quality. |

### Verdict on "MAK will be a net positive in certain areas" (your ~70%)

I **agree for a narrow, well-defined niche** and would be **less optimistic for
general-purpose use**. My honest estimate is **~65% for the niche as the code
stands today**. I'd expect it to reach **75–80% if the top three suggestions
(§6: S1–S3) land**. For general coding work I'd put it at **~35–40%**.

The deciding evidence comes from the project's own research:

- The Wave 23 contention study found **zero** change-versus-change textual
  conflicts across **124,473** concurrent human PR pairs. All **5,316** pairs
  that touched a shared node merged cleanly. Same-file overlap was only
  0.13–2.54%. For *human-style* parallelism, merge conflicts are **not** the
  bottleneck. So the "no merge conflicts" argument alone does not justify MAK.
- MAK's case therefore rests on **agent fan-out**: one task split N ways,
  where all the pieces land in the same area of the code by construction. That
  concentrates contention far more than independent human PRs do. MAK also has
  something worktree systems lack entirely: **deterministic, commit-time
  semantic checking** (stale reads, signature drift, broken imports, registry
  collisions, missing required fields).

**Where MAK is likely net positive (today or after modest work):**

- **Fan-out feature work in registry-heavy Python code.** For example: add N
  endpoints, handlers, integrations or event listeners that all register into
  shared tables. Think Django apps, Home-Assistant-style integrations, and
  plugin systems. This is exactly what templates 3 and 4 model, and where the
  recorded results favour MAK on tokens, accuracy and wall-clock.
- **An API change plus all of its callers.** A signature change followed by N
  function-scoped caller updates, with MAK's signature, cross-module and
  stale-read checks as the safety net.
- **Mechanical migrations and codemod-style refactors** spread across many
  functions, once the planner can scale (S2).
- **Test-generation fan-out.** Each test function is an independent node, and
  the shared parts are fixtures and conftest registries.
- **Greenfield scaffolding split one file per task.**

**Where MAK is unlikely to be net positive:**

- Exploratory debugging and "figure out why this fails" work, which needs tool
  loops and test runs (bet B).
- Tasks one agent can do alone: MAK's planning, locking and validation overhead
  doesn't pay for itself.
- Changes dominated by non-Python files (configs, migrations, docs, CI, lock
  files). The contention study shows these saturate first, and MAK cannot lock
  them finer than whole files.
- **Large monorepos, until the planner can retrieve instead of listing
  everything.** This is ironic, because that is where concurrency matters most.
- Projects that don't use `ruff format`, because every file MAK writes is
  reformatted (B5).

---

## 2) Ratings (out of 10)

| Category | Score | One-line rationale |
|---|:-:|---|
| **Core concept & originality** | **8** | "OS/DB concurrency control applied to code" is a genuinely distinct, well-argued alternative to worktrees. |
| **Concurrency control & correctness engineering** | **9** | Atomic pre-allocation, one policy function, commit-time lease re-validation, a single-writer commit coordinator. Textbook-correct. |
| **Failure safety, durability & recovery** | **9** | Real transactions with a journal and commit point, atomic writes, a `flock` project lease, crash recovery, containment checks. |
| **Semantic conflict handling** | **8** | Read-set stale detection, interface/body locks, registrar key locks, repair obligations, loop fingerprints. Rare in this space. |
| **Planner design & scalability** | **4** | Gets a flat list of every node ID (no signatures, no call graph) and must predict exact write sets. Input grows linearly with the repo. |
| **Agent execution model** | **4.5** | Single-shot, no tools, no test feedback. Safe and deterministic, but it caps quality below current agentic tools. |
| **Modularity & code structure** | **5** | Subsystem packages are clean and injected through Protocols, but `Session` is a 4,600-line god object, and the two front ends duplicate logic. |
| **Scalability (repo size, I/O)** | **3.5** | Planner inventory ≈ 26 K tokens for 146 files. `metadata.json` is fully rewritten on every commit. |
| **Language / ecosystem generality** | **3** | Python-only by construction (`compile()`, ruff, Python node kinds). Other files are whole-file or excluded. |
| **Evaluation & evidence quality** | **6** | The contention study is excellent (9). The headline benchmarks use oracle plans and a simulated baseline (5). |
| **Test suite & quality gates** | **8.5** | 2.4 K fast tests, mypy strict, ruff, injectable seams everywhere. Docked for permanently red tests and one isolation leak. |
| **Documentation** | **7** | Remarkably thorough "why" writing, but 5.8 K lines of CONTRIBUTING mixes reference and history, and it has drifted (it still says "2035 tests"). |
| **Usability / developer experience** | **6** | A polished CLI app with modes, endpoints, local runtimes and plan review. Undermined by forced formatting and commits on the user's branch. |
| **Security & privacy posture** | **8** | Keys referenced by env-var name, no cross-host key forwarding, path containment, a Docker sandbox for CLI agents. |
| **Overall viability — targeted niche** | **7** | |
| **Overall viability — general-purpose coding** | **4.5** | |

---

## 3) Good design choices

### G1. Conflicts are resolved at scheduling time, with atomic lock pre-allocation
`Scheduler.tick` (`mak/scheduler/scheduler.py:133`) acquires **all** of a task's
locks in a single `try_acquire_all`, or none. A waiting task therefore never holds
a lock, which makes the wait-for graph acyclic by construction. **Deadlock freedom
is structural, not detected.** This is conservative two-phase locking, the right
choice when write sets are declared up front. The `DeadlockDetector` is kept as
honest defence in depth, and the docs say so.

### G2. Node identity is position-independent and lossless
- IDs are based on qualified names, not line numbers, so adding a function never
  invalidates another agent's lock.
- Ingestion **tiles every line exactly once** and keeps raw text (comments,
  decorators). Reconstruction is concatenation, not `ast.unparse`, so no comments
  are lost.
- `#n` suffixes keep `@overload` stubs and conditional definitions as separate
  nodes instead of silently collapsing them (`ingestion.py:39`).
- Whole-file nodes are a clean fallback for greenfield files and full rewrites,
  with explicit supersede semantics.

### G3. A single-writer commit coordinator over a concurrent agent pool
Agent calls run on a `ThreadPoolExecutor`. Enrichment happens on the calling
thread while the write locks are held, and **all validation and commits happen
serially on the session thread**, in deterministic topological-then-ID order
(`_process_batch`, `session.py:1550`). Results that finish together are
validated as one **batch**, with earlier commits in the batch acting as "peers".
That is what lets cross-agent checks fire, and it blames a genuine conflict on
the later task. This combination (concurrent I/O, serialized mutation) is the
simplest correct design for a Python kernel. It avoids an entire class of races
in the node store.

### G4. An ordered validation pipeline with commit-time lease re-validation
`_validate_and_commit` (`session.py:2051`) runs these checks in order before
anything becomes durable:

providers committed → registrar reconciliation → **read set still current** →
structural conflict detection against batch peers → declared contracts hold →
interface changes were granted → **prospective whole-file reconstruction
compiles** → prospective semantic checks → **still own every write lock** (a
lease may have expired during a long model call).

The last check matters most: MAK never commits through a stolen lock.

### G5. Real transactions, not "write and hope"
- A store snapshot plus a file journal with an explicit commit point
  (`transaction.py`). A metadata save is the single durable commit point.
- Every file of a multi-file change is rendered **before** any file is written,
  so a failure on the second file can't leave the first one ahead of the store.
- Atomic writes (`write_text_atomic`) everywhere state is persisted.
- Restart recovery re-runs the audit step idempotently.
- Git audit commits use a **private index**, so the user's staged work is never
  swept in.

This is well beyond what comparable agent orchestrators do.

### G6. One lock-policy function, three consumers
`lock_policy.lock_requests(task, policy)` is the only thing that decides what a
task locks. The scheduler (acquire), the session (commit re-validation) and the
deadlock watchdog (wait graph) all call it, so they **cannot disagree**. With
every flag off it reproduces the legacy lock set exactly, so the flags double as
clean ablation switches for experiments. That is good research engineering.

### G7. Database hierarchy-locking ideas adapted to code
- **Interface/body split** (`#api` resources): a body-only edit runs alongside
  the callers of the function it edits.
- **Intention locks**: many fragment writers can share a file, while a
  whole-file rewrite excludes them all.
- **Key-level registrar locks**: appends to a keyed registration table commute.
  Registrars are detected **structurally** (a flat list of
  `callee("literal", …)` calls), never by name. Unkeyed, ordered tables
  correctly keep a plain node lock.

These are exactly the right abstractions for the "shared table everyone touches"
hot spot that dominates real fan-out work.

### G8. Optimistic validation layered on pessimistic locks
Every dispatched bundle records a **read set** (node, version, digest). At commit,
`_reads_are_current` detects write skew: a context node changed underneath the
task. The kernel then re-dispatches, accepts the result if the API is stable, or
rejects it, depending on the configured policy. This is essentially serializable
snapshot isolation for code, catching a class of bug (shape 1 in the semantic
corpus) that no worktree system can see.

### G9. The LLM plan is grounded deterministically, never trusted
`planner/validation.py`:
- **adds** missing dependency edges from the real `DepGraph`;
- **fuzzy-corrects** hallucinated node IDs only when there is exactly one
  confident match, and otherwise flags them;
- **flags but never removes** edges that look spurious, because the LLM may know
  an ordering the AST cannot see;
- **keeps** context nodes that another task will create, and orders the reader
  after the creator.

Every change becomes a `PlanFinding` shown to the human reviewer. That is the
right way to use an LLM's output: as a proposal that deterministic code
verifies.

### G10. Outcomes are reported truthfully
- An empty result is a **failure** unless the agent positively asserts
  `no_changes_required`. A truncated reply can never be counted as a no-op.
- A `retry_note` tells the model why its last attempt failed, so a retry is not
  a byte-identical repeat.
- Bundles with nothing in them are refused before a model call is spent.
- Every failure reason is kept (`_failure_history`), not just the last one.
- A token ceiling, plus reporting of which tasks were stranded when it stopped
  the run.

This "never let a symptom masquerade as success" discipline shows up across the
whole codebase and is a major strength.

### G11. The kernel owns repair obligations, and cascade loops can stop
Post-wave fix-ups carry **postconditions the planner cannot weaken**. They are
checked against the *prospective* repository **before** the transaction commits,
and "family" identities keep a guessed name swapped at the same import site from
counting as progress. The cascade loop fingerprints state, so it stops as
`stalled` or `oscillating` instead of asking a human forever. Very few
agent systems think about their own non-convergence.

### G12. The conflict detector prefers precision over recall
A false positive costs one bounded retry, while a false negative is caught later
by the tests. So the detector skips anything it cannot prove (splats, untyped
receivers, ambiguous imports), and uses **strict** import resolution when its
findings would generate repair tasks. This is the correct asymmetry for a gate
that sits in front of automated work.

### G13. Dependency injection and a fast, strict test suite
The scheduler and session depend on `Protocol`s (`LockManager`,
`AdapterRegistryLike`, `_Assigner`, …). Adapters, gate runners, model clients and
host probes are all injectable, so 2.4 K tests run in under a minute with no
network, and `mypy --strict` plus `ruff` cover both `mak/` and `cli/`. The
integration gate (`tests/test_concurrency_integration.py`) exercises the real
concurrent pipeline.

### G14. A careful security and privacy posture
- One owner per project through `flock`, so the OS releases the lock however the
  owner dies.
- Every node-ID→path conversion is containment-checked.
- Endpoints store the **name** of the env var holding a key, never the key.
- A real OpenAI key is never forwarded to a third-party base URL unless it was
  explicitly named.
- A Docker sandbox for CLI agents.

### G15. Provider and endpoint abstraction
A resolved-endpoint layer, capability negotiation that steps down one rung on a
verified rejection and remembers what worked, explicit transports, and a
self-refreshing model catalog that **keeps the cached list when a fetch fails**.
Well-factored, even if its size is a scope question (Q8).

### G16. The human stays in the loop, and human work is respected
Plan review with findings. At startup, reconciliation **adopts** human edits to
the working tree instead of discarding them, with an opt-in `"conflict"` mode.

### G17. The contention study is excellent research
It uses base overlap rather than lifetime overlap, forward-port merges against a
shared base (rejecting the biased `git merge-tree` shortcut, with the bias
measured at 10–30%), a mapper validated against an independent `ast` walk, a
real-merge audit, an explicit failure taxonomy, and **negative results reported
as such**. It re-uses the kernel's own ingestion code read-only, so "node" means
exactly what MAK would lock. This is the most trustworthy evidence in the repo,
and it is what exposes the key strategic insight (Q1).

---

## 4) Bad design choices

### B1. `Session` is a god object
`mak/session.py` is **4,617 lines with ~180 methods and ~60 mutable instance
attributes**. It covers orchestration, dispatch enrichment (siblings, callers,
dependency outputs, a symbol index), the whole commit pipeline, stale-read
policy, registrar merging, interface enforcement, parked commits, no-op
adjudication, cascade detection, post-wave gates, cross-module repair, recovery
and teardown.

- It contradicts the project's own `AGENTS.md` ("each module lives in its own
  file", "functions ~40 lines").
- About 25 attributes are per-wave state (`_wave_committed`,
  `_wave_file_before`, `_wave_commit_log`, `_read_sets`, `_granted`,
  `_parked`, …) that must be reset correctly between waves. That is a
  bug-prone implicit contract, and the kind of state that drifts silently.
- It has become the place every new wave adds its logic, which accelerates
  the growth.

The individual subsystems are clean. **The architecture's weakest seam is where
they meet.**

### B2. The planner sees everything, but only as names
`Session.plan` passes `self._node_store.list_nodes()` (**the entire inventory**)
to the planner as bare IDs (`planner.py:600`), with no signatures, no source,
and no call graph.

- **Scaling:** MAK's own 146 files produce ≈ 26 K tokens of IDs alone. A
  1 M-line repo would produce several hundred thousand tokens, past practical
  context limits. That wall hits exactly where MAK's concurrency argument is
  strongest.
- **Quality:** the prompt's "CASCADE PREVENTION" section asks the LLM to *"search
  the inventory for any node whose name suggests it calls a symbol you are
  changing"*. The kernel **already has the real call graph** (`DepGraph`) and
  uses it only *after* planning, to repair edges. The model is guessing what the
  kernel knows.
- The optional outline mode helps, but it is not the default and still starts
  from names.

### B3. Write sets are fixed before the agent writes any code
Everything the agent returns outside its lock grant is **dropped**
(`protocol.map_returned_sources`). But real edits routinely need things the
planner didn't predict:

- **an import.** `module_header` is a *single node per file*, so every task that
  needs an import in a file must write-lock that header and serialize on it. If
  the planner forgot to list it, the import is dropped and the task fails or
  retries. The planner prompt never mentions headers or imports.
- **a new helper function or constant** next to the target.
- **a small fix in a caller** the planner didn't foresee.

The predictable result is that planners learn to over-claim (whole files, extra
headers), which destroys the very parallelism MAK exists to provide. Or they
under-claim, which burns retries. Conservative 2PL is the right lock protocol,
but it needs an **escalation path** (S3).

### B4. Agents are single-shot, tool-less transforms
Each dispatch is one completion over a curated bundle. The CLI adapters run
`claude -p` in print mode through the same bridge. Agents cannot:

- read code the enrichment heuristics didn't include;
- run the tests, or even import the module they are editing;
- try something, observe the result, and iterate.

Most of the recent gains in coding agents come from exactly those loops. MAK
replaces exploration with heuristics (same-file siblings, callers found by
scanning for names, dependency outputs) and replaces feedback with retry notes.
This keeps the kernel deterministic, but it **caps per-task quality** below
what the same model achieves in an agentic tool, and it hands all foresight to
the planner (B2, B3).

### B5. Every file MAK writes is reformatted with `ruff`
`render_file(fragments, use_ruff=True)` (`reconstruction.py:72`) is called from
the commit path with no option (`transaction.py:75`), and nothing in
`config.yaml` controls it. As a result:

- in any project not formatted with ruff (Black with different settings, yapf,
  or hand-formatted legacy code), **editing one function reformats the whole
  file**, which produces noisy diffs and churn;
- the **store keeps the unformatted fragments while disk holds the formatted
  file**. The two quietly diverge in formatting, and agents are shown source
  that differs from what the user sees;
- every file on every commit costs a `ruff` subprocess.

### B6. Storage writes the whole JSON file on every change
`NodeStore._save_metadata` rewrites the **entire** `metadata.json`
(`indent=2`) after every commit (`store.py:327`). The lock table persists after
every mutation, and every node version is its own file. The cost is O(repo) per
commit and many small files on disk. That's fine for demos, but it won't hold up
on large repositories or long sessions. The contention study already uses
SQLite, which would give incremental writes and native transactions here too
(S6).

### B7. The two front ends duplicate logic and couple in the wrong direction
- Planner key resolution exists **twice**, with different precedence:
  `cli/runner.py::_resolve_planner_api_key` and
  `mak/__main__.py::_planner_api_key`. The 0.9.2b hotfix had to change both.
- The CLI app builds sessions by **importing the entry-point module**
  (`from mak.__main__ import build_session`, `cli/runner.py`). An `__main__`
  module is being used as a library API.
- Planner-route handling (endpoint vs. backend vs. base_url) is re-derived in the
  CLI's `build_session`, in `_apply_state_to_config`, and in `__main__`.

Each duplicated piece is a place where `mak run` and the app can behave
differently for the same configuration.

### B8. Global mutable state, against the project's own rules
`AGENTS.md` says "No global mutable state", yet:

- `cli/local.py` keeps module-level seams (`_discover_fn`, `_client_factory`,
  `_probe_host_fn`) that are reassigned through `global`;
- `cli/core/models.py` holds a process-wide `_REGISTRY` singleton.

Minor on its own, but it's the pattern that makes test isolation leak (see the
failing `test_cli_adapter` test, which reads the real user cache).

### B9. The scheduler has no fairness
`tick()` walks the ready queue in FIFO order and dispatches greedily with
all-or-nothing acquisition. There is no aging, priority or reservation. A task
that needs a **wide** lock (a whole-file or whole-class `WRITE`, which conflicts
with every `INTENT_WRITE` below it) can **starve** behind a steady stream of
narrow fragment writers. There is also no critical-path prioritization, which
matters for wall-clock time on deep DAGs.

### B10. Audit commits go onto the user's current branch
`git.auto_commit: true` by default writes one `[MAK]` commit per task onto
whatever branch is checked out. The README has to warn users to *"create a
separate branch for MAK to work on"*. A kernel this careful about state should
create and own its own branch or ref namespace (S9).

### B11. Human edits made during a run are overwritten without warning
External-edit detection (`_reconcile_work_dir`) runs **only in `initialize()`**
(`session.py:714`). During a run, `install_files` writes each file from the
store's view without comparing it to what MAK last wrote (the journal backup is
discarded on success). So if a person saves a file mid-run and MAK later commits
to that file, **the edit is lost with no warning**. The project lease stops a
second MAK process, but not an editor.

---

## 5) Questionable areas

### Q1. The premise versus the project's own evidence
The contention study (G17) shows that for human PRs, textual merge conflicts are
effectively **zero**, and shared-node edits merge cleanly **100%** of the time.
At k = 16 concurrent changes, 78.9–99.4% of windows have a *file-path* collision,
but these are dominated by **non-Python resources**: build/CI config, dependency
lists, docs, changelogs, registries. MAK cannot lock those at node level. So:

- "MAK avoids merge conflicts" is a weak selling point for human-like workloads.
- MAK's value has to come from **agent fan-out concentration** and
  **commit-time semantic checking**. Those are real, but the project's
  positioning (README, CONTRIBUTING "Why not Git worktrees?") still leads with
  merge conflicts.
- The study itself names the next concurrency frontier: **structured operations
  on append-oriented text files**. That isn't on the roadmap yet.

### Q2. The benchmarks don't test the riskiest components
`benchmark/harness/mak_runner.py` builds the MAK plan **by hand**:

- perfect `target_nodes`, `changes_api=False`, precomputed `registry_keys`;
- the registry line itself applied by a **deterministic helper**, so the model
  only writes one function body;
- the "traditional" side is a **simulated** worktree pipeline (sequential calls,
  modelled parallel time, one model call per resolution), not a real agentic
  tool running in worktrees with test loops;
- all workloads were written by the project, around registries, with one model
  family.

So the headline numbers measure *the kernel given an oracle plan and oracle lock
declarations*. That's a legitimate controlled experiment, but it doesn't
evaluate the planner (B2), write-set prediction (B3) or agent quality (B4),
which are the three places the design is most likely to fail in real use.
Template 4, which uses a real planner, is the exception and points in the right
direction.

### Q3. Python-only, and "whole file or nothing" for everything else
The planner is told to *leave out* non-Python work. Real features touch
migrations, settings files, YAML, JSON, templates, tests' data files and docs.
Those parts of a task are silently not done, or need a second tool.

### Q4. Granularity stops at top-level definitions and methods
Nested functions, closures and inner classes are not split. All top-level code
after the first `def` or `class` is one `module_body` node, and all imports are
one `module_header` node. A 500-line function is one lock. Class shells are not
parseable on their own (a documented limitation). The granularity is right for
typical library code, and too coarse at exactly the module-level hot spots
(headers, bodies) that fan-out tasks tend to share.

### Q5. Hung agent calls can't be interrupted
This is documented and accepted: Python can't kill a thread, so a hung SDK call
is bounded only by the SDK's own timeout. Running agents as subprocesses would
solve it and would also enable per-agent sandboxes and tool loops (S4).

### Q6. Cost and convergence are open-ended by default
`session.max_total_tokens` defaults to unlimited, and repairs are more LLM
waves. Loop fingerprints stop non-convergence, but not *expensive* convergence.
There is no cost estimate before dispatch.

### Q7. The optional LLM adjudicator reintroduces nondeterminism
`semantic.adjudicator` lets an LLM break ties on uncertain stale reads, inside
the path the architecture otherwise keeps deterministic (bet C). It is off by
default, which is right, but it's a principle exception that should stay
clearly fenced.

### Q8. The complexity budget
About 35 K lines of product code and 8.5 K lines of design documents, in beta,
with what looks like one human maintainer. A large share of the surface is
**peripheral** to the thesis:

- the model catalog with scheduled auto-refresh;
- provider wizards and OpenRouter capability negotiation;
- three CLI-agent bridges;
- local-runtime discovery and host management;
- a Docker sandbox;
- optional semantic gates.

Each is well built, but each is ongoing maintenance, and each one expands
`Session` and `config.py` (1,000 lines).

### Q9. Documentation drift and normalized red tests
- CONTRIBUTING's "Current status" still says **2,035 tests**; the suite has 2,436.
- Three `TestIterSourceFiles` failures have been "tracked, not fixed" since Wave
  20. A permanently red suite trains everyone to ignore failures.
- One more failure depends on the developer's real `~/.config/mak` content. The
  suite isn't hermetic.
- CONTRIBUTING mixes reference material with a long wave-by-wave history. It
  is hard to find the current contract for a subsystem without reading how it
  evolved.

### Q10. History lives in the code
Docstrings and comments carry a lot of "Wave N fixed X because Y". That is
valuable, but it also marks a pattern of **accreting special cases** (parked
commits, provider waits, no-op refusals, starved bundles, registrar upgrades,
interface holds…) instead of simplifying the underlying model. Several of those
special cases exist because of B3 (fixed write sets) and B4 (blind single-shot
agents).

### Q11. The tiered-AI development process
`AGENTS.md` describes a plan → execute → document pipeline across model tiers.
It clearly produces thorough, well-tested increments. The risk is architectural:
each wave is optimized locally, and nobody's job is to **refactor across waves**
(B1 is the symptom). A periodic "consolidation wave" with no new features would
help.

### Q12. Multi-language support will be a large refactor, not an add-on
The node-ID kinds, `compile()` gates, `ruff` formatting, `api_digest`, registrar
detection and signature checks are all Python-specific and spread across
subsystems. Nothing yet defines a `LanguageBackend` boundary (S11).

---

## 6) Suggestions

Ordered by expected impact on the viability of MAK's niche.

### S1. Break up `Session` (enables everything else)
- Pull per-wave state into a `WaveState` dataclass that is created fresh for each
  wave. That removes the implicit reset contract.
- Split the rest out into collaborators, each in its own module:
  - `DispatchEnricher` (siblings, callers, dependency outputs, symbol index);
  - `CommitPipeline`: the validation chain as an **ordered list of `CommitCheck`
    objects** with one interface (`check(ctx) -> Accept | Reject | Defer`), so
    new semantic checks become plug-ins rather than new `Session` methods;
  - `CascadeController`, `RecoveryManager`, `Teardown`.
- `Session` becomes a thin state machine of a few hundred lines.

### S2. Make the planner retrieval-based and graph-aware
- Replace the flat inventory with a **hierarchical view**: a file tree, then
  per-file symbol summaries with signatures (you already have
  `api_digest.public_api_digest`). Let the planner **ask to expand** specific
  files. The outline mode is halfway there; make it the default above a size
  threshold.
- **Give the planner the call graph.** When a plan targets X, put X's actual
  callers from `DepGraph` in the prompt (or add them automatically as proposed
  caller tasks) instead of asking the LLM to guess from names.
- Put a token budget on the inventory, with measured truncation.

### S3. Let write sets grow safely
- **Imports as a commutative merge.** Let agents return a separate `imports`
  field. The kernel merges imports into `module_header` the same way it already
  merges registrar appends (with `INTENT_WRITE`, and deduplication and conflict
  checks from `import_check`). This one change removes the hottest lock in
  typical fan-out.
- **Additive new symbols.** Let a task add new top-level functions or constants
  to a file under the file's intention lock without declaring them in advance
  (new IDs cannot conflict with existing ones, and name collisions are already
  checked).
- **A lock-escalation protocol.** When an agent needs a node it wasn't granted,
  it returns a `needs_locks` request. The kernel tries to acquire those locks
  atomically. On success it commits or re-dispatches with the wider grant; on
  failure it parks the result. Today that work is dropped.

### S4. Give agents tools inside the kernel's sandbox
Keep the principle that the kernel owns every write, but let agents *look* and
*test* before they return:

- read-only tools: `get_node`, `find_symbol`, `callers_of`, `search`;
- **run the tests against the prospective repository.** The Wave 26 machinery
  for obligations and prospective views already builds "the repo as it would be
  if this commit landed". Expose it as a scratch overlay (a temp dir or a
  process sandbox) where an agent can run `pytest -k …` before submitting.
- Run each agent in a **subprocess** (which also fixes Q5) with a turn budget.

This keeps MAK's transactional guarantees while recovering most of the quality
gap with agentic tools.

### S5. Make formatting a project decision
`reconstruction.formatter: none | ruff | black | "<command>"`, defaulting to
**`none`** (keep the original bytes around untouched fragments). If a formatter
is configured, format only the fragments that changed, or re-ingest the
formatted file so the store and disk stay identical.

### S6. Move persistent state to SQLite
Store node metadata, versions (or content-addressed blobs), the lock table,
`file_state` and the commit journal in one SQLite database in WAL mode. You get
incremental writes, real transactions (possibly replacing the hand-rolled
journal for store state), fast queries for the planner's retrieval (S2), and far
fewer files.

### S7. Unify the two front ends behind one application API
Create a `mak/app.py` (or extend `bootstrap`) with `build_session(RunRequest)`,
planner-route resolution and key resolution, and have **both** `mak run` and
`cli/` call it. `cli/` should never import `mak.__main__`. Add a parity test:
the same logical settings through both front ends must produce identical
`MakConfig`s.

### S8. Add fairness and priority to the scheduler
Add aging, or a reservation for tasks that have waited N ticks: once reserved,
no new narrow grant may be issued that would block it. Order ready tasks by
**critical-path length** so long chains start first. Run the deadlock watchdog
rarely, or as an assertion, since it cannot fire by construction.

### S9. Own the Git lifecycle
By default, run on a MAK-created branch (`mak/<session-id>`), or record audit
commits under `refs/mak/…`. Offer to squash or merge at the end, and drop the
README warning.

### S10. Detect edits made during a run
Before writing a file in `install_files`, compare its on-disk digest with
`file_state`. If they differ, apply the `on_external_edit` policy (adopt →
re-ingest and re-validate the commit; conflict → park and report) instead of
overwriting it.

### S11. Evaluate what can actually fail
- Run benchmarks **with MAK's own planner end to end** and report plan-quality
  metrics (hallucinated targets, missing locks, escalations, retries per task).
- Compare against a **real** baseline: current agentic CLIs running in worktrees
  with test loops, given the same task split.
- Add **real open-source tasks** (multi-file features and refactors in
  registry-heavy projects such as Django apps and Home-Assistant integrations),
  not only generated templates.
- Use several model families, and report the non-Python share of each task.

### S12. Define a `LanguageBackend` interface, and handle non-Python files
- Define one Protocol — `split(source) -> fragments`, `validate(file)`,
  `format(file)`, `api_fingerprint(node)`, `symbols(node)`, `registrars(node)` —
  and make Python its first implementation. Tree-sitter can come later.
- Following the study's own conclusion, add **structured append operations**
  for text resources (changelog entries, dependency lists, YAML/JSON arrays and
  maps) as commutative merges with key-level locks. That covers the files that
  saturate first.

### S13. Scope discipline
Freeze peripheral features (catalog refresh, provider wizardry, endpoint
negotiation beyond what exists) until S1–S4 land. Consider delegating provider
abstraction to an existing library, so the kernel's complexity budget goes to
the thesis.

### S14. Test and documentation hygiene
- Fix or `xfail` the three `TestIterSourceFiles` failures with a linked issue,
  and make CI gate on a green suite.
- Make tests hermetic: point `XDG_CONFIG_HOME` and `HOME` at a temp directory in
  a session-wide autouse fixture.
- Generate the "Current status" numbers, or remove them.
- Split CONTRIBUTING into **Reference** (the current contract per subsystem) and
  **History** (the wave log).
- Hold a periodic consolidation wave with no features, only refactoring.

### S15. Position MAK as a coordination layer, not an end-to-end agent
The strongest long-term product may be the **kernel as a library or MCP
server**: other agent frameworks (including agentic CLIs) *commit through* MAK
and get node locks, transactional commits, stale-read detection and semantic
checks, while bringing their own planning and tool loops. That plays to MAK's
proven strengths (bets A and C) and routes around its weakest (B). It also
makes the "net positive in certain areas" claim easy to test: any fan-out
workflow could adopt MAK's guarantees without adopting its agent model.

### S16. Safer defaults
- A default `session.max_total_tokens`, plus a pre-dispatch cost estimate shown
  at plan review.
- `require_clean_tree: true` when `auto_commit` is on.
- Show predicted lock contention (hot nodes, expected serialization) in plan
  review, so a human can spot an over-claiming plan before paying for it.

---

### Bottom line

MAK's **kernel** (locking, transactions, validation, recovery, semantic
checking) is excellent, and would hold up in a serious database codebase. Its
**edges** (a planner that must foresee everything from names, agents that
can't look or test, Python-only scope, forced formatting, whole-JSON storage)
are what currently limit its value.

The contention study says the thing to sell isn't "no merge conflicts". It's
**safe, validated fan-out of many small agent edits into shared hot spots**.
Aim MAK at that niche, fix B2, B3 and B4, and your 70% is justified. Without
those fixes, MAK stays an impressive kernel whose surrounding pieces limit it.
