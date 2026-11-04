# 5. Conflict detection

> [Contributing guide](../../CONTRIBUTING.md) · ← [4. Lock manager](04-lock-manager.md) · [6. Scheduler](06-scheduler.md) →
>
> **Code:** `mak/conflict_detector/`, `mak/semantic/`, `mak/session/commit/` · **Tests:** `tests/conflict_detector/`, `tests/semantic/`, `tests/session/test_commit_pipeline.py`, `tests/test_semantic_corpus.py`

Node-level write locks guarantee that no two agents write the same node at once —
a textual guarantee. Conflict detection covers the rest, in layers: structural
checks on each commit, semantic checks at commit time, whole-repository checks at
wave end, and optional gates. The whole design follows one rule: **precision over
recall.** A false conflict fails a task, its retries, and everything depending on
it; a missed one is what tests are for.

## 5.1 Structural checks (`mak/conflict_detector/`)

Run by `ConflictDetector.detect(EditRound)` (`detector.py`) after the `compile()`
gate, returning a `ConflictReport` (`ok`, `reasons`, `by_check`):

- **`signature_check.py`** — call sites vs. a rewritten function's signature
  (arity and keyword names). A `*args`/`**kwargs` splat suppresses what it makes
  unprovable. Types are never inspected.
- **`import_check.py`** — across header edits of one file, flag the same bound
  name imported to different targets, and duplicates. One edit binding a name two
  ways (`try: import ujson as json / except: import json`) is not a conflict.
- **`name_collision_check.py`** — a qualified symbol introduced by more than one
  agent in the same file and round.
- **`registry_key_check.py`** — a registry key registered twice **by this edit**,
  compared against the table's previous committed source so pre-existing debt is
  not blamed on the task.
- **`node_ids.py`** — the one place edit keys are parsed (`file_scope_of`,
  `class_scope_of`).

Concurrently completing tasks are validated together: each task's `EditRound`
carries definitions from the whole batch (cross-agent signature authority) and
edits scoped to its own files. A task colliding with a batch peer already
committed ahead of it is rejected and retried.

**Resolution rules that keep the checks precise:**

- **Decorators are read.** `@staticmethod` has no receiver to strip;
  `@classmethod` binds `cls`; any other decorator not known to preserve the
  signature (`@lru_cache()`, `@app.route(...)`, `@x.setter`) drops the definition
  from the table rather than checking a guessed shape.
- **Attribute calls resolve by receiver, never by bare name:**

  | call | resolves to |
  |---|---|
  | `foo(...)` | module-level `foo` |
  | `self.foo(...)` | `<enclosing class>.foo` |
  | `cls.foo(...)` / `Owner.foo(...)` | `Owner.foo`, only if it is a class/static method |
  | `self._data.get(...)`, `svc.run(...)` | nothing — receiver type unknown |

- **Methods are keyed `Class.method` only**, so two classes in one file never
  shadow each other.
- **Fragments are re-framed** (`detector._frame_fragment`): `method` and
  `class_body` fragments are wrapped back in a synthetic `class C:` so a
  dedented method is not misread as a function with a real `self` parameter.
- A whole-file node id is its own scope, so two new files each defining `main`
  do not collide.

`tests/conflict_detector/test_false_positive_corpus.py` is the standing guard: a
corpus of correct code that must yield zero conflicts and a corpus of genuine
breakage that must still be reported. **Add to both when you touch a check.**

## 5.2 Wave-end checks

`Session.detect_cross_module_defects()` runs whole-repository checks over the
files a wave touched, all sharing `mak/conflict_detector/module_index.py`
(`ModuleIndex`: one strict import-resolution and class-lookup layer, so checks
cannot disagree about what a module binds):

- **`cross_module_check.py`** — `unresolved_import` (importing a name the target
  module does not bind) and `signature_mismatch` (calling an imported function
  with a shape its definition rejects). Import resolution is **strict**: the whole
  dotted tail must match a repo path, so third-party imports never resolve onto a
  same-named repo file.
- **`attribute_check.py`** — `mod.name` where `mod` is an in-repo module that no
  longer binds `name`. Skips modules that bind names dynamically.
- **`override_check.py`** — an override that cannot accept what its base
  accepts. Skips constructors, non-Liskov dunders, receiver-kind mismatches, and
  unresolvable bases.
- **`constructor_check.py`** — a call to an in-repo class its constructor rejects
  (`__init__`, own or inherited, or `@dataclass` fields including `kw_only`,
  `ClassVar`, `field(init=False)`). Skips metaclasses, `__new__`, multiple
  resolved bases, and unrecognised decorators.
- **`cycle_check.py`** — a **new** module-level import cycle among touched files
  (iterative Tarjan SCC), reported only if it contains a `from`-import edge.
  Function-local and `TYPE_CHECKING` imports are excluded.
- **`duplicate_check.py`** — the same top-level function created by different
  tasks in different files with an equivalent body. Conventional names (`main`,
  `run`, `test*`, dunders) are never reported.

Every check also runs against the **pre-wave** state, and only defects absent
there are reported. Results are cached per store `generation`.

## 5.3 Semantic conflicts (`mak/semantic/`)

Two edits on *disjoint* nodes can each be correct and wrong together. MAK
prevents what it can at scheduling time, detects the rest at commit and at wave
end, and resolves by re-dispatch or fix-up. The taxonomy MAK is measured against
(ten shapes; see the [semantic conflict corpus](../benchmarks-and-research.md#semantic-conflict-corpus)) drives the design.

### Prevention

- **Read-set versioning (`read_set.py`).** For **every** context key a bundle
  carries, a `ReadMark` records the node's committed version and a **content
  digest** — the digest is the identity that matters, because a version number
  can recur with different content (an uncommitted node, or the nodes of a
  deleted file ingested again, restart at version 1).
  `build_read_set` derives the set from the enriched context, so a new
  enrichment layer is covered automatically. It is captured on the dispatching
  thread and persisted with the task graph (`Scheduler.annotations`).
- **The lock refinements in [§4.5](04-lock-manager.md#45-derived-lock-resources-and-the-lock-policy).**
- **Declared contracts (`mak/planner/contracts.py`, `mak/semantic/contracts.py`).**
  A task may declare, per target, the signature it will give it
  (`contract: {node_id: "def f(a: int) -> R"}`), plus `changes_api`,
  `api_targets`, and `registry_keys`. Every declaration is **enforced at commit,
  never trusted**: `implementation_mismatch` compares the committed signature
  (name, parameters with annotations and defaults, return, async-ness, or class
  bases) with the declaration. Dependents see providers' contracts as layer 0
  of their bundle. With `semantic.contract_dispatch` (off by default), an edge
  whose provider fully declares every node it writes and whose locks do not
  conflict becomes **soft** (`soft_edges`, `DAG.soft_dependencies`): the
  dependent is dispatched against the contract while the provider is still being
  built, and its commit is parked until the provider commits. If the provider
  fails, the dependent fails with it.
- **Plan validation reads the declarations** ([§9](09-planner.md)): it relaxes an edge a body-only
  declaration made unnecessary, adds an edge from a declared API change to every
  task that names the node, orders a class's structure writer before its other
  members, and flags duplicate registry keys and appends to an ordered
  (unkeyed) registrar.

### Detection at commit

The commit pipeline (`mak/session/commit/`) is an **ordered list of checks**.
Each implements `CommitCheck` — a `name` and `check(ctx) -> Verdict` — and reads
the commit through a `CommitContext`: the task id, its staged node ids, the
sources committed earlier in the same batch, and read access to the store, the
lock table and the `WaveState`. `CommitPipeline.commit` stops at the first
verdict that is not `accept` and hands it to the one handler for its kind:

| Verdict | Handler |
|---|---|
| `accept` | continue; after the last check, `CommitApplier` commits ([§11](11-session-lifecycle.md)) |
| `reject` | a conflict: count it, log `CONFLICT_DETECTED`, record the reason, roll back |
| `defer` | park the result (below): it waits on a lock or on a contract provider |
| `resend` | roll back and re-dispatch with the check's `retry_note` and `error_kind` |
| `fail` | the task cannot succeed (its contract provider failed): roll back and spend its attempts |

Checks never roll back, record failures, write kernel notes or park results —
the handlers own every consequence, so a verdict means the same thing whichever
check returned it. Two effects stay with a check because they are the question
it asks: logging its own findings, and escalating a lock through
`ctx.escalate` (a try-acquire *is* the test). A check that rewrites staged
sources — the registrar merge — returns them as `Verdict.restaged`, and the
pipeline stages them before acting on the verdict.

`DEFAULT_CHECKS` (`commit/pipeline.py`), in order — a test pins the order, so
reordering is a reviewed change:

1. **`ProvidersCommitted`** — a task dispatched against a contract (soft edge)
   is deferred until its providers commit, and fails if one of them failed.
2. **`RegistrarMerge`** (`registrar.py`, `registry_merge.py`). Two appenders
   each return the table as they read it plus their lines. `plan_merge` extracts
   what an agent **appended** to the version it read (`appended_entries`) and
   replays exactly those entries onto the table as it is **now**
   (`merge_append`) — a textual splice that preserves formatting. Anything that
   is not a pure keyed append takes the node's plain WRITE lock instead (or is
   deferred until it can), or is sent back with a fresh read when the table
   moved.
3. **`ReadSetCurrent`** (`stale.py`). Every read-set node is compared by
   digest against what is committed now. A stale node is classified
   `body_only`, `api_change`, `deleted`, or `created` — where "interface" is
   binding-level (`interface.py::changed_bindings`: gaining a name breaks nobody)
   — and checked for whether the task's own code **references** what changed.
   `semantic.stale_read` decides:

   | Policy | body-only | referenced API change |
   |---|---|---|
   | `accept_if_api_stable` | accept | re-dispatch |
   | `revalidate` (default) | accept | re-run the static checks on the new code; accept a parameter-shape-only change that passes; ask the adjudicator if configured; else re-dispatch |
   | `redispatch` | re-dispatch | re-dispatch |
   | `reject` | accept | reject |

   A node seen only as an API digest never re-dispatches on a body change; a node
   covered by a contract the task was built against is accepted outright. Every
   stale read is logged (`STALE_READ`). A re-dispatch carries a bounded unified
   diff of each blocking node in `retry_note` and counts against `max_attempts`
   with `error_kind="stale_read"`.
4. **`StructuralConflicts`** ([§5.1](#51-structural-checks-makconflict_detector)), over this edit, the batch's earlier
   commits, and the contract stubs the task builds against.
5. **`ContractsHold`** — a contracted node must match its declared contract.
6. **`InterfaceGranted`** (`resources.py`). A task that declared
   `changes_api=False` and changed an existing binding is sent back. Any other
   undeclared interface change needs `#api` WRITE: taken on the spot if free
   (`API_ESCALATED`), otherwise the commit is deferred until concurrent readers
   finish.
7. **`PreviewCompiles`** — every touched file, assembled with the staged
   fragments, must `compile()`.
8. **`ProspectiveSemantics`** — the whole-repository checks rerun with the staged
   files substituted; a newly introduced defect, or a repair obligation still
   unresolved, rejects the edit before anything is durable ([§11](11-session-lifecycle.md)).
9. **`LeaseStillHeld`** — the task must still hold every lease it is about to
   commit through; a lease that lapsed during a long agent call is never
   committed through.

A new commit-time check is one more entry in `CommitPipeline.checks`
(`default_checks(...)` builds the default tuple); it needs no `Session` change.

**Parked commits.** A finished result that cannot commit *yet* — waiting on a
registrar's exclusive lock, an `#api` reader, or a contract provider — is parked
(`ParkedCommits.park`, `COMMIT_DEFERRED`), not re-run: the work is fine, only the
timing is wrong. Every batch completion retries the parked set
(`ParkedCommits.resume`). If every
in-flight task is parked, the run loop releases the highest-id one — re-gated on
its dependencies if it waited on a contract provider
(`Scheduler.wait_for_dependencies`), re-dispatched with a note otherwise.

### Detection at wave end

`Session.detect_cascade_tasks()` assembles fix-up work from three sources, folded
into one task per node (`merge_fixups`), by `PostWaveAnalyzer` and
`FixupBuilder` (`mak/session/post_wave.py`, `fixups.py`):

- **Cascade on the reference graph** (`cascade_graph.py`). The wave's changes are
  diffed as **symbols** (`symbols.py::diff_symbols` — signature change, deletion,
  or body change, whether the file is stored as fragments or whole). Callers are
  found by walking both the pre-wave and post-wave reference graphs, same-file
  callers included. A caller whose calls are provably compatible with the new
  signature is left alone. A deleted symbol's fix-up names a same-bodied symbol
  the wave added as a rename hint.
- **Cross-module defects** ([§5.2](#52-wave-end-checks)). Each fix-up names the task(s) whose work met
  in the defect and carries a bounded diff of **both sides**.
- **Optional gates** (`gates.py`), all off by default. None can fail a wave: a
  finding becomes a fix-up task, and a gate whose tool is missing or times out is
  logged (`GATE_FINDING`) and skipped.
  - **`type_gate.py`** (`semantic.type_check: pyright | mypy`) diffs diagnostics
    over touched files and their importers against a baseline taken at
    `initialize()`, so only what the wave introduced counts.
  - **`impact_tests.py`** (`semantic.impact_tests`) selects tests whose static
    import closure reaches a touched module, runs them on the wave's end state
    and on the pre-wave state (materialized without git by `overlay.py`), and
    attributes each **new** failure to the smallest task or task **pair** that
    reproduces it, within `semantic.impact_max_overlays`. `WaveView.subset`
    rebuilds "pre-wave plus these tasks' commits" from the per-commit fragment
    log (`WaveState.fragments_before`, `WaveState.commit_log`).
  - **`import_smoke.py`** (`semantic.import_smoke`) imports each touched module
    in a fresh subprocess before and after the wave.
  - **`adjudicator.py`** (`semantic.adjudicator: "<backend>:<model>"`) asks a
    model whether a dependent's use still holds, for a stale read the static
    checks could not settle. It can only turn an uncertain re-dispatch into an
    accept; any other answer leaves the re-dispatch standing. **It is the only
    model call on the commit path**, so it is fenced
    (`mak/session/adjudication.py::AdjudicatorFence`): it is wired into
    `ReadSetCurrent` only when `semantic.adjudicator` is set (an injected
    `adjudicator_llm` replaces how that model is built, never whether it runs);
    `validate_config` rejects it with any `stale_read` other than `revalidate`,
    which is the only policy that consults it; it is budgeted
    (`adjudicator_max_calls` per wave); every consultation is logged as
    `ADJUDICATION` with `nondeterministic: true`, as is each `STALE_READ` it
    accepted; and accepts are counted (`adjudicated_accepts` in the plan
    metrics) and named in the run summary.

`mak/semantic/sources.py` provides the repository as a lazily assembled
file → source mapping, so commit-time checks do not pay O(repo) up front.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [4. Lock manager](04-lock-manager.md) · [6. Scheduler](06-scheduler.md) →
