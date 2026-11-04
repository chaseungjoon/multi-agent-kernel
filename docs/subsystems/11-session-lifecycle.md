# 11. Session lifecycle

> [Contributing guide](../../CONTRIBUTING.md) · ← [10. Git integration](10-git-integration.md) · [12. Configuration](12-configuration.md) →
>
> **Code:** `mak/session/`, `mak/cascade.py`, `mak/execution_result.py`, `mak/teardown.py` · **Tests:** `tests/test_session.py`, `tests/session/`, `tests/test_concurrency_integration.py`, `tests/test_crash_recovery.py`, `tests/test_cascade.py`

`mak/session/` is a package. `Session` (`core.py`) is a thin state machine —
`CREATED → INITIALIZED → PLANNED → RUNNING → {COMPLETED | FAILED | ABORTED}` —
over single-purpose collaborators, each in its own module and each built once
per session by `wiring.wire()`. A collaborator receives what it needs explicitly
— the store or a `StoreView`, the lock table, a config section, the session's
`EventLog`, and the current `WaveState` per call — and never the `Session`. The
subsystems are injected behind `Protocol`s, and `mak/session/__init__.py`
re-exports the public names (`Session`, `SessionResult`, `SessionState`,
`SubTaskProgress`, `PlanProposal`, `TestRunner`).

| Module | Collaborator | Owns |
|---|---|---|
| `core.py` | `Session` | lifecycle transitions, the run loop, the spend ceiling, token usage |
| `wave.py` | `WaveState` | every per-wave field (below) |
| `wiring.py` | `wire()` | building the collaborators |
| `types.py` | — | `SessionState`, `SessionResult`, `SubTaskProgress`, `PlanProposal`, the injected protocols |
| `events.py` | `EventLog`, `timed_phase` | session-stamped logging; `PHASE_SPAN` timing |
| `store_view.py`, `workspace.py` | `StoreView`, `Workspace` | read helpers over the store; the work, mak and journal dirs and the safe output path |
| `reconcile.py` | `WorkTreeReconciler` | `.makignore`, pruning, store ↔ tree reconciliation, the clean-tree and audit-repo preflight |
| `planning.py` | `PlanPreparer`, `PlanningIndex` | the planning index, proposing (planner call, `PLANNER_CALL` events, caller proposals), unsafe-target refusal, plan validation, agent assignment, the planning summary |
| `dispatch.py`, `cross_file.py`, `grants.py` | `DispatchEnricher`, `CrossFileIndex` | enrichment layers 0–5 ([§3.3](03-ast-pipeline.md#33-dispatch-bundle-enrichment)), read-set capture, the starvation guard, the grant each task holds |
| `concurrency.py` | `ConcurrentRunner` | fanning agent calls out to the thread pool |
| `batch.py` | `BatchProcessor` | the completion queue, deterministic batch order, settling a result, staging returned sources, draining in-flight work |
| `commit/` | `CommitPipeline`, the checks, `CommitApplier` | the commit path ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic) "Detection at commit") and the transaction |
| `parking.py` | `ParkedCommits` | defer, park, resume, releasing a cycle's victim |
| `outcomes.py`, `failures.py` | `RetryPolicy`, `NoopPolicy` | retries and retry notes, no-op acceptance, empty-result diagnosis, the failure log |
| `repair.py` | — | the whole-repository checks and repair-obligation predicates |
| `adjudication.py` | `AdjudicatorFence` | the optional LLM adjudicator ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)) |
| `post_wave.py`, `fixups.py` | `PostWaveAnalyzer`, `FixupBuilder` | wave-end defects, the cascade, the gates, fix-up tasks, state fingerprints |
| `results.py`, `usage.py` | — | `SessionResult` and plan metrics; token totals |
| `recovery.py` | `RecoveryManager` | journal recovery, `recover` |
| `finalize.py` | `Finalizer` | teardown and the push gate |
| `watchdog.py` | `LockWatchdog` | lease heartbeat, deadlock scan |

**Per-wave state is created fresh.** Everything a wave accumulates — the
scheduler and the reference graph it was planned against, the lock policy,
progress, completions and failures, commit bookkeeping, grants, read sets,
parked results, post-wave caches, the metric counters and the planning
summary — lives in one `WaveState`. `install_plan` replaces it with `WaveState.start(...)`, and
`recover` with the restored wave; there is no reset list, so a field added to
`WaveState` is per-wave by construction. Session-lifetime state — the
collaborators, the user objective, cascade history, token usage, the symbol
index, `.makignore`, the executor, the last result — stays on `Session`.
`session.wave` exposes the current wave, and `session.batches`,
`session.pipeline` and `session.post_wave` the collaborators an embedder or a
test drives directly.

**Size budgets.** `tests/test_module_budgets.py` keeps `core.py` within 600
lines, every module in `mak/session/` within 800 and every function within 80;
an allowlist entry with a written reason is the only exception.

## Initialize

Four ordered steps, then ingestion:

1. **Take the project lease** before anything reads or mutates `.mak/`.
2. **`RecoveryManager.recover_journal()`** — resolve any commit an interrupted
   run left in flight.
3. **`lock_table.clear()`** — sound because the lease proves the prior owner is
   gone.
4. **`WorkTreeReconciler`** — load `.makignore`, prune stored nodes whose file
   is no longer ingestable (reported as `pruned_nodes`), and synchronize the store
   with the working tree via `NodeStore.sync_file`: changed fragments advance to
   their next version, unchanged ones are left alone, symbols that disappeared
   are **retired**, and a file gone from disk retires all its nodes.

MAK's own `mak_dir` is skipped unconditionally (`Workspace.is_store_path`), independent
of `exclude_patterns`. A file whose content differs from the digest MAK recorded
when it last wrote it was edited by someone else; `session.on_external_edit`
decides: `adopt` (default — the working tree is the newer truth) or `conflict`
(raise `WorkTreeConflictError` before planning).

## Plan

**One index per store generation.** `PlanPreparer.index()` returns a
`PlanningIndex` — `generation`, the dependency `graph`, its `referrers`, the
planner's `InventoryView` and `build_ms` — rebuilt only when
`NodeStore.generation` moves. Proposing, validation and `install_plan` all use it,
so a planned wave parses the store once. The graph is read-only: the installed
wave keeps it as its pre-wave graph, and a later generation gets a new index.

`propose_plan(user_task)` (`PlanPreparer.propose`) runs the planner with the
index's view and an observer that logs each call as `PLANNER_CALL`, validates the
plan (with `seen_files` for a `retrieval` plan), and — when
`planner.auto_caller_tasks` and `planner.validate` are on — adds MAK's caller
tasks ([§9](09-planner.md)) and re-validates the augmented plan. It returns a
`PlanProposal(subtasks, findings, proposed_task_ids, planning)` without reviewing
or installing it — the public entry point for a front end that reviews the plan
its own way (the interactive app). A planner object with only `decompose` still
works (no telemetry). `plan()` is `propose_plan` + optional review +
`install_plan`. Nothing outside `mak/` touches a `Session` private attribute.
**`install_plan` always re-validates** — it is the one entry point shared by
`plan()`, the interactive app, cascade waves, and edited review plans:

- containment check on every target (`PlanPreparer.reject_unsafe_targets`),
  naming every offender;
- `validate_plan` against the index's graph when `planner.validate` is on;
  findings go to `session.last_plan_findings` and one `PLAN_VALIDATED` event. A
  proposed task the reviewer dropped comes back as a `missing_caller` finding, not
  as a task;
- the last proposal's `PlanningSummary` is handed to the new wave once
  (`take_planning_summary()`), so a cascade wave or a directly installed plan
  reports zeros;
- `agent_type` normalization (`PlanPreparer.assign_agents`): unassigned tasks are
  distributed **round-robin across the healthy agent pool**; an unconfigured type
  is remapped to the pool's first agent (`AGENT_REMAPPED`);
- an optional `objective=` keyword carries the user's request for repair
  descriptions when the planner is bypassed.

## Run

Dispatch lock-satisfiable ready tasks onto the thread pool, enriching each bundle
([§3.3](03-ast-pipeline.md#33-dispatch-bundle-enrichment)) and capturing its read set. As results arrive, `BatchProcessor` stages
returned sources (within the grant), orders each batch of concurrent completions
topologically, runs the commit pipeline ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)) and commits transactionally
(`CommitApplier`).

**Transactional commit.** The prospective file is reconstructed and `compile()`d
before any `commit_node`. `NodeStore.transaction()` defers destructive effects;
`install_files` renders every affected file in memory, journals each
destination's prior content, and writes them all atomically. **The store's
metadata save is the commit point.** The journal (`mak/node_store/journal.py`) is
written before the first output file is touched; on restart, recovery compares
the versions it recorded with the reopened store — all match means roll
**forward**, any differ means roll **back**; an `installed` journal only re-runs
the (idempotent) git audit.

**Partial completion.** When `modified_nodes ⊊ target_nodes`, completed grants
commit and only their locks release; the remaining grants are re-dispatched as a
narrowed task (`SubTaskProgress`, bounded by `max_attempts`).

**No-op acceptance.** A no-op is accepted only when the agent **set**
`no_changes_required` — a truncated reply never contains it — and the targets
exist and the assembled file compiles (`NoopPolicy.is_asserted`). `NoopPolicy`
additionally refuses the assertion when it cannot be true: for a target absent
when the wave was installed, if a task this one directly depends on targets the
same file, or if it is a whole-file grant on the first attempt. An accepted no-op
logs `ACCEPTED_NOOP` and counts in `tasks_noop`, never inflating
`tasks_completed`. A live repair obligation also blocks the no-op path.

**Retries differ from the attempt they follow.** `RetryPolicy` chooses a
note by `error_kind`: a truncation asks for the same work in less output; a
protocol slip restates the schema in full; a stale read carries the diff; anything
else names the reason and asks not to repeat it. A result with `retryable=False`
fails the task immediately rather than spending the remaining attempts.

**Empty results are explained.** `NoopPolicy.describe_empty_result` names the
actual cause:
a truncation stop reason, ids outside the grant, ids with no source, a missing
target, a file still invalid after "no changes", or an unasserted empty success.

**The spend ceiling.** `session.max_total_tokens` is checked between run-loop
iterations. On a breach MAK stops dispatching, lets in-flight work finish and
commit (`BatchProcessor.drain`), and reports
`SessionResult.stopped_reason`. It never interrupts a commit.

**Token accounting** is the session's own: `Session.token_usage` /
`total_tokens` sum `TaskResult.usage` from every attempt (repair turns included)
plus `Planner.token_usage`. The CLI counter, the final report, and the budget all
read this one number.

**Honest outcomes.** A run is `COMPLETED` only if the scheduler is done.
`SessionResult` splits the rest into `failed` (exhausted attempts or not
retryable), `skipped` (has a failed ancestor), and `blocked` (stranded for
another reason). `failure_reasons` lists **every distinct reason** per task, in
the order first seen.

**Metrics** (`SessionResult.metrics`, logged as `PLAN_METRICS`):
`max_concurrency`, `mean_concurrency`, `conflict_rejections`, `redispatches`,
`tasks_completed`, `tasks_failed`, `tasks_noop`, `dispatches`,
`context_bytes_total`, `mean_context_bytes`, `starved_dispatches`, `stale_reads`,
`stale_redispatches`, `adjudicated_accepts`, and what planning the wave cost:
`planner_calls`, `planner_rounds`, `planner_input_tokens` (cached included),
`planner_cached_tokens`, `planner_output_tokens` (zeros for a wave the planner
did not plan).

**Wave bookkeeping** for post-wave analysis, written only past a commit point:
`WaveState.committed` (old/new source per node, `None` for a superseded
fragment), `file_before`, `file_writers`, `node_writer`, `fragments_before`,
`commit_log`.

## Cascade waves and repair obligations

`mak/cascade.py::run_cascade_waves(session, approve, announce=…)` is the one
post-wave loop both front ends call: detect ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)) → announce → approve → install
→ run, until nothing remains, the approver declines, or `max_waves` is reached.

Every cross-module fix-up carries kernel-generated **`RepairObligation`s**. Each
records the finding's exact identity and a stable *family* identity for its
syntactic site (so swapping one nonexistent name for another at the same site is
not progress). Obligations are persisted with the task graph, rendered in review
(`must resolve=`), preserved by `merge_fixups`, and reattached after an edited
review plan; an edited plan that keeps neither the caller nor the provider is
rejected as unrepairable. Before a repair commits, the
`ProspectiveSemantics` check substitutes the staged sources into a
whole-repository view and reruns the deterministic checks; an unresolved
obligation, or a newly introduced defect, rolls the edit back **before** the
node-store transaction and audit commit. When the provider of an unresolved
import exposes nothing that could satisfy it, the provider becomes a writable
target rather than inviting a caller-only rename. The user's original objective is
repeated in every repair description.

Before presenting another wave, the loop fingerprints the implicated source,
repair scope, and obligation families (`cascade_state_fingerprint`). An immediate
repeat stops as `stalled`; a non-adjacent repeat (A → B → A) stops as
`oscillating`. Fingerprints are persisted, so a recovered session cannot restart
the same loop. Fix-up task ids are a sanitized slug plus a digest of the
unsanitized subject (`fixup_task_id`), so two files that sanitize alike cannot
collide.

`CascadeOutcome` carries every wave plus `declined`, `limit_reached`, `stalled`,
`oscillating`, `unrepairable`, a deterministic `stop_reason`, and `unresolved`.
**`ExecutionResult`** (`mak/execution_result.py`) aggregates the initial wave and
the cascade outcome and answers two questions separately: `tasks_completed` (a
statistic) and `request_satisfied` (the verdict that gates exit codes and the
push). **A later wave never clears an earlier failure.** Task ids are namespaced
by wave index.

## Teardown and recovery

`teardown()` (`Finalizer`) runs `session.test_command` through the `TestRunner`
(`mak/test_runner.py`) and returns a `TeardownResult` (`mak/teardown.py`) with
outcome `passed`, `failed`, `skipped`, or `error`. The push gate requires
`git.auto_push`, a git helper, a **satisfied aggregate outcome**, and
`session.test_policy` — `require_pass` (default) or `allow_skip`.
`push_skipped_reason` names whichever gate refused.

`recover()` takes the lease, resolves the journal, expires stale leases, and
rebuilds the scheduler — and a fresh `WaveState` around it — from
`task_graph.json` (`RecoveryManager`). `mak run --recover` calls it instead
of `initialize()`/`plan()`. A missing or corrupt graph logs
`SESSION_ENDED(recover_failed=True)` and reports "nothing to resume" rather than
raising.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [10. Git integration](10-git-integration.md) · [12. Configuration](12-configuration.md) →
