# Contributing to the Multi Agent Kernel (MAK)

Welcome, and thank you for considering a contribution to MAK. This document is the
**single, self-contained reference** for working on the project: it explains what
MAK is and why, walks through every subsystem in depth, and lays out exactly how to
set up, build, test, and submit changes.

It is long on purpose. MAK implements an unusual idea (a shared-memory concurrency
kernel for coding agents), and contributing effectively requires understanding the
architecture, not just the file layout. Read Part I for the mental model, Part II
when you need subsystem detail, and Parts III–V for the day-to-day workflow,
roadmap, and design rationale.

> **A note on honesty.** Where the implementation does not yet match the ambition,
> this guide says so plainly (see [Current status](#current-status) and
> [Open problems](#open-problems)). Please keep that standard: document the gap,
> don't paper over it.

---

## Table of contents

- [Part I — Understanding MAK](#part-i--understanding-mak)
  - [What MAK is](#what-mak-is)
  - [Why not Git worktrees?](#why-not-git-worktrees)
  - [Architecture at a glance](#architecture-at-a-glance)
  - [End-to-end data flow](#end-to-end-data-flow)
  - [Current status](#current-status)
  - [Benchmark: MAK vs. git worktrees](#benchmark-mak-vs-git-worktrees)
- [Part II — The subsystems in depth](#part-ii--the-subsystems-in-depth)
  - [Core types, exceptions, logging](#1-core-types-exceptions-logging)
  - [Node Store](#2-node-store)
  - [The AST pipeline](#3-the-ast-pipeline)
  - [Lock Manager](#4-lock-manager)
  - [Conflict Detector](#5-conflict-detector)
  - [Scheduler](#6-scheduler)
  - [Agent Runner & Adapters](#7-agent-runner--adapters)
  - [Planner & human-in-the-loop review](#8-planner--human-in-the-loop-review)
  - [Git integration](#9-git-integration)
  - [Session lifecycle](#10-session-lifecycle)
  - [Configuration](#11-configuration)
  - [Command-line interface](#12-command-line-interface)
- [Part III — Developing](#part-iii--developing)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Project layout](#project-layout)
  - [The quality gates](#the-quality-gates)
  - [Coding standards](#coding-standards)
  - [Commits, branches, and pull requests](#commits-branches-and-pull-requests)
- [Part IV — Where to contribute](#part-iv--where-to-contribute)
  - [Open problems](#open-problems)
  - [Good first contributions](#good-first-contributions)
  - [How MAK was built (history)](#how-mak-was-built-history)
- [Part V — Design decisions & rationale](#part-v--design-decisions--rationale)
- [Glossary](#glossary)
- [License](#license)

---

# Part I — Understanding MAK

## What MAK is

MAK is a **kernel for concurrent multi-agent software development**. The goal: let
several coding agents edit one shared codebase at the same time — without Git
worktrees, without merge conflicts, and without a reconciliation step at the end.

Most multi-agent coding systems give each agent its own Git branch and merge at the
end. That is a **message-passing** architecture: agents work in isolation and
synchronize only at boundaries, by which point the dependency information needed to
resolve conflicts has been lost.

MAK takes the **shared-memory** approach instead. All agents operate on the same
working directory. The kernel owns a symbol-level lock table and arbitrates
concurrent access the way an operating system arbitrates shared memory between
threads — with reader-writer locks, dependency tracking, and deadlock detection.
Git is demoted to a post-hoc audit log, written *after* MAK validates an agent's
output.

**Core constraint:** MAK is self-contained and bootstrap-capable. There is no
external orchestration system. The kernel manages everything — planning,
scheduling, lock arbitration, agent lifecycle, conflict detection, and file
reconstruction — in a single Python process.

## Why not Git worktrees?

Worktree-based systems defer conflict resolution to *merge time*, where the
dependency graph between changes is no longer explicit. MAK resolves conflicts at
*scheduling time*, where the dependency graph is known and locks can be
pre-allocated to prevent conflicting concurrent writes from ever happening.

Two corollaries shape the whole design:

- **The node store, not the filesystem, is the source of truth.** Files on disk are
  *derived artifacts*, reconstructed on demand from the committed node versions.
- **An agent never sees the whole file.** It receives only the AST nodes it holds
  write locks on (plus read-only context), edits them in isolation, and returns the
  modified fragments. The kernel reassembles the file. This is the shared-memory
  model: agents see a window into the codebase, not the codebase.

**Why is the LLM confined to the planner?** Every LLM call in the runtime path adds
latency and unpredictability. Task decomposition genuinely needs language
understanding; everything downstream — graph traversal, lock arbitration, AST
reconstruction, conflict detection — is deterministic and stays that way.

## Architecture at a glance

```
┌─────────────────────────────────────────────────────────────────────┐
│                            MAK KERNEL                               │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐   │
│  │   Planner   │───▶│ Dependency Graph │───▶│    Scheduler      │   │
│  │  (LLM call) │    │    (DAG)         │    │  (DAG traversal)  │   │
│  └─────────────┘    └──────────────────┘    └────────┬──────────┘   │
│                                                      │              │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                      Lock Manager                             │  │
│  │   node_id → { holder, mode, acquired_at, timeout }            │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                      Node Store                               │  │
│  │   (file, kind, qualified_name) → versioned AST fragment       │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                   Conflict Detector                           │  │
│  │        parse gate → structural checks → accept/reject         │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                    Agent Runner                               │  │
│  │   route to adapter → assign task → collect TaskResult         │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
   API adapter:          API adapter:         API adapter:
   anthropic_api         openai_api           gemini_api
   (+ CLI fallbacks: claude_code / codex / copilot)
          │                    │                    │
          └────────────────────┼────────────────────┘
                               ▼
                    Shared working directory
                    + Node Store (on disk)
                    + Git (audit log only)
```

The kernel manages four moving parts:

- **Node Store** — decomposes the codebase into independently lockable AST nodes
  (functions, methods, classes, module headers); the source of truth.
- **Lock Manager** — a reader-writer lock per node; atomic, all-or-nothing
  acquisition; deadlock detection.
- **Scheduler** — turns the planner's subtask DAG into running work, pre-allocating
  locks before dispatch and unblocking downstream tasks as dependencies complete.
- **Agent Runner** — calls agents through a swappable adapter interface; API
  adapters (Anthropic/OpenAI/Gemini SDKs) are primary and return structured JSON.

## End-to-end data flow

```
User: "Implement topological sort in the scheduler module."
│
▼
Planner (one LLM call)
  → SubTask A: implement TopologicalSorter.sort   [write: dag.py::function::...sort]
  → SubTask B: implement Scheduler.tick           [write: scheduler.py::...tick]
                                                  [read:  dag.py::...sort]  (depends on A)
│
▼
DAG builder:  A ──▶ B   (B depends on A)
│
▼  (optional) Human-in-the-loop review of the plan: approve / edit / abort
│
▼
Scheduler tick #1
  A is ready → atomically acquire write lock on dag.py::...sort
  B waits for A
  → dispatch A to an agent (via its adapter)
│
▼
Agent Runner
  → enrich the TaskBundle with the current source of A's write targets + read context
  → send to the adapter; agent returns a TaskResult with the modified fragment(s)
│
▼
Collection phase
  → ast.parse each new fragment             ✓
  → conflict detector (parse gate + checks) ✓
  → reconstruct the affected file from committed fragments + staged versions,
    ast.parse-validate the result *before* committing (transactional)
  → commit fragment versions, write the file, release A's locks
  → write a [MAK-A] audit commit
│
▼
Scheduler tick #2:  A complete → B unblocked → dispatch B  → (same collection phase)
│
▼
Session complete → run the test suite → push if green → write the session summary
```

## Current status

The **kernel is functionally complete and well-tested**: **490 tests pass**,
`mypy --strict mak` is clean, and `ruff check mak tests` is clean. The concurrent
shared-memory pipeline — the project's reason to exist — runs end-to-end and is
proven by an integration gate, and a real agent's rewritten source now reaches the
node store over the wire. Primary development is done; the work now is the **open
problems** in [Part IV](#part-iv--where-to-contribute) — evaluation, planner
efficiency, and multi-language support.

The module-by-module state:

| Module | Status |
|---|---|
| `mak/core/` (types, exceptions, logging) | Complete |
| `mak/config.py` + `mak/config.yaml` | Complete |
| `mak/node_store/` | Complete |
| `mak/lock_manager/` | Complete |
| `mak/agent_runner/` (runner, registry, protocol, API adapters) | Complete |
| `mak/scheduler/` | Complete |
| `mak/conflict_detector/` | Complete |
| `mak/planner/` (planner, review, LLM backends) | Complete |
| `mak/git_integration/` | Complete |
| `mak/session.py` | Complete (concurrent) |
| `mak/bootstrap.py` (composition root) | Complete |
| `mak/__main__.py` (CLI entry point) | Complete |
| CLI subprocess adapters (`claude_code`, `codex`, `copilot`) | Complete |
| `mak/agent_runner/sandbox.py` (Docker isolation) | Complete |
| **Concurrent execution** | **Complete (Wave 5)** — see below |

> ### ⚠️ The mental model to hold before contributing
>
> **The node store, not the filesystem, is the source of truth, and an agent is a
> pure fragment transform** — one node's source in, one node's rewritten source out.
> An agent never roams the repo or edits disk directly; it returns the new source of
> each node it was granted (`TaskResult.new_sources`), and the *kernel* stages,
> validates, conflict-checks, commits, reconstructs, and writes. Anything an agent
> returns outside its lock grant is ignored. Internalize this and the rest of the
> codebase follows: the lock table, scheduler, conflict detector, and transactional
> commit all exist to make that fragment-transform contract safe under concurrency.
>
> The concurrency *is* done and proven. `Session.run` dispatches every
> lock-satisfiable ready task onto a bounded thread pool (`max_concurrent_agents`),
> **batches** concurrently-completing results into one multi-task conflict-detection
> round (so the cross-agent checks fire), commits in a deterministic order against the
> batch's already-committed peers, re-validates write-lock ownership at commit time,
> and renews in-flight leases with a heartbeat. Atomic lock pre-allocation makes the
> pipeline deadlock-free; a `DeadlockDetector` watchdog is defense in depth. The gate
> is `tests/test_concurrency_integration.py`. A real model can drive the whole thing
> given an API key (a live hosted-model call is simply not exercised in CI).

## Benchmark: MAK vs. git worktrees

[`benchmark/`](benchmark/) is a fair, reproducible head-to-head between MAK and the
git-worktree multi-agent model it was designed to replace. Both sides run the **same
workload** with the **same agents** (same models, same per-operation prompt, same
task assignment); the only thing that differs is the coordination model, so any
difference in the numbers is attributable to that.

### The workload

A small library, `toolkit`, with **9 operations** across three modules — each an
unimplemented stub — plus a shared dispatch table, `registry._register_all`, that
**every** operation must add one line to. A 30-test suite is the accuracy oracle.

That shared `_register_all` is the whole point: it is the one symbol every agent must
touch. Under MAK a node-level write lock serializes those edits and none are lost;
under worktrees every branch edits it independently, so every merge after the first
collides there and must be reconciled. Module files are assigned one-agent-per-module
so they merge cleanly — the conflict is isolated to exactly the contended symbol.

### Fairness controls

- **Same agents/models** on both sides, and the **same agent layer** — identical
  prompts, and the registry line itself is applied by a deterministic helper, so the
  model's only creative job is the function body. The comparison isolates
  *coordination*, not registry-editing skill.
- **Same assignment** (operation → agent) and the **same 30-test oracle**, run the
  same way.
- **Parallel timing model.** The worktree side's agents work concurrently, so its
  implementation phase is charged as `max` over agents of that agent's call time (not
  the sum); the sequential merge+resolve phase is added on top. MAK is charged its real
  end-to-end wall-clock. If anything this is generous to the worktree side.
- The worktree baseline **resolves** each conflict with one model call (rather than
  leaving conflict markers, which would fail import and collapse accuracy) — the fairer,
  stronger baseline.

### First real result

Recorded run: **3 × `claude-sonnet-4-6`** (the same three agents on both sides), 9
operations, 30 tests.

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 20.37s | **11.64s** |
| Total tokens | **2,052** | 3,192 |
| — input / output | 1,229 / 823 | 2,153 / 1,039 |
| Model calls | 9 | 11 |
| Accuracy (tests passed) | 30/30 (100%) | 30/30 (100%) |
| Registry merge conflicts | **0** | 2 |
| Conflict-resolution calls | **0** | 2 |

### Reading it carefully

- **Tokens — MAK wins by 36%.** Both sides make the same 9 implementation calls; the
  entire ≈1,140-token gap is the worktree side's **two conflict-resolution calls** (the
  registry collided on 2 of the 3 merges), which re-send the conflicted file as input.
  MAK reconciles nothing, so it never makes those calls. This is the cleanest, most
  robust signal in the benchmark.
- **Merge conflicts — 0 vs 2, by construction.** MAK serializes the registry node under
  one write lock; each task reads the latest committed version and appends, so a
  collision is *impossible*. The worktree side hits `agents − 1` conflicts (the first
  branch merges clean; each later branch collides on `_register_all`).
- **Accuracy — tied at 100%, but read the asterisk.** The functions are small, the model
  implements them correctly, and — critically — the resolver merged the 2 conflicts
  *correctly this time*. The failure mode MAK removes is a resolver that drops or
  garbles a `register(...)` line: that operation silently never enters the table and its
  dispatch test fails. The tie reflects easy tasks plus a strong resolver, not the
  absence of a difference; on larger or harder workloads the worktree accuracy is the
  one that erodes.
- **Time — the worktree side was faster here, and the *why* matters.** This workload is
  **maximally contended**: all 9 tasks must edit the single shared node. MAK's
  correctness on that node comes from serializing its writes, so the 9 tasks run
  effectively sequentially (≈9 model calls back-to-back ≈ 20s). The worktree model lets
  all three agents implement fully in parallel and defers the collision to a cheap merge
  phase. In other words, worktrees win wall-clock *precisely because they do not
  coordinate during implementation* — they pay for it afterward in tokens, conflicts,
  and the risk of lost work. On a realistic workload where most edits are independent
  (different files/symbols), MAK parallelizes that independent work just like worktrees
  and serializes only the few contended writes, so the wall-clock gap shrinks while the
  token and correctness advantages remain. **This benchmark is the worst case for MAK's
  wall-clock and a strong case for its tokens and correctness** — that is the honest
  shape of the trade.

### Caveats

- **Single model.** The recorded run used three Claude agents because the OpenAI account
  had no active billing and the Gemini key's prepaid credits were depleted. Same model
  on both sides keeps it fair, but it is not a cross-model comparison. Supply your own
  keys and `--models` to compare across providers.
- **Maximally contended, small.** Every task touches the shared node, and there are only
  9 of them. Real projects are mostly independent work plus some contention; a larger,
  *partially*-contended workload would show MAK's parallelism on the independent part and
  widen the token/correctness gap on the contended part. Extending the benchmark in those
  directions is [open problem #1](#1-extend-the-worktree-benchmark).

### Running it

```bash
python benchmark/run_benchmark.py --mode mock     # keyless self-test
python benchmark/run_benchmark.py --mode real     # real models (needs keys)
```

Results are written to `benchmark/README.md` (summary) and `benchmark/STATS.md`
(detail); `--render-only` regenerates them from the last run without spending tokens.
See [`benchmark/README.md`](benchmark/README.md) for the full guide.

---

# Part II — The subsystems in depth

This part is the technical design reference. Each section is independently readable;
skip to the subsystem you're touching.

## 1. Core types, exceptions, logging

`mak/core/` holds the contracts every other module imports.

- **`types.py`** — the shared value objects, all frozen dataclasses where possible:
  - `NodeId` — a `NewType(str)`. The identity of a lockable code unit (see the
    [node identity scheme](#node-identity)).
  - `NodeFragment` — a node's raw source plus metadata (`node_id`, `kind`,
    `source`, `version`).
  - `LockMode` — `READ`, `WRITE`, `INTENT_WRITE` (a `StrEnum`).
  - `LockEntry` — a single held lock (`node_id`, `mode`, `holder`, `task_id`,
    `acquired_at`, `timeout_s`).
  - `ResourceRef` / `ResourceKind` — a reference to a file- or symbol-level resource.
  - `TaskBundle` — the unit sent *to* an agent: `task_id`, `description`,
    `target_nodes`, and a `context` dict (enriched with write/read source).
  - `TaskResult` — the unit returned *from* an agent: `task_id`, `success`,
    `modified_nodes`, `error`.
  - `SubTask` — a planned unit of work: `task_id`, `description`, `target_nodes`
    (what it will *write*), `context_nodes` (what it needs to *read*),
    `depends_on`, `agent_type`.
- **`exceptions.py`** — every domain exception derives from `MakError`:
  `LockError`, `SchedulingError`, `ConflictDetectionError`, `GitIntegrationError`,
  `NodeStoreError`, `PlannerFailedError`, `PlanReviewAborted`, `SessionError`,
  `AgentError`, `UnknownAgentTypeError`, `ConfigError`.
- **`logging.py`** — `SessionLogger`: an append-only JSON-Lines event log. `EventType`
  is a `StrEnum`; `LogEntry` round-trips via `to_json()` / `from_json()`. Writes are
  serialized under a lock and flushed, so events never interleave or truncate.

## 2. Node Store

The Node Store (`mak/node_store/`) is MAK's equivalent of shared memory. It replaces
the filesystem as the source of truth for code.

### Node identity

A **node** is the smallest independently lockable unit of code. Identity is
**position-independent** — based on qualified name, not line number — so inserting a
new function does not invalidate another agent's lock on an existing one. The id
format is:

```
<file_path>::<kind>::<qualified_name>
```

| Kind | Example id |
|---|---|
| `function` (top-level def) | `mak/scheduler/dag.py::function::topological_order` |
| `class` (the class *shell*) | `mak/lock_manager/rwlock.py::class::RWLock` |
| `method` (def inside a class) | `mak/lock_manager/rwlock.py::method::RWLock.acquire` |
| `module_header` (imports + leading constants) | `mak/config.py::module_header::__header__` |
| `module_body` (top-level code after the first def/class) | `mak/config.py::module_body::__body__` |
| `class_body` (class-level statements after a method) | `…::class_body::RWLock` |

Duplicate names (e.g. `@overload` stubs, conditional defs) are disambiguated with a
`#n` suffix so no symbol is silently dropped.

### On-disk layout

Runtime state lives under `.mak/` (gitignored):

```
.mak/
├── node_store/
│   └── <mirrored source tree>/<file>.py/
│       ├── __header__.v1.py
│       ├── <Class>.v1.py
│       ├── <Class.method>.v1.py
│       └── metadata.json     ← index: kind, order, current version per node
├── lock_table.json           ← persisted lock state (rebuilt on crash recovery)
├── task_graph.json           ← DAG execution state (for crash recovery)
└── session.log               ← append-only event log
```

### `NodeStore` API

`NodeStore` (`store.py`) owns versioning and persistence. Key methods:
`get_node`, `put_node`, `commit_node`, `rollback_node`, `revert_node`, `get_staged`,
`list_nodes`, `get_committed_fragments`, `parse_file_into_nodes`.

The store **owns version assignment**: `put_node` ignores any version on the
incoming fragment and stamps it `current_committed + 1`, so callers never guess the
next version. Prior versions are retained on disk, which is what makes
`revert_node` (roll a committed node back to its previous version) possible.
Fragment order is preserved as `order` metadata so reconstruction emits source in
its original order. **All mutations are guarded by a re-entrant lock.**

## 3. The AST pipeline

This is the kernel's core mechanism — it replaces Git's diff/merge with a
structured operation. Four phases:

### 3.1 Ingestion (file → fragments) — `ingestion.py`

> **Key design decision:** ingestion uses **raw-source span tiling**, *not*
> `ast.unparse()` and *not* `libcst`. The file is partitioned into line spans that
> tile it completely in source order; each fragment keeps its **raw source text**.
> Because nothing is ever re-rendered through an unparser or a CST, comments,
> decorators, blank lines, and formatting survive a round trip *by construction*.
> `libcst` is **not** a dependency.

Mechanics:
- `ast.parse` the file for structure, then tile by line spans:
  - leading import/constant block → `module_header`;
  - each top-level `def`/`async def` → a `function` fragment (decorator lines
    included — spans start at `min(decorator lineno)`);
  - top-level executable code between defs → `module_body` fragments.
- Classes decompose **one level**: a `class` *shell* fragment (the `class` line,
  docstring, and leading attributes) plus one `method` fragment per method, plus
  `class_body` fragments for class-level statements that follow a method. This is
  what gives **method-level lock granularity**.
- `parse_file_into_fragments(path, source=None)` returns fragments in source order;
  `walk_and_parse(root, include, exclude)` runs it over a directory tree.

### 3.2 Fragment dispatch (node store → agent)

When a task is dispatched, the session builds a `TaskBundle` and **enriches** it:
for every write-target node it attaches the current committed source
(`write_source:<id>`), and for every `context_node` it attaches read-only source
(`read_source:<id>`). The agent edits with full sight of the current code and its
read context — it is never asked to edit blind — but it still never sees the whole
file.

### 3.3 Collection (agent output → node store)

When an agent returns a `TaskResult`:
1. `ast.parse` each modified fragment — reject on failure.
2. Run the [conflict detector](#5-conflict-detector).
3. **Transactional commit** (see [Session](#10-session-lifecycle)): build the
   prospective file from committed fragments with the staged versions substituted,
   `ast.parse`-validate it *before* committing. Only if every affected file
   reconstructs cleanly are the fragment versions committed and the files written.
4. On success, release the task's locks and write an audit commit. On any failure,
   roll back the staged versions (and revert any commit) so the store and disk
   never diverge.

### 3.4 Reconstruction (fragments → file) — `reconstruction.py`

`assemble_fragments(fragments)` concatenates fragments **in their stored source
order** (separated by blank lines). `reconstruct_file(...)` assembles, runs
`ast.parse` as a guard, formats with `ruff format` (auto-discovering the venv's
`ruff` binary, falling back to raw source and *logging* on failure — never silently
swallowing), and writes to disk.

### 3.5 The round-trip invariant (load-bearing)

The contract that makes shared-memory editing trustworthy:

```
ingest(file) → store → reconstruct  ≡  semantically equivalent to the original,
with decorators, statement ordering, and comments intact.
```

This is verified by a property test (`tests/node_store/test_roundtrip.py`) over a
corpus that includes decorated defs, methods, module-level constants between
classes, top-level executable blocks, inline and standalone comments, and
`@overload` stubs — plus a test that MAK round-trips its *own* source. **If you
touch ingestion or reconstruction, this test is your gate.**

> **Known limitation:** a `class` shell fragment (the class line with methods
> removed) is not independently parseable, which mildly contradicts the "fragments
> parse in isolation" aspiration. This is acceptable because reconstruction
> validates the *assembled* file, not individual shells. A consequence to be aware
> of when editing methods: an agent must return method source with its original
> indentation; a dedented method would fail the assembled-file parse and be
> rejected (not corrupted) by the transactional commit gate.

## 4. Lock Manager

`mak/lock_manager/` is the concurrency arbiter.

### 4.1 Lock model

A reader-writer lock per node, with three modes:

| Mode | Concurrent holders | Use |
|---|---|---|
| `read` | unlimited | agent reads a symbol as context |
| `write` | 1 (exclusive) | agent edits a symbol |
| `intent_write` | multiple (compatible with reads, **excludes writers**) | declare a future write; deadlock-prevention signal |

The canonical conflict matrix lives in `conflicts.py` and is consumed by **both**
`RWLock.can_acquire` *and* the `DeadlockDetector`, so the two can never disagree.

### 4.2 Lock table

`LockTable` (`lock_table.py`) holds lock state in memory and persists to
`.mak/lock_table.json` after every mutation (for crash recovery). Notable methods:
`try_acquire`, `try_acquire_all` (atomic multi-lock — all-or-nothing, never partial
acquisition), `release`, `release_all`, `renew` / `renew_all` (lease heartbeat),
`expire_stale`, and the entry accessors.

**Concurrency model (option B):** every public mutation is guarded by one
table-wide **re-entrant lock**, so `try_acquire_all`'s check-pass and acquire-pass
cannot be interleaved by another thread. Per-node `RWLock` objects are only ever
touched while this lock is held. A concurrency stress test
(`tests/lock_manager/test_concurrency.py`) drives many threads at a shared node set
and asserts that no two conflicting holders ever coexist.

**Lease safety:** lock expiry is *observable*, not silent — an expiring lease is
logged and reported via an optional `on_expire` callback, so a scheduler can fail
and roll back the holder's task rather than have its lock vanish underneath it.
Holders keep leases alive with `renew`.

### 4.3 Deadlock detection

`DeadlockDetector` (`deadlock_detector.py`) builds a directed **wait graph** (edge
A → B means task A waits for a lock held by task B, with a conflict check),
detects cycles via an **iterative, deduplicated** DFS (`find_cycles`), and resolves
them **wound-wait** style: abort the youngest task in the cycle, release its locks,
and re-queue it.

> The lock-contention paths are now reached by the concurrent live pipeline (Wave 5),
> driven by the concurrency integration gate. The deadlock detector is wired into
> `Session.run` as a per-iteration watchdog; because the scheduler pre-allocates all
> of a task's locks atomically, a waiting task holds none, so the wait graph is
> acyclic by construction and the watchdog is defense in depth rather than a
> hot path.

## 5. Conflict Detector

`mak/conflict_detector/` runs in the collection phase, between an agent's output
and its acceptance. It is **intentionally shallow** — a structural gate, not a type
checker. It gates on `ast.parse` success plus three checks; full correctness is the
test suite's job.

- **`signature_check.py`** — when one agent rewrites `func_b` and another's fragment
  calls `func_b`, verify the call sites are still compatible with the new signature
  (arity + keyword names). Conservative: a `*args`/`**kwargs` splat suppresses the
  checks it makes unprovable, so no false conflicts are reported. Types are never
  inspected.
- **`import_check.py`** — across concurrent `__header__` edits, flag **conflicting**
  imports (same bound name → different targets) and **duplicate** imports.
- **`name_collision_check.py`** — flag a qualified symbol (including `Class.method`)
  introduced by more than one agent in the same file/round.
- **`detector.py`** — `ConflictDetector.detect(EditRound)` runs the parse gate then
  all three checks, returning a `ConflictReport` (`ok`, `reasons`, `by_check`).

> The cross-agent value of these checks is now live (Wave 5): `Session._process_batch`
> validates concurrently-completing tasks together, building each task's `EditRound`
> with `definitions` spanning the whole batch (cross-agent signature authority) and
> `symbol_edits`/`header_edits` scoped to the files the task touches (name-collision
> and import checks are file-local). A task that collides with a batch peer already
> committed ahead of it is rejected and retried.

## 6. Scheduler

`mak/scheduler/` turns the planner's plan into running work.

- **`dag.py`** — `DAG` builds the directed graph from `SubTask.depends_on` and
  **validates at construction**: unique ids, every dependency references a known
  task, no self-edges, acyclic (Kahn's algorithm) → `SchedulingError` otherwise.
  Exposes a deterministic `topological_order()`, `mark_complete()`, and
  `newly_unblocked()` (hands out each task exactly once; the first call yields the
  initial ready set).
- **`scheduler.py`** — `Scheduler.tick()` drains the ready queue under **atomic lock
  pre-allocation**: before dispatching a task it acquires *all* of the task's write
  locks in one `try_acquire_all`. If any lock is unavailable the task stays ready
  and is retried next tick — partial acquisition (the classic deadlock setup) never
  happens. `on_task_complete` releases locks, marks the DAG edge, and extends the
  ready queue; `on_task_failed` optionally re-queues. Execution state persists to
  `.mak/task_graph.json` after every transition; `from_persisted(...)` rebuilds the
  scheduler for crash recovery (re-queuing in-flight tasks). Collaborators (lock
  manager, registry, agent runner) are injected behind `Protocol`s for mock-based
  testing.

## 7. Agent Runner & Adapters

`mak/agent_runner/` is the boundary to the actual agents. The key principle: **the
kernel never calls a model API directly.** It speaks to an `AgentAdapter`, which
translates between MAK's protocol and a specific backend.

### 7.1 The adapter interface

- `AgentAdapter` (ABC, `adapters/base_adapter.py`) — transport-agnostic. Methods:
  `format_task(bundle) -> str`, `parse_result(raw) -> TaskResult`,
  `health_check() -> bool`. This is all an API adapter needs.
- `SubprocessAgentAdapter` (ABC) — adds `spawn(working_dir) -> Popen` for CLI
  adapters, so API adapters aren't forced to implement a meaningless subprocess
  method.

### 7.2 API adapters are primary

> **Design decision:** the primary adapters are **direct API integrations**, not CLI
> subprocess wrappers. CLI stdout scraping is brittle against upstream format
> changes; direct API calls return structured JSON natively. Equally important,
> MAK's agent contract is a *pure fragment transform* (one node in → one strict
> `TaskResult` out) — the kernel owns planning, locking, conflict detection,
> reconstruction, and git. Autonomous file-editing agents (which roam the repo, edit
> disk, run tests, commit) conflict with the node-store-as-source-of-truth model. A
> single structured API call is the right shape; an autonomous agent loop is not.

The three built-in API adapters all **force structured output** so the model cannot
reply with prose:

| Adapter (`agent_type`) | Backend | How structured output is forced |
|---|---|---|
| `anthropic_api` (primary) | Anthropic Messages API | `tool_choice` pinned to a `submit_task_result` tool whose schema is the `TaskResult` shape |
| `openai_api` | OpenAI Chat Completions | JSON mode (`response_format={"type": "json_object"}`) |
| `gemini_api` | Google GenAI `generate_content` | function-calling config in `ANY` mode restricted to `submit_task_result` |

The SDKs (`anthropic`, `openai`, `google-genai`) are declared dependencies, but each
adapter imports its SDK **lazily** and accepts an **injectable client** — so import
time stays fast and the tests never make a real call (they inject fakes).

CLI subprocess adapters (`claude_code`, `codex`, `copilot`) are a **secondary
fallback**, implemented over a shared `CliSubprocessAdapter` base (`cli_adapter.py`):
they speak MAK's newline-JSON wire protocol over a pooled subprocess, take a `cmd`
override, and can run inside a Docker sandbox (see §7.6). Real CLIs typically need a
thin wrapper to speak the line protocol, so the adapter does not hard-code any one
CLI's flags. The API adapters remain primary.

### 7.3 Registry and composition root

- `AdapterRegistry` (`registry.py`) — an **instance**, never module-global mutable
  state. `register(agent_type, cls)` registers a zero-arg class; `register_factory(
  agent_type, factory)` registers a callable that builds a *configured* adapter
  (this is how a configured `model` + API key reach an adapter, which a bare class
  can't carry). `get(agent_type)` resolves and instantiates, raising
  `UnknownAgentTypeError` for unknown types.
- `bootstrap.py` (the **composition root**) — `build_registry(config)` registers a
  config-bound factory per agent type (binding each agent's configured `model` and
  the API key resolved from its `api_key_env` at build time; SDK clients are still
  built lazily, so this performs **no network call**). CLI types get a factory bound
  to their `cmd` and an optional sandbox; an unknown type resolves to a clear error.
  `default_agent_type(config)` returns the routing default (the first configured
  agent), and `validate_config(config)` rejects unknown agent types at startup.
  `mak/__main__.py` is the thin CLI shell over these functions.

### 7.4 The wire protocol

`protocol.py` defines the single canonical wire schema — exactly the `TaskBundle` /
`TaskResult` dataclasses, serialized as newline-delimited JSON with
`protocol_version` `"1.0"`. `decode_task_bundle` rebuilds nested `LockEntry` /
`ResourceRef` objects rather than leaving raw dicts.

**The agent's rewritten source travels on the result.** `TaskResult.new_sources` maps
each changed node id to its full new source. `decode_task_result` accepts three
shapes and normalizes them into that field: `modified_nodes` (ids only — source
staged out of band, e.g. by a local test runner), a `modified_fragments` array of
`{node_id, new_source}` (what the API adapters elicit from the model), or an explicit
`new_sources` map. The session stages each returned source via `put_node` before the
commit phase (§10), so a real agent's edit reaches the store through the normal
transactional path; sources for nodes outside the task's grant are ignored.

### 7.5 The runner

`AgentRunner.assign(adapter, task)` (`runner.py`) is the single entry point and
routes by adapter type:
- **API adapters** (primary): `format_task → send → parse_result`.
- **Subprocess adapters** (the CLI path): driven over an idle-process pool
  per agent type — write the task as a JSON line, read the result back under a
  timeout (the reader tolerates noisy preamble and multiline pretty-printed JSON),
  SIGTERM on timeout, discard a process on failure rather than returning it to the
  pool.

Every path returns a `TaskResult`: backend failures become `success=False` (so the
scheduler can re-queue); a genuinely misconfigured adapter raises `AgentError`.
`shutdown()` drains the pool.

### 7.6 Sandboxing CLI agents

CLI agents are arbitrary external processes — an attack surface. `sandbox.py`'s
`SandboxConfig.wrap(argv, working_dir)` builds the `docker run` argv that runs the
agent in a container with its filesystem scoped to the working directory (bind-mount
+ workdir) and its network restricted (`--network none` by default). The CLI
`--sandbox` flag threads a `SandboxConfig` into every CLI adapter (API adapters make
no subprocess and ignore it); `docker_available()` lets the CLI fail fast with
guidance if Docker is missing. The module only *builds* argv and probes the daemon,
so it is unit-testable without Docker.

## 8. Planner & human-in-the-loop review

`mak/planner/` is the only module that calls an LLM.

- **`planner.py`** — `Planner.decompose(user_task, node_inventory)` builds a prompt
  containing the task and the current node inventory (qualified names only, never
  source), calls an injected `PlannerLLM` (anything with
  `complete(prompt) -> str`), and validates the JSON plan with `parse_plan`. The
  parser accepts a bare array or `{"subtasks": …}`, strips code fences, validates
  each `SubTask` (including the optional `context_nodes`), and rejects duplicate ids
  and unknown dependencies. On a malformed response it retries up to `max_retries`,
  feeding the rejection reason back, then raises `PlannerFailedError`.
- **`review.py`** — `display_plan_for_review` renders the subtask list and dependency
  edges and loops **approve / edit (paste corrected JSON) / abort**
  (`PlanReviewAborted`). I/O is injected (`prompt_fn` / `printer`) for testability;
  `--no-review` skips the call. A bad plan (a missed dependency or hallucinated
  edge) causes agent collisions or needless serialization that are expensive to
  unwind mid-session, so this ~5-second human check removes the single point of
  failure in one-shot LLM DAG generation.
- **`llm.py`** — concrete `PlannerLLM` completion backends (Anthropic / OpenAI /
  Gemini), each a thin prompt-in/text-out wrapper with a lazy SDK and injectable
  client (distinct from the agent adapters, which force a structured `TaskResult`).
  `build_planner_llm(model)` picks the backend from the model-id prefix, so the CLI
  can construct a working planner from `config.planner.model` alone.

## 9. Git integration

`mak/git_integration/git.py` treats Git as an **audit log**, not an isolation layer
— lock discipline already prevents conflicting writes, so all commits go directly to
the working branch (no branches, no worktrees). `GitHelper`:

- `commit_task(task_id, files, description, agent_type, session_id)` stages and
  commits with a `[MAK-<task_id>]` subject and a `Files/Status/Agent/Session` body,
  returning the commit hash — or `None` when the staged content is byte-identical to
  HEAD (an empty diff is a no-op, not an error, so a no-change reconstruction does
  not crash the session).
- `get_session_commits(session_id)` parses `git log` into `CommitInfo` filtered by
  session; `validate_clean_state()` checks porcelain; `push(branch, remote)`
  coordinates the single end-of-session push.

All operations shell out to `git` and raise `GitIntegrationError` with stderr on
failure — nothing is swallowed.

## 10. Session lifecycle

`mak/session.py` wires everything together behind an explicit `SessionState`
machine: `CREATED → INITIALIZED → PLANNED → RUNNING → {COMPLETED | FAILED | ABORTED}`.

- **initialize** — ingest the working dir's Python files into the node store.
- **plan** — planner → optional HitL review → `install_plan` (builds the DAG +
  persisted `Scheduler`). Tasks whose `agent_type` is empty are normalized to the
  configured default agent.
- **run** — dispatch lock-satisfiable ready tasks onto the thread pool (enriching
  each bundle with write/read source); as results arrive, **stage the source each
  agent returned** (`new_sources`, within the task's grant) via `put_node`; batch
  concurrently-completing results; gate staged fragments through the conflict
  detector; **transactionally** commit and reconstruct; write a git audit commit on
  success. A node the agent claims it changed but provides no source for is dropped,
  so a misbehaving agent fails its task cleanly rather than crashing the commit.
- **teardown** — run the test suite; push if green and `auto_push` is enabled.

Three robustness properties worth knowing:

- **Transactional commit** — the prospective file is reconstructed and
  `ast.parse`-validated *before* any `commit_node`; a post-commit write failure
  triggers a best-effort revert, so the node store and disk never diverge.
- **Partial completion** — when a result's `modified_nodes ⊊ target_nodes`, the
  completed grants are accepted and committed and only their locks released; the
  *remaining* grants are re-dispatched as a narrowed task. This is tracked per task
  by `SubTaskProgress` and bounded by `max_attempts`.
- **Crash recovery** — `recover()` expires stale leases and rebuilds the scheduler
  from `task_graph.json` via `from_persisted`.
- **Honest stall reporting** — a run is `COMPLETED` *only if* the scheduler is
  genuinely done; a run stranded by an unsatisfiable DAG or locks that never freed
  reports `FAILED` with a `blocked=(…)` list, never a false success.

All collaborators are injected behind `Protocol`s, so the session is testable with
fakes.

## 11. Configuration

`mak/config.py` loads and validates `mak/config.yaml` into a `MakConfig` dataclass
tree (all frozen, `slots`). The schema:

```yaml
session:
  work_dir: "."
  mak_dir: ".mak"
  max_concurrent_agents: 3      # used by Wave 5's thread pool
  lock_timeout_s: 300.0
  deadlock_check_interval_s: 5.0

planner:
  model: "claude-sonnet-4-6"
  max_retries: 3
  temperature: 0.0

agents:                         # first entry is the default agent
  - type: "anthropic_api"
    model: "claude-sonnet-4-6"
    api_key_env: "ANTHROPIC_API_KEY"
    max_instances: 2
    timeout: 300
  - type: "openai_api"
    model: "gpt-4o"
    api_key_env: "OPENAI_API_KEY"
  - type: "gemini_api"
    model: "gemini-3-pro"
    api_key_env: "GEMINI_API_KEY"

git:
  auto_commit: true
  auto_push: false
  commit_prefix: "[MAK]"

node_store:
  include_patterns: ["**/*.py"]
  exclude_patterns: ["**/node_modules/**", "**/.venv/**", "**/__pycache__/**"]
```

Rules and behaviors:
- `agents` is **required** and must be non-empty; each entry needs a `type`. Per-agent
  fields `model`, `api_key_env`, and `cmd` are all optional.
- **API keys are never stored in config** — `api_key_env` names the environment
  variable to read at composition time. Put real keys in `mak/.env` (gitignored);
  `mak/.env.example` lists the expected variable names.
- Type coercion is strict and wrapped in `ConfigError` (e.g. `"false"` parses to
  `False`, not Python's truthy `bool("false")`).

## 12. Command-line interface

`mak/__main__.py` is the entry point: `python -m mak --task "..."`. It is a thin
shell over the composition root, split into testable functions:

- `parse_args(argv)` — flags: `--task` (required), `--config` (default
  `mak/config.yaml`), `--work-dir`, `--agent` (override the default agent type),
  `--no-review`, `--sandbox`, `-v/-vv`.
- `build_session(args, config, sandbox)` — assembles the `Session` and all its
  collaborators (node store, lock table, registry via `build_registry`, agent runner,
  planner via `build_planner_llm`, git helper, logger, default agent).
- `main(argv, *, session_builder=build_session)` — loads + validates config, builds
  the session, drives **initialize → plan → run → teardown**, and maps domain errors
  to friendly messages and exit codes: `0` success, `1` for an aborted review /
  planner failure / failed-or-blocked run / failing tests, `2` for a config error or
  a missing Docker daemon under `--sandbox`. The `session_builder` seam lets tests
  drive `main` end-to-end with a fully-faked session.

Run end-to-end: `python -m mak --task "..." --config mak/config.yaml`. Agents are
dispatched concurrently (bounded by `max_concurrent_agents`); agent and planner
backends are selected entirely by the config file.

---

# Part III — Developing

## Prerequisites

- **Python ≥ 3.11** (the project uses `match`/`StrEnum`/modern typing).
- **git** on `PATH` (the git integration shells out to it).
- The agent SDKs (`anthropic`, `openai`, `google-genai`) install automatically as
  dependencies; they're imported lazily, so the test suite never needs a live key.

## Setup

```bash
git clone <repo-url>
cd multi-agent-kernel

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -e ".[dev]"             # installs mypy, pytest, ruff, types-PyYAML

pre-commit install                  # optional: run the gates on every commit
```

Copy the env template if you'll make real calls:

```bash
cp mak/.env.example mak/.env        # then fill in the keys you use
```

## Project layout

```
mak/
├── __main__.py            # CLI entry point: python -m mak --task "..."
├── bootstrap.py           # composition root: build_registry / default_agent_type / validate_config
├── config.py              # config loading + validation
├── config.yaml            # default configuration
├── session.py             # session lifecycle, transactional commit, recovery
│
├── core/
│   ├── types.py           # NodeId, NodeFragment, LockEntry, TaskBundle, …
│   ├── exceptions.py      # all MakError subclasses
│   └── logging.py         # append-only JSON-Lines session logger
│
├── node_store/
│   ├── ingestion.py       # file → raw-source span-tiled fragments
│   ├── store.py           # NodeStore: versioned get/put/commit/rollback/revert
│   └── reconstruction.py  # fragments → file (assemble + ruff format)
│
├── lock_manager/
│   ├── rwlock.py          # per-node reader-writer lock
│   ├── lock_table.py      # thread-safe lock state + persistence + leases
│   ├── conflicts.py       # the single canonical conflict matrix
│   └── deadlock_detector.py
│
├── scheduler/
│   ├── dag.py             # DAG build + validation + topological order
│   └── scheduler.py       # tick loop, atomic lock pre-allocation, persistence
│
├── conflict_detector/
│   ├── detector.py        # orchestrates the checks
│   ├── signature_check.py
│   ├── import_check.py
│   └── name_collision_check.py
│
├── planner/
│   ├── planner.py         # LLM decomposition, SubTask schema, retry logic
│   ├── llm.py             # PlannerLLM completion backends (build_planner_llm)
│   └── review.py          # human-in-the-loop DAG review
│
├── agent_runner/
│   ├── runner.py          # routes to API/subprocess adapters; failure policy
│   ├── registry.py        # AdapterRegistry (instance, not global)
│   ├── protocol.py        # TaskBundle/TaskResult wire (de)serialization
│   ├── sandbox.py         # Docker isolation for CLI agents (--sandbox)
│   └── adapters/
│       ├── base_adapter.py
│       ├── anthropic_api_adapter.py   # primary
│       ├── openai_api_adapter.py      # primary
│       ├── gemini_api_adapter.py      # primary
│       ├── cli_adapter.py             # shared CliSubprocessAdapter base
│       ├── claude_code_adapter.py     # secondary (claude CLI)
│       ├── codex_adapter.py           # secondary (codex CLI)
│       └── copilot_adapter.py         # secondary (gh copilot CLI)
│
└── git_integration/
    └── git.py             # audit-log commits, log parsing, push

tests/                     # mirrors mak/ package-for-package
.github/workflows/ci.yml   # CI: ruff + mypy --strict + pytest
.pre-commit-config.yaml    # local hooks mirroring CI
pyproject.toml             # deps, ruff + mypy + pytest config
```

Tests mirror the source tree (`tests/core/`, `tests/node_store/`,
`tests/lock_manager/`, `tests/scheduler/`, `tests/conflict_detector/`,
`tests/agent_runner/`, `tests/planner/`, `tests/git_integration/`,
`tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_session.py`).

## The quality gates

Three gates must be green for every change — locally, in pre-commit, and in CI:

```bash
pytest -q                  # the full suite (currently 480 tests)
mypy --strict mak          # zero errors
ruff check mak tests       # zero findings
```

CI (`.github/workflows/ci.yml`) runs all three on push and PR against `main`; the
pre-commit hooks mirror them. A change that breaks any gate will not merge.

To run a focused subset while iterating:

```bash
pytest tests/node_store/ -q
pytest tests/test_session.py -q
```

If you add a feature, add tests for it. If you change ingestion or reconstruction,
the round-trip property test (`tests/node_store/test_roundtrip.py`) is mandatory; if
you change locking, keep the concurrency stress test
(`tests/lock_manager/test_concurrency.py`) green.

## Coding standards

Python throughout, with these conventions (enforced by `ruff` and `mypy --strict`):

**Naming**
- `snake_case` for variables, functions, modules; `PascalCase` for classes;
  `UPPER_SNAKE_CASE` for constants; `_leading_underscore` for private members.

**Structure**
- One module, one responsibility — don't co-locate unrelated logic.
- Functions do one thing. If one exceeds ~40 lines, ask whether to split it.
- **No global mutable state.** Pass state explicitly via arguments or dataclass
  instances. (The registry being an instance rather than a module-global dict is a
  direct consequence of this rule.)
- Use `dataclasses` for structured data — no raw dicts as function arguments.
- **Type annotations are mandatory** on every function signature; the codebase is
  `mypy --strict` clean and must stay that way.

**Imports**
- Standard library, then third-party, then internal (`mak.*`), separated by blank
  lines (ruff's isort enforces this; `mak` is configured as first-party).
- Never use wildcard imports.

**Error handling**
- Explicit exceptions with descriptive messages; define domain exceptions in
  `mak/core/exceptions.py`.
- Never silently swallow an exception — log and re-raise, or handle deliberately.

**Comments & docstrings**
- Public functions and classes require docstrings (ruff enforces this in `mak/`;
  tests are exempt).
- Inline comments explain *why*, not *what*.
- **No TODO comments in committed code** — open a tracked issue instead.
- **Keep comments self-contained.** Do not reference internal planning artifacts or
  documents that aren't part of the committed tree — a comment must make sense to a
  contributor who only has the source in front of them.

## Commits, branches, and pull requests

- **Branch off `main`.** Don't commit directly to `main`.
- **Keep PRs scoped.** One logical change per PR; keep the three gates green in every
  commit you push where practical, and certainly in the final state.
- **Write descriptive commit messages** — explain the *why*. (Note: the `[MAK-<id>]`
  commit subject format is what the *kernel* writes for agent audit commits; your own
  development commits should follow ordinary good practice.)
- **Tests accompany behavior changes.** A PR that changes behavior without tests, or
  that drops the suite/`mypy`/`ruff` from green, will be asked for revision.
- **Update this file** when you change something it documents (a new subsystem, a
  config key, a workflow step). Because the internal planning docs are not part of
  the committed tree, `CONTRIBUTING.md` is the canonical reference contributors rely
  on — keep it accurate.

---

# Part IV — Where to contribute

Primary development is done — the kernel is built, gated, and proven. This part is
about **what's left**, ordered by leverage. Start here.

## Open problems

The active tracks, highest-leverage first. These are research and tooling on top of a
finished kernel — open an issue to align before starting a large one.

### 1. Extend the worktree benchmark

A first measured comparison of MAK against a git-worktree workflow now exists in
[`benchmark/`](benchmark/) — see [Benchmark: MAK vs. git
worktrees](#benchmark-mak-vs-git-worktrees) below for the methodology and the first
real numbers. It is deliberately small and maximally-contended; the open work is to
make it representative: more model mixes (the recorded run is single-model because of
account/billing limits), larger and *partially*-contended workloads (where MAK's
parallelism on independent work should show), harder tasks (where the accuracy gap
should open), and a throughput-focused variant. This is still the highest-leverage
track — turning one data point into a curve.

### 2. Planner context / token efficiency

`Planner.decompose` embeds the *entire* node inventory in the prompt on *every* call —
many tokens re-sent each time on a large codebase, even though the inventory barely
changes. Directions: prompt caching of the stable inventory prefix, retrieval (send
only task-relevant nodes), a coarse→fine two-stage planner, a module-level summarized
inventory, or a template bypass for fixed task shapes.

### 3. Planner quality at scale

Even with HitL review, one-shot subtask decomposition degrades on large tasks with
many interdependent nodes. Prompt engineering, few-shot examples, and a possible
multi-step planner (outline → detail) are open.

### 4. Multi-language support

The node-store schema is language-agnostic; only ingestion and reconstruction are
Python-specific. The plan is to abstract them behind a `LanguageBackend` interface
(with `tree-sitter` as the target backend for TypeScript/Go). Python proves the
model; language-agnosticism follows.

### 5. Live-model hardening

The wire and adapters carry an agent's rewritten source, and the pipeline runs against
a real model given an API key — but live behavior isn't exercised in CI (no key). Real
runs against hosted models, prompt tuning so models reliably return *complete* node
source, and retry/repair policy for malformed edits are worth shaking out on real
tasks.

## Known limitations (accepted tradeoffs)

These are deliberate limits of an intentionally shallow, correctness-first kernel.
None can corrupt code — each *fails safe* — so they are accepted tradeoffs, documented
here so contributors don't mistake them for bugs:

- **Class-shell fragments aren't independently parseable.** A `class Foo:` shell with
  its methods removed isn't valid Python alone, so a task that targets a `class`-shell
  node and returns just the shell is *rejected* by the parse gate (never corrupted).
  Reconstruction validates the assembled file, so this is safe; making shells
  standalone-parseable (or the detector shell-aware) is a possible improvement.
- **The conflict detector is name-based and shallow.** Its cross-file signature check
  can flag a call to a same-named-but-unrelated function in another module — a false
  positive that costs a bounded retry, not correctness. It is a structural gate, not a
  type checker, by design.
- **`context_nodes` are read but not read-locked.** This is intentional: a *hard*
  dependency on another task's output belongs in `depends_on` (which the DAG
  enforces); `context_nodes` are *soft* reference (sibling methods, attributes) read
  at dispatch. Read-locking them would add serialization for no correctness gain on
  the cases the DAG already covers.
- **The deadlock watchdog never fires.** Atomic lock pre-allocation means a waiting
  task holds no locks, so the wait graph is acyclic by construction. The
  `DeadlockDetector` runs each iteration as genuine defense-in-depth that, by design,
  finds nothing.

## Good first contributions

- Add test coverage for an edge case in an existing module (ingestion corner cases,
  conflict-detector splat handling, config coercion).
- Improve error messages — make failures point at the fix.
- Documentation: clarify a subsystem in this file, or add module-level examples.
- Harden a CLI adapter: add a real wrapper that makes `claude`/`codex`/`gh copilot`
  speak the MAK line protocol, or extend the sandbox (host allowlisting).

## How MAK was built (history)

MAK was built in gated **waves** — each a set of independent tasks that had to leave
the full suite, `mypy --strict`, and `ruff` green before the next began. That
discipline is why the foundation is solid. Waves 0–1 built the core (types, config,
logging, node store, lock manager, agent-runner base); Wave H hardened ingestion
(span-tiling for comments/decorators/methods), the concurrency model, and CI; Waves
2–3 added the scheduler, conflict detector, API adapters, planner + HitL, git
integration, and the session; Wave 3.5 + a hotfix hardened the session and wired the
composition root; Wave 4 delivered the CLI, secondary CLI adapters, and the sandbox;
**Wave 5** made dispatch concurrent and proved the shared-memory thesis with the
integration gate; **Wave 6** carried the agent's rewritten source over the wire so a
real agent's edit reaches the store. What remains is the open-problems list above.

---

# Part V — Design decisions & rationale

The decisions that shaped MAK, and why — useful when a change seems to cut against
the grain.

- **Shared memory over message passing.** Resolve conflicts at scheduling time
  (dependency graph explicit) rather than merge time (dependency graph lost).
- **Node store as source of truth; files are derived.** Enables symbol-level locking
  and position-independent identity, and means an agent only ever handles fragments.
- **LLM only in the planner.** Keep the runtime path deterministic; pay for language
  understanding exactly once, where it's genuinely needed.
- **Raw-source span tiling for ingestion** (not `ast.unparse`, not `libcst`).
  Comments, decorators, and formatting survive a round trip *by construction*, with
  zero extra dependencies. This superseded an earlier plan to adopt `libcst` once it
  became clear comments are lost at *ingestion*, before any reconstruction step runs.
- **API-first adapters with forced structured output.** Structured JSON is
  guaranteed, not scraped; the agent stays a constrained fragment-transformer rather
  than an autonomous file editor (which would bypass the node store and lock
  manager).
- **Thread-safe lock table via one table-wide re-entrant lock (option B).** Makes
  `try_acquire_all` genuinely atomic and is the prerequisite for real concurrency.
- **Human-in-the-loop plan review.** A ~5-second check eliminates the single point of
  failure in one-shot LLM DAG generation; bypassable with `--no-review`.
- **Transactional commit.** Validate the reassembled file before advancing the store,
  so the node store and disk never diverge.
- **Sequential-first, concurrency-as-a-gated-wave.** Prove the pipeline end-to-end
  sequentially (Wave 4) before adding the thread pool (Wave 5). Don't add concurrency
  to a pipeline that has never run once — and don't claim the concurrent path works
  until its integration test is green.

---

# Glossary

- **Node** — the smallest independently lockable unit of code (a function, method,
  class shell, module header, or interstitial body block).
- **NodeId** — `<file>::<kind>::<qualified_name>`; position-independent identity.
- **Fragment (`NodeFragment`)** — a node's raw source text plus version/order
  metadata. The unit stored, dispatched, and reassembled.
- **TaskBundle / TaskResult** — the wire objects sent to / returned from an agent.
- **SubTask** — a planned unit of work with write targets, read context, and
  dependencies.
- **EditRound** — the set of staged fragments the conflict detector validates
  together — multi-task in a concurrent batch, so cross-agent conflicts are seen.
- **Adapter** — the swappable translator between MAK's protocol and a specific agent
  backend.
- **Composition root** — `mak/bootstrap.py`, which assembles configured collaborators
  from a `MakConfig`.
- **Wave** — a gated, parallelizable phase of the build-out (see Part IV).
- **`.mak/`** — the gitignored runtime directory (node store, lock table, task graph,
  session log).

---

# License

[MIT](LICENSE) © 2026 Seungjoon Cha

By contributing, you agree that your contributions are licensed under the project's
MIT License.
