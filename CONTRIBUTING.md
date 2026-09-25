# Contributing to the Multi Agent Kernel (MAK)

Welcome, and thank you for considering a contribution to MAK.

This document is the single reference for working on the project. It explains
what MAK is, how each subsystem works, how to set up and test a change, and what
is open for contribution. By participating you agree to uphold the
[Code of Conduct](CODE_OF_CONDUCT.md).

MAK implements an unusual idea — a shared-memory concurrency kernel for coding
agents — so contributing well requires the architecture, not just the file
layout. Read **Part I** for the mental model, **Part II** for the subsystem you
are touching, **Part IV** for the day-to-day workflow, and **Part V** for what to
work on next.

> Jump to: [Current status](#current-status) · [Open issues](#open-issues) ·
> [Quality gates](#quality-gates)

---

## Table of contents

- [Part I — Understanding MAK](#part-i--understanding-mak)
  - [What MAK is](#what-mak-is)
  - [Why not Git worktrees?](#why-not-git-worktrees)
  - [Architecture at a glance](#architecture-at-a-glance)
  - [End-to-end data flow](#end-to-end-data-flow)
  - [The mental model](#the-mental-model)
  - [Current status](#current-status)
- [Part II — The subsystems](#part-ii--the-subsystems)
  - [1. Core types, exceptions, logging](#1-core-types-exceptions-logging)
  - [2. Node store](#2-node-store)
  - [3. The AST pipeline](#3-the-ast-pipeline)
  - [4. Lock manager](#4-lock-manager)
  - [5. Conflict detection](#5-conflict-detection)
  - [6. Scheduler](#6-scheduler)
  - [7. Agent runner and adapters](#7-agent-runner-and-adapters)
  - [8. Endpoints](#8-endpoints)
  - [9. Planner](#9-planner)
  - [10. Git integration](#10-git-integration)
  - [11. Session lifecycle](#11-session-lifecycle)
  - [12. Configuration](#12-configuration)
  - [13. Command line (`mak`)](#13-command-line-mak)
  - [14. Interactive app (`cli/`)](#14-interactive-app-cli)
  - [15. Model catalog](#15-model-catalog)
  - [16. Local runtimes](#16-local-runtimes)
- [Part III — Benchmarks and research](#part-iii--benchmarks-and-research)
- [Part IV — Developing](#part-iv--developing)
- [Part V — Where to contribute](#part-v--where-to-contribute)
  - [Open issues](#open-issues)
  - [Known limitations](#known-limitations)
  - [Good first contributions](#good-first-contributions)
- [Part VI — Design principles](#part-vi--design-principles)
- [Glossary](#glossary)
- [License](#license)

---

# Part I — Understanding MAK

## What MAK is

MAK is a **kernel for concurrent multi-agent software development**. It lets
several coding agents edit one shared codebase at the same time — without Git
worktrees, without merge conflicts, and without a reconciliation step at the end.

Most multi-agent coding systems give each agent its own Git branch and merge at
the end. That is a **message-passing** architecture: agents work in isolation
and synchronize only at boundaries, by which point the dependency information
needed to resolve conflicts has been lost.

MAK takes the **shared-memory** approach. All agents operate on the same working
directory. The kernel owns a symbol-level lock table and arbitrates concurrent
access the way an operating system arbitrates shared memory between threads —
with reader-writer locks, dependency tracking, and deadlock detection. Git is an
audit log, written *after* MAK validates an agent's output.

**Core constraint:** MAK is self-contained. There is no external orchestration
system; planning, scheduling, lock arbitration, agent lifecycle, conflict
detection, and file reconstruction all run in a single Python process.

## Why not Git worktrees?

Worktree-based systems defer conflict resolution to *merge time*, where the
dependency graph between changes is no longer explicit. MAK resolves conflicts at
*scheduling time*, where the dependency graph is known and locks can be
pre-allocated so conflicting concurrent writes never happen.

Two corollaries shape the whole design:

- **The node store, not the filesystem, is the source of truth.** Files on disk
  are *derived artifacts*, reconstructed from committed node versions.
- **An agent never sees the whole file.** It receives only the AST nodes it holds
  write locks on (plus read-only context), edits them in isolation, and returns
  the modified fragments. The kernel reassembles the file.

**The LLM is confined to the planner** (and to agents, which are pure fragment
transforms). Task decomposition needs language understanding; everything
downstream — graph traversal, lock arbitration, AST reconstruction, conflict
detection — is deterministic and stays that way.

## Architecture at a glance

[`diagram/`](diagram/README.md) holds the component architecture and execution
sequence as editable Mermaid sources with PNG exports.

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
│  │   resource → { holder, mode, acquired_at, timeout }           │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                      Node Store                               │  │
│  │   (file, kind, qualified_name) → versioned AST fragment       │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │              Conflict Detection (structural + semantic)       │  │
│  │     parse gate → commit-time checks → wave-end checks         │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                    Agent Runner                               │  │
│  │   route to adapter → assign task → collect TaskResult         │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
   anthropic_api         openai_api /          gemini_api
                         OpenAI-compatible     ollama_api / local_api
                         endpoints             (+ CLI: claude_code / codex / copilot)
          │                    │                    │
          └────────────────────┼────────────────────┘
                               ▼
                    Shared working directory
                    + Node Store (on disk, .mak/)
                    + Git (audit log only)
```

- **Node store** — decomposes the codebase into independently lockable AST nodes
  (functions, methods, classes, module headers); the source of truth.
- **Lock manager** — reader-writer locks per resource; atomic, all-or-nothing
  acquisition; deadlock detection; one owner per project.
- **Scheduler** — turns the planner's subtask DAG into running work,
  pre-allocating locks before dispatch and unblocking dependents as tasks finish.
- **Agent runner** — calls agents through a swappable adapter interface. API
  adapters force structured JSON output and are the primary path.

## End-to-end data flow

```
User: "Implement topological sort in the scheduler module."
│
▼
Planner (LLM call) → deterministic plan validation → (optional) human review
  → SubTask A: implement TopologicalSorter.sort   [write: dag.py::function::...sort]
  → SubTask B: implement Scheduler.tick           [write: scheduler.py::...tick]
                                                  [read:  dag.py::...sort]  (depends on A)
│
▼
DAG:  A ──▶ B
│
▼
Scheduler tick
  A is ready → atomically acquire A's locks → dispatch A; B waits
│
▼
Agent runner
  → enrich the TaskBundle (write sources, read context, dependency outputs)
  → adapter call → TaskResult with the rewritten fragment(s)
│
▼
Commit pipeline
  → map returned ids onto the lock grant; compile() each fragment
  → registrar merge → stale-read check → structural checks → contract check
    → interface enforcement
  → reconstruct affected files in memory, compile() them, journal, install
    atomically, commit node versions, release locks, write a [MAK-A] audit commit
│
▼
A complete → B unblocked → dispatch B → same pipeline
│
▼
Wave end → cascade / cross-module / optional gates → fix-up waves (reviewed)
│
▼
Teardown → run the test suite → push only if the whole run succeeded and tests pass
```

## The mental model

> **The node store is the source of truth, and an agent is a pure fragment
> transform** — node source in, rewritten node source out.

An agent never roams the repo or edits disk. It returns the new source of each
node it was granted (`TaskResult.new_sources`); the kernel stages, validates,
conflict-checks, commits, reconstructs, and writes. Anything returned outside the
lock grant is refused and logged. The agent receives its write-target sources plus
an automatically enriched context window — same-file siblings, cross-file callers,
the committed output of the tasks it depends on, and any declared contracts — so it
arrives with the dependency picture even when the planner did not enumerate it.

Everything else — the lock table, scheduler, conflict detection, the commit
transaction — exists to make that fragment-transform contract safe under
concurrency. `Session.run` dispatches every lock-satisfiable ready task onto a
bounded thread pool (`max_concurrent_agents`), batches concurrently completing
results into one conflict-detection round, commits in a deterministic order,
re-validates lock ownership at commit time, and renews leases with a heartbeat.
The concurrency gate is `tests/test_concurrency_integration.py`.

## Current status

MAK **0.9.3 Beta** (`mak/_version.py`). The kernel, the semantic-conflict layer,
endpoint support, local runtimes, and the interactive app are all implemented.

| Gate | State |
|---|---|
| `mypy --strict mak cli` | clean |
| `ruff check mak cli tests` | clean |
| `pytest -q` | green on Python 3.11 and 3.13, locally and in CI; hermetic (never reads the real `~/.config/mak/`) |

| Area | Modules |
|---|---|
| Core types, errors, logging, atomic writes, path containment | `mak/core/` |
| Node store, ingestion, reconstruction, commit transaction, journal | `mak/node_store/` |
| Locks, project lease, derived lock resources | `mak/lock_manager/`, `mak/scheduler/lock_policy.py` |
| Scheduler | `mak/scheduler/` |
| Structural conflict checks | `mak/conflict_detector/` |
| Semantic conflicts (read sets, stale reads, contracts, cascade, gates) | `mak/semantic/` |
| Planner, plan validation, human review | `mak/planner/` |
| Agent runner, API/CLI/local adapters, sandbox | `mak/agent_runner/` |
| OpenAI-compatible endpoints and capability negotiation | `mak/endpoints/` |
| Model catalog | `mak/models/` |
| Local runtime discovery and native Ollama client | `mak/local/` |
| Session, cascade loop, run outcome, teardown | `mak/session.py`, `mak/cascade.py`, `mak/execution_result.py`, `mak/teardown.py` |
| Git audit log | `mak/git_integration/` |
| Application API: run request, config, planner route and key, session assembly | `mak/application/` |
| `mak run` / `mak` console script | `mak/__main__.py`, `cli/__main__.py` |
| Interactive app | `cli/` |

What is not done yet — packaging for a public release, planner token efficiency,
multi-language support — is planned in [`TASKS.md`](TASKS.md).

---

# Part II — The subsystems

Each section is independently readable; skip to the subsystem you are touching.

## 1. Core types, exceptions, logging

`mak/core/` holds the contracts every other module imports.

**`types.py`** — shared value objects, frozen dataclasses where possible:

- `NodeId` — `NewType(str)`; the identity of a lockable code unit (see
  [node identity](#node-identity)).
- `NodeFragment` — a node's raw source plus `node_id`, `kind`, `version`.
- `LockMode` — `READ`, `WRITE`, `INTENT_WRITE` (`StrEnum`).
- `LockEntry` — one held lock (`node_id`, `mode`, `holder`, `task_id`,
  `acquired_at`, `timeout_s`).
- `ResourceRef` / `ResourceKind` — a file- or symbol-level resource reference.
- `TaskBundle` — sent *to* an agent: `task_id`, `description`, `target_nodes`,
  an enriched `context` dict, and `retry_note` (feedback for a re-dispatch, so a
  second attempt is never a byte-identical copy of the first).
- `TaskResult` — returned *from* an agent: `task_id`, `success`,
  `modified_nodes`, `new_sources`, `error`, plus `no_changes_required` (the
  agent's positive assertion that nothing needed changing), `stop_reason` and
  `usage` (from the provider), `retryable` (false for a failure that would repeat
  verbatim, such as a refusal), `error_kind` (`truncated` / `refused` /
  `protocol` / `api` / `context` / `stale_read`), and `repairs` (repair turns
  spent).
- `SubTask` — a planned unit of work: `task_id`, `description`, `target_nodes`
  (writes), `context_nodes` (reads), `depends_on`, `agent_type`, plus optional
  declarations `changes_api`, `api_targets`, `contract`, `registry_keys`, and
  kernel-owned `repair_obligations` (see §11).
- `RepairObligation` — a defect a fix-up task must resolve before it may commit.

**`task_codec.py`** — the one JSON shape for a `SubTask`, shared by the
planner's output and the persisted task graph, so a field can never exist in one
and be dropped by the other.

**`exceptions.py`** — every domain exception derives from `MakError`:
`LockError`, `SchedulingError`, `ConflictDetectionError`, `GitIntegrationError`,
`NodeStoreError`, `PlannerFailedError`, `PlanReviewAborted`, `SessionError`,
`AgentError`, `UnknownAgentTypeError`, `ConfigError`, `UnsafeNodeIdError`,
`ProjectBusyError`, `WorkTreeConflictError`, and `AgentResponseError` with its
subclasses:

| Exception | Meaning | Retryable |
|---|---|---|
| `AgentTruncatedError` | reply hit the output cap mid-generation | yes |
| `AgentRefusedError` | the model declined | no |
| `AgentProtocolError` | the HTTP call succeeded but the body did not decode into a `TaskResult` | yes |
| `AgentContextExceededError` | the bundle cannot fit the model's context window | no |

All carry `stop_reason` and `usage` so a failed attempt still accounts for its
tokens.

**`logging.py`** — `SessionLogger`, an append-only JSON-Lines event log.
`EventType` is a `StrEnum`; writes are serialized and flushed so events never
interleave. The rule behind the event set: **an event names what happened** — a
flag inside a payload is not a substitute for the right event type. Events that
exist so a run is diagnosable without re-running it:

- `TASK_DISPATCHED` — what the kernel *gave* the agent: context entry counts,
  bytes, a per-layer `layers` breakdown (`{count, bytes, nodes}`), and a
  `starved` flag.
- `AGENT_RESULT` — what came back, per attempt: granted and returned ids, source
  lengths, error, `no_changes_required`, `stop_reason`, `usage`.
- `SOURCE_DROPPED` — anything refused at staging, with the id and the grant.
- `ACCEPTED_NOOP`, `TASK_COMPLETED`, `TASK_FAILED`, `AGENT_REMAPPED`.
- `STALE_READ`, `API_ESCALATED`, `COMMIT_DEFERRED`, `GATE_FINDING`,
  `ADJUDICATION`, `CONFLICT_DETECTED`, `PLAN_VALIDATED`, `PLAN_METRICS`,
  `phase_span`.

**`atomic.py`** — `write_text_atomic`: temp file in the same directory, `fsync`,
`os.replace`. Every persisted state file goes through it, so a kill mid-write
leaves the whole old file or the whole new one.

**`paths.py`** — containment for the paths carried by node ids.
`unsafe_node_id_reason` / `check_node_id` reject an absolute path, a `..`
component, or anything under the MAK dir; `safe_path_under` additionally resolves
symlinks. A node id originates with a model and becomes a real filesystem path,
so containment is checked independently at the planner, at `install_plan`, and at
the node store.

**`budget.py`** — `resolve_output_budget(model, *, fallback, minimum, maximum)`,
the catalog lookup shared by the planner and the agent adapters.

## 2. Node store

`mak/node_store/` is MAK's shared memory. It replaces the filesystem as the
source of truth for code.

### Node identity

A **node** is the smallest independently lockable unit of code. Identity is
**position-independent** — based on qualified name, not line number — so
inserting a new function does not invalidate another agent's lock.

```
<file_path>::<kind>::<qualified_name>
```

| Kind | Example id |
|---|---|
| `function` (top-level def) | `mak/scheduler/dag.py::function::topological_order` |
| `class` (the class *shell*) | `mak/lock_manager/rwlock.py::class::RWLock` |
| `method` | `mak/lock_manager/rwlock.py::method::RWLock.acquire` |
| `module_header` (imports + leading constants) | `mak/config.py::module_header::__header__` |
| `module_body` (top-level code after the first def/class) | `mak/config.py::module_body::__body__` |
| `class_body` (class-level statements after a method) | `…::class_body::RWLock` |

Duplicate names (`@overload` stubs, conditional defs) get a `#n` suffix so no
symbol is dropped.

**Whole-file nodes.** A node id may be a **bare file path** — `app/main.py`. The
agent returns the entire file as one node, which reconstruction writes verbatim.
This is how MAK creates a new file (`reconstruct_file` creates parent
directories) and how a task rewrites an existing module wholesale. Committing a
whole-file node **supersedes** the file's fragments: `commit_node` drops every
`path::…` node for that file, `list_nodes(file_path)` returns only the whole-file
node, `list_nodes()` omits the superseded fragments from the planner inventory,
and `parse_file_into_nodes` keeps a committed whole-file node whole (a differing
source becomes its next version). Mixing whole-file and fragment targets for the
same file in one plan is rejected at plan time (§9).

### On-disk layout

Runtime state lives under `.mak/` in the work dir:

```
.mak/
├── node_store/
│   ├── metadata.json         ← index: kind, order, version, retired flag per node
│   ├── file_state.json       ← SHA-256 of what MAK last materialized per file
│   └── <mirrored source tree>/<file>.py/
│       ├── __header__.v1.py
│       ├── <Class>.v1.py
│       └── <Class.method>.v1.py
├── journal/                  ← write-ahead commit journal
├── lock_table.json           ← persisted lock state
├── owner.lock                ← the project lease
├── task_graph.json           ← DAG execution state, for --recover
└── session.log               ← append-only event log
```

### `NodeStore` API

Key methods on `NodeStore` (`store.py`): `get_node`, `put_node`, `commit_node`,
`rollback_node`, `revert_node`, `uncommit_node`, `retire_node`, `remove_node`,
`get_staged`, `list_nodes`, `list_all_nodes`, `get_committed_fragments`,
`get_preview_fragments`, `parse_file_into_nodes`, `sync_file`, `transaction`,
`node_order`, `record_materialized` / `materialized_digest`, `gc`, and the
`generation` counter.

- **The store owns version assignment.** `put_node` stamps
  `current_committed + 1`; callers never guess. All mutations run under one
  re-entrant lock.
- **Five kinds of undo, each for one job:**

  | Method | Undoes | Keeps history? |
  |---|---|---|
  | `rollback_node` | a pending (staged) fragment | n/a |
  | `revert_node` | one committed version, to `version - 1` | yes |
  | `uncommit_node` | a first-version commit, back to absent | yes |
  | `retire_node` | a symbol the working tree no longer has | yes |
  | `remove_node` | everything, permanently — maintenance only | no |

  A retired node leaves the live set (no listing, no reconstruction, no planner
  inventory) but its metadata and versions stay, so `get_node(nid, version=n)`
  still answers and `gc` does not treat its directory as an orphan.
- **`transaction()` is the commit point.** Inside a transaction the destructive
  effects — superseded-fragment deletion, version pruning, and the metadata save
  — are deferred; `_save_metadata` runs once at the end, and **that save is the
  commit point**. Before it the in-memory index is restored verbatim on failure;
  after it the deferred deletions drain. Transactions nest by depth; only the
  outermost commits or rolls back.
- **`get_preview_fragments(file, staged_overrides)`** builds the prospective file
  before committing, substituting staged versions, re-applying each fragment's
  `indent_prefix`, and superseding fragments for a staged whole-file node exactly
  as the commit would.
- **Retention.** A commit prunes the node back to `node_store.version_retention`
  versions (default 5, floor 2 because `revert_node` needs one prior version,
  `-1` keeps everything). `gc()` applies retention store-wide and removes orphan
  directories by *forward*-mapping every live id to its directory — never by
  parsing a path back into an id, since `a/b.py` and `a/b.py::function::f` nest
  inside one another on disk.
- **Ordering and `generation`.** Listings sort by `(file_path, order)` and are
  memoized; the memo is invalidated whenever the committed set changes, and the
  same invalidation increments `generation`, which callers use to cache derived
  state (the session's symbol index, the wave-end check results).
- **Crash safety.** `metadata.json` is written atomically. An unreadable one is
  quarantined to `metadata.json.corrupt` and the store starts with an empty index;
  fragments on disk are left untouched. `file_state.json` that cannot be read
  counts as "never seen", so reconciliation trusts the working tree.
- **Containment.** `_fragment_dir`, the single choke point for fragment I/O,
  asserts `check_node_id`.

## 3. The AST pipeline

The kernel's core mechanism — a structured replacement for diff/merge.

### 3.1 Ingestion (`ingestion.py`)

> Ingestion uses **raw-source span tiling**, not `ast.unparse()` and not
> `libcst`. The file is partitioned into line spans that tile it completely in
> source order, and each fragment keeps its raw text. Comments, decorators,
> blank lines, and formatting survive a round trip by construction.

- `ast.parse` for structure, then tile by line spans: the leading import/constant
  block → `module_header`; each top-level `def` → `function` (decorators
  included); top-level code between defs → `module_body`.
- Classes decompose **one level**: a `class` shell (class line, docstring, leading
  attributes), one `method` per method, and `class_body` fragments for statements
  after a method. This gives method-level lock granularity.
- `parse_file_into_fragments(path, source=None)` returns fragments in source
  order; `walk_and_parse(root, include, exclude)` runs it over a tree.
- `iter_source_files(root, include, exclude, skip=…, ignore=…)` walks the tree
  and **prunes excluded directories before descending**, so `.venv`,
  `node_modules`, and `site-packages` cost nothing. Include patterns use a small
  glob→regex translator whose `*` never crosses `/` and whose `**/` spans zero or
  more whole segments. A **trailing** `**` (`src/**`) matches every file below
  it at any depth — decided by MAK, not by the host Python, whose
  `Path.glob("src/**")` returns directories only before 3.13 and files too from
  3.13; it is the meaning `.makignore` gives `a/**`. Symlinked directories are
  not descended. A differential test pins every other shape against `Path.glob`;
  the trailing-`**` shape has an explicit expected list.

### 3.2 `.makignore` (`makignore.py`)

`node_store.exclude_patterns` is MAK's config-level list; `.makignore` is the
project's own gitignore-style list at the work-dir root, read on every
`Session.initialize()`. If missing, the first session writes one containing
`.mak/` and `.git/` (after the `git.require_clean_tree` check, so it cannot fail
that check on the run that creates it). An existing file is never overwritten.

| Pattern | Meaning |
|---|---|
| `# text` / blank | ignored; `\#` for a literal `#` |
| `name` | matches a file or directory named `name` at any depth |
| `dir/` | directories only (and everything under them) |
| `/top.py`, `pkg/mod.py` | a leading or middle `/` anchors to the work-dir root |
| `*`, `?`, `[a-z]` | wildcards that never cross `/` |
| `**/x`, `a/**/b`, `a/**` | `**` spans zero or more whole segments |
| `!pattern` | re-includes; `\!` for a literal `!` |

The last matching pattern wins, and — as in git — a file cannot be re-included
when a parent directory is ignored. `.makignore` applies to the walk
(`MakIgnore.matches`, pruning ignored directories) and to the startup prune
(`MakIgnore.is_ignored`, which checks every parent). It is not the safety net:
the session skips its own `mak_dir` unconditionally regardless of either list.

### 3.3 Dispatch: bundle enrichment

When a task is dispatched the session builds a `TaskBundle` and enriches it in
layers:

0. **Declared contracts** (`contract:<id>`) — the signatures the task's providers
   (and the task itself) declared, rendered with a role: "implement this" or
   "build against this fixed signature" (§5.3).
1. **Write targets** — committed source of every node the agent will write
   (`write_source:<id>`).
2. **Planner context nodes** — read-only source (`read_source:<id>`).
3. **Same-file siblings** — every other committed node in the target files.
4. **Cross-file callers** — nodes elsewhere whose source references a target's
   symbol. A whole-file target contributes the symbols of its committed nodes.
5. **Dependency outputs** — the committed source of every target of every task
   this one **directly** `depends_on`.

**Layer 4's filters and budget.** Only names that could be node ids (functions,
classes, methods) count as symbols — never module-level bindings like `__all__`.
A symbol shorter than 4 characters is ignored, a symbol matching more than 8
nodes is discarded as uninformative, and survivors are ranked (most matches,
then smallest, then id) until `session.cross_file_context_bytes` (default
32000) is spent. Past the budget an entry is **dropped** and counted as
`cross_file_dropped` on the dispatch event. The lookup is an inverted
`symbol → [node_id]` index keyed on `NodeStore.generation`, equivalent to the
word-boundary regex it replaces (a differential test pins that).

**Layer 5's budget.** `session.dependency_context_bytes` (default 24000; `0`
disables, `-1` unbounded). Past it, an entry **degrades to a public API digest**
(`read_api:<id>` — signatures, class members, constants, from
`mak/node_store/api_digest.py`) instead of being dropped: a dependent needs its
dependency's contract more than its implementation.

**The starvation guard.** A bundle that ends enrichment with zero context entries
while its task declares `depends_on` or `context_nodes` is a kernel defect. It is
never sent; the task fails immediately with `retryable=False` naming the defect.

Every attempt logs a `TASK_DISPATCHED` event, and the task's **read set** is
captured on the dispatching thread right after enrichment (§5.3).

### 3.4 Collection (agent output → node store)

1. **Map returned ids onto the grant** (`protocol.map_returned_sources`). Granted
   ids pass through; a symbol id inside a whole-file grant is **folded** into that
   grant; anything else is refused and logged as `SOURCE_DROPPED`. Folded
   fragments are ordered: a `module_header` first, then the store's recorded
   `order`, then emission order.
2. **`compile()` each fragment** — not `ast.parse()`, because `compile()`
   enforces every compile-time rule, including `from __future__` placement.
3. **The commit pipeline** (§5, §11).
4. **Transactional commit and reconstruction** (§11). On success, release the
   task's locks and write an audit commit; on failure the whole transaction rolls
   back.

### 3.5 Reconstruction (`reconstruction.py`)

`assemble_fragments` concatenates fragments in stored order. `reconstruct_file`
assembles, runs `compile()` as a guard, formats with `ruff format`, and writes.
`ruff` is a **runtime dependency**: `_find_ruff()` looks beside `sys.executable`,
then on `PATH`. If formatting fails, the raw source is written and the failure is
logged — formatting never fails a run.

`transaction.py` renders every affected file in memory, journals each
destination's prior content, then installs all of them with `write_text_atomic`,
so a multi-file change is all-or-nothing on disk.

### 3.6 The round-trip invariant

```
ingest(file) → store → reconstruct  ≡  the original, semantically,
with decorators, statement order, and comments intact.
```

`tests/node_store/test_roundtrip.py` is a property test over decorated defs,
methods, constants between classes, top-level blocks, comments, and `@overload`
stubs — plus MAK's own source. **If you touch ingestion or reconstruction, this
test is your gate.**

A class shell fragment is not independently parseable; that is acceptable because
reconstruction validates the assembled file. Agents must return method source
with its original indentation — a dedented method fails the assembled-file gate
and is rejected, never written.

## 4. Lock manager

`mak/lock_manager/` is the concurrency arbiter. The lock table is
**intra-process** by design; single ownership of a project across processes is
the project lease's job (§4.4). MAK does not implement distributed locking.

### 4.1 Lock model

| Mode | Concurrent holders | Use |
|---|---|---|
| `read` | unlimited | read a resource as context |
| `write` | one, exclusive | edit a resource |
| `intent_write` | many; compatible with reads, excludes writers | hierarchy intention locks and keyed-registrar appends (§4.5) |

The conflict matrix lives once in `conflicts.py` and is used by both
`RWLock.can_acquire` and the `DeadlockDetector`, so they cannot disagree.

### 4.2 Lock table

`LockTable` (`lock_table.py`) holds state in memory and persists
`.mak/lock_table.json` atomically after every mutation. Methods: `try_acquire`,
`try_acquire_all` (atomic, all-or-nothing), `release`, `release_all`, `renew` /
`renew_all`, `expire_stale`, `clear`, and accessors. Every public mutation runs
under one table-wide re-entrant lock, so the check pass and acquire pass of
`try_acquire_all` cannot interleave. `tests/lock_manager/test_concurrency.py`
drives many threads at a shared node set and asserts no two conflicting holders
coexist.

Lease expiry is observable: an expiring lease is logged and reported via an
optional `on_expire` callback. A fresh session calls `clear()` (sound because it
holds the project lease); crash recovery keeps the persisted table and
`expire_stale`s it. An unreadable `lock_table.json` starts empty — every lease in
it is reconstructible.

### 4.3 Deadlock detection

`DeadlockDetector` builds a wait graph (A → B: A waits for a lock B holds),
finds cycles with an iterative deduplicated DFS, and resolves them wound-wait
style (abort the youngest, release its locks, re-queue it). Because the scheduler
pre-allocates all of a task's locks atomically, a waiting task holds none, so the
graph is acyclic by construction and the watchdog in `Session.run` is defense in
depth. The one state in which a task waits *while holding locks* is a parked
commit (§5.3), which the run loop resolves itself.

### 4.4 Project lease (`project_lease.py`)

`ProjectLease` guarantees **one owner per project**. A session takes it as the
first action of `initialize()` and `recover()`, renews it on the heartbeat, and
releases it in `close()`. A second live owner fails fast with `ProjectBusyError`
naming the holder's pid, host, session id, and heartbeat age. `mak gc` takes the
same lease.

On POSIX the lease is an `flock`, which the OS releases when the holder dies
however it dies — no timeout, no pid heuristic. The JSON record inside the file
is diagnostics. On Windows, `msvcrt` byte-range locks can outlive their process,
so that path falls back to a heartbeat-age threshold (`stale_after_s`, 90s); it is
the weaker path and is not exercised by CI (see Wave R in [`TASKS.md`](TASKS.md)).

### 4.5 Derived lock resources and the lock policy

One function builds every lock request — the scheduler's, the commit-time
re-validation's, and the deadlock watchdog's:
`mak/scheduler/lock_policy.py::lock_requests(task, policy)`. With every
`LockPolicy` flag off it returns WRITE on each target and READ on each
`context_node`, which makes the flags a clean ablation switch.
`mak/semantic/locking.py` builds the policy for a wave from config, store, and
code graph.

- **Interface/body split (`semantic.api_locks`).** `resources.py` derives two
  resources per node: `<id>#api` (its interface — signature, decorators, bases,
  fields, imports; whatever `api_digest.api_fingerprint` renders) and `<id>` (its
  body). A caller takes READ on `X#api`; a task that declared a body-only edit
  (`changes_api=False`) takes only `X` and runs beside X's callers. An undeclared
  task (`changes_api=None`) takes WRITE on every target's `#api`.
- **Intention locks (`semantic.intention_locks`).** A fragment write also takes
  INTENT_WRITE on its file id, and a method/`class_body` write on its class node.
  A whole-file or whole-class write needs WRITE at that level, which conflicts
  with any INTENT_WRITE below it — so a whole-file rewrite cannot start beside a
  fragment writer, while fragment writers of one file run together.
- **Key-level registry locks (`semantic.registry_keys`).** A *registrar* — a
  function whose body is a flat list of `callee("<literal>", …)` calls, detected
  structurally by `mak/node_store/registrar.py`, never by name — is commutative
  when every entry is keyed. An appender takes INTENT_WRITE on the node and WRITE
  on `<id>#key=<literal>` for each declared key (`SubTask.registry_keys`). An
  unkeyed list is order-dependent and keeps the plain node lock.

`Scheduler` accepts an optional `lock_policy` (`use_lock_policy` rebuilds it after
recovery), and `Session._granted` records the mode each task actually holds per
resource, so release and commit-time re-validation check the real mode.

## 5. Conflict detection

Node-level write locks guarantee that no two agents write the same node at once —
a textual guarantee. Conflict detection covers the rest, in layers: structural
checks on each commit, semantic checks at commit time, whole-repository checks at
wave end, and optional gates. The whole design follows one rule: **precision over
recall.** A false conflict fails a task, its retries, and everything depending on
it; a missed one is what tests are for.

### 5.1 Structural checks (`mak/conflict_detector/`)

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

### 5.2 Wave-end checks

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

### 5.3 Semantic conflicts (`mak/semantic/`)

Two edits on *disjoint* nodes can each be correct and wrong together. MAK
prevents what it can at scheduling time, detects the rest at commit and at wave
end, and resolves by re-dispatch or fix-up. The taxonomy MAK is measured against
(ten shapes, §Part III) drives the design.

#### Prevention

- **Read-set versioning (`read_set.py`).** For **every** context key a bundle
  carries, a `ReadMark` records the node's committed version and a **content
  digest** — the digest is the identity that matters, because an uncommitted or
  retired-and-recreated node restarts at version 1 with different content.
  `build_read_set` derives the set from the enriched context, so a new
  enrichment layer is covered automatically. It is captured on the dispatching
  thread and persisted with the task graph (`Scheduler.annotations`).
- **The lock refinements in §4.5.**
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
- **Plan validation reads the declarations** (§9): it relaxes an edge a body-only
  declaration made unnecessary, adds an edge from a declared API change to every
  task that names the node, orders a class's structure writer before its other
  members, and flags duplicate registry keys and appends to an ordered
  (unkeyed) registrar.

#### Detection at commit

`Session._validate_and_commit` runs, in order, and any step can reject or defer:

1. **Keyed-registrar merge** (`registrar.py`, `registry_merge.py`). Two appenders
   each return the table as they read it plus their lines. `plan_merge` extracts
   what an agent **appended** to the version it read (`appended_entries`) and
   replays exactly those entries onto the table as it is **now**
   (`merge_append`) — a textual splice that preserves formatting. Anything that
   is not a pure keyed append takes the node's plain WRITE lock instead, or is
   sent back with a fresh read.
2. **Stale-read validation** (`stale.py`). Every read-set node is compared by
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
3. **Structural checks** (§5.1).
4. **Contract check** (`Session._contracts_hold`).
5. **Interface enforcement** (`resources.py`). A task that declared
   `changes_api=False` and changed an existing binding is refused. Any other
   undeclared interface change needs `#api` WRITE: taken on the spot if free
   (`API_ESCALATED`), otherwise the commit is parked until concurrent readers
   finish.

**Parked commits.** A finished result that cannot commit *yet* — waiting on a
registrar's exclusive lock, an `#api` reader, or a contract provider — is parked
(`_park`, `COMMIT_DEFERRED`), not re-run: the work is fine, only the timing is
wrong. Every batch completion retries the parked set (`_resume_parked`). If every
in-flight task is parked, the run loop releases the highest-id one — re-gated on
its dependencies if it waited on a contract provider
(`Scheduler.wait_for_dependencies`), re-dispatched with a note otherwise.

#### Detection at wave end

`Session.detect_cascade_tasks()` assembles fix-up work from three sources, folded
into one task per node (`_merge_fixups`):

- **Cascade on the reference graph** (`cascade_graph.py`). The wave's changes are
  diffed as **symbols** (`symbols.py::diff_symbols` — signature change, deletion,
  or body change, whether the file is stored as fragments or whole). Callers are
  found by walking both the pre-wave and post-wave reference graphs, same-file
  callers included. A caller whose calls are provably compatible with the new
  signature is left alone. A deleted symbol's fix-up names a same-bodied symbol
  the wave added as a rename hint.
- **Cross-module defects** (§5.2). Each fix-up names the task(s) whose work met
  in the defect and carries a bounded diff of **both sides** (`_pair_context`).
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
    log (`_wave_fragments_before`, `_wave_commit_log`).
  - **`import_smoke.py`** (`semantic.import_smoke`) imports each touched module
    in a fresh subprocess before and after the wave.
  - **`adjudicator.py`** (`semantic.adjudicator: "<backend>:<model>"`) asks a
    model whether a dependent's use still holds, for a stale read the static
    checks could not settle. Budgeted (`adjudicator_max_calls`) and logged
    (`ADJUDICATION`). It can only turn an uncertain re-dispatch into an accept;
    any other answer leaves the re-dispatch standing.

`mak/semantic/sources.py` provides the repository as a lazily assembled
file → source mapping, so commit-time checks do not pay O(repo) up front.

## 6. Scheduler

`mak/scheduler/` turns a plan into running work.

- **`dag.py`** — `DAG` validates at construction: unique ids, known dependencies,
  no self-edges, acyclic (Kahn) — `SchedulingError` otherwise. Exposes a
  deterministic `topological_order()`, `mark_complete()`, and `newly_unblocked()`
  (each task handed out once). Soft dependencies (§5.3) are satisfied for dispatch
  but still respected by ordering.
- **`scheduler.py`** — `Scheduler.tick()` drains the ready queue under **atomic
  lock pre-allocation**: all of a task's lock requests (from `lock_requests`,
  §4.5) are acquired in one `try_acquire_all`, or the task stays ready for the
  next tick. Partial acquisition never happens. `on_task_complete` releases locks
  and extends the ready queue; `on_task_failed` optionally re-queues. State
  persists to `.mak/task_graph.json` after every transition (atomically);
  `from_persisted(...)` rebuilds it for recovery, including `context_nodes`, read
  sets, lock policy, and annotations. An unparseable graph raises
  `SchedulingError`. Collaborators are injected behind `Protocol`s.

## 7. Agent runner and adapters

`mak/agent_runner/` is the boundary to agents. **The kernel never calls a model
API directly** — it speaks to an `AgentAdapter`.

### 7.1 The adapter interface

- `AgentAdapter` (`adapters/base_adapter.py`) — `format_task(bundle) -> str`,
  `parse_result(raw) -> TaskResult`, `health_check() -> bool`, and optionally
  `health_detail()` (why a check failed; read with `getattr`).
- `SubprocessAgentAdapter` — adds `spawn(working_dir) -> Popen` for CLI agents.

### 7.2 API adapters (primary)

Direct API calls return structured JSON natively, and a single structured call
matches MAK's agent contract: one node in, one strict `TaskResult` out. Autonomous
file-editing agent loops would bypass the node store and lock manager.

| `agent_type` | Backend | Structured output |
|---|---|---|
| `anthropic_api` | Anthropic Messages API (streamed) | `tool_choice` pinned to a `submit_task_result` tool |
| `openai_api` | OpenAI Chat Completions | JSON schema / JSON mode, per the capability ladder (§8) |
| `gemini_api` | Google GenAI `generate_content` | function calling in `ANY` mode restricted to `submit_task_result` |
| `local_api` | any OpenAI-compatible server | same class as `openai_api`, `base_url` required (§7.8) |
| `ollama_api` | native Ollama API | `format` = JSON schema, compiled to a grammar (§7.8) |

Each adapter imports its SDK lazily and accepts an injectable client, so tests
never make a real call. `result_schema.py` renders the one `TaskResult` schema in
four dialects (`anthropic`, `gemini`, `openai` strict, `ollama`), so the schema
exists once.

Every system prompt states three contracts from `protocol.py`:

- `NODE_ID_CONTRACT` — copy ids verbatim from `target_nodes`; return a bare-path
  target as one complete file.
- `NO_CHANGE_CONTRACT` — a no-op counts only when `no_changes_required` is set.
- `RETRY_NOTE_CONTRACT` — follow a bundle's `retry_note` rather than repeating
  the failed attempt.

### 7.3 Output budget and stop signals

A truncated reply and a deliberate "nothing to change" look the same once
decoded, so every adapter checks the provider's stop signal **before** reading
the payload.

- `adapters/budget.py::resolve_agent_max_tokens(model)` — floor 8192, ceiling
  32000, fallback 16384 for an unknown model. Declares
  `TRUNCATION_STOP_REASONS` (`max_tokens`, `length`, `MAX_TOKENS`) and
  `REFUSAL_STOP_REASONS` (`refusal`, `content_filter`, `SAFETY`, `RECITATION`,
  `PROHIBITED_CONTENT`, `BLOCKLIST`).
- `stop_signals.py` — `check_stop_reason` raises `AgentTruncatedError` /
  `AgentRefusedError`; `extract_usage` normalizes each provider's token fields;
  `with_response_metadata` merges stop reason and usage into the payload so even
  a good attempt carries them.

| Adapter | Budget sent | Stop signal |
|---|---|---|
| `anthropic_api` | `resolve_agent_max_tokens(model)`; streaming is required at this size | `stop_reason` |
| `openai_api` / `local_api` | only if `max_tokens` is configured (`max_completion_tokens` for cloud OpenAI, per the endpoint's `token_parameter` otherwise) | `finish_reason` |
| `gemini_api` | only if configured (`max_output_tokens`) | `candidate.finish_reason` |
| `ollama_api` | `num_predict` = the resolved budget | `done_reason` |

`AgentConfig.max_tokens` overrides the budget per agent.

### 7.4 CLI adapters (secondary)

`claude_code`, `codex`, and `copilot` share `CliSubprocessAdapter`
(`cli_adapter.py`). Real CLIs do not speak MAK's newline-JSON protocol, so each
adapter launches a **bridge wrapper** — `python -m
mak.agent_runner.wrappers.<name>` — which reads a `TaskBundle`, prompts the CLI
for each target's rewritten source as strict JSON, runs the CLI
non-interactively, and writes back a `TaskResult`. The config `cmd` selects the
binary (`--cli <binary>`); `MAK_<AGENT>_CMD` (e.g. `MAK_CLAUDE_CODE_CMD`) replaces
the whole command line. They can be Docker-sandboxed (§7.7).

### 7.5 Registry and composition root

- `AdapterRegistry` (`registry.py`) is an **instance**, never module-global
  state, **keyed by agent id**. `register` / `register_factory` raise
  `ConfigError` on a duplicate id; `replace_factory` exists for tests;
  `list_ids()` returns every id (`list_types()` is a deprecated alias).
- `mak/bootstrap.py` is the composition root:
  - `build_registry(config, …)` registers a config-bound factory per agent,
    binding model, resolved credential, timeout, and endpoint settings. It makes
    **no network call**. The capability lookup is injected (§8).
  - `default_agent_type(config)` — the first configured agent.
  - `validate_config(config)` — rejects unknown types and fields set on a type
    that ignores them.
  - `agents_from_specs(specs, *, env, endpoint_ids)` — parse
    `provider[:model][@url]` roster specs (§13). `env` is where an
    `ollama:`/`local:` spec without a URL reads `$MAK_LOCAL_BASE_URL`;
    `endpoint_ids` is the set a prefix may name.
  - `configured_endpoint_ids(config)` — the user store's endpoint ids plus
    `config`'s own; the caller that holds the run's config passes it, so a
    project config discovered from a work dir is honoured.
  - `planner_from_spec(spec, planner)` — a compatibility wrapper over
    `PlannerRoute.from_spec(...).apply(planner)` (§13); the grammar lives once.
  - `resolved_agents(config, *, env)` — resolves the roster against the merged
    endpoint set and the given environment.
  - `healthy_agent_types(registry, types)` — the startup health preflight. An
    unhealthy agent is dropped with a warning naming why (`health_detail`); the
    run aborts if the default agent is unusable.
  - `seed_capabilities` — seeds the capability cache from the model catalog.
- The bootstrap builds pieces; `mak/application/` (§13) is what assembles a
  whole `Session` from them, for both front ends.

### 7.6 The wire protocol (`protocol.py`)

`TaskBundle` / `TaskResult` serialized as newline-delimited JSON,
`protocol_version` `"1.0"`. `decode_task_result` accepts `modified_nodes` (ids
only), a `modified_fragments` array of `{node_id, new_source}`, or a
`new_sources` map, and normalizes them into `new_sources`. It is hardened
against the shapes models actually produce:

- a lone `modified_fragments` object is coerced into a list, and a JSON-encoded
  string is parsed once; any other non-array is rejected;
- every fragment must be an object with a non-empty string `node_id`;
- a missing `task_id` / `success` is a named failure, not a `KeyError`;
- error messages include a bounded excerpt of the rejected value.

No malformed shape escapes as a raw `TypeError` or `KeyError` — every one is an
`AgentProtocolError`, which keeps the provider's `stop_reason` and `usage`.

`map_returned_sources(grant, new_sources, order_key=None)` enforces the
node-granularity contract for both the session and the CLI bridge (§3.4). A
whole-file rewrite returned under a fragment grant is refused, because it would
touch nodes owned by other tasks.

### 7.7 The runner and sandbox

`AgentRunner.assign(adapter, task, working_dir=None)` (`runner.py`) is the single
entry point. `work_dir` is fixed at construction.

- **API adapters:** `format_task → send → parse_result`, classifying failures
  three ways: `AgentResponseError` (provider answered; reply rejected — its
  `stop_reason`, `usage`, and `retryable` flow through), any other exception from
  `send` (`"api call failed: …"`), and any other exception from `parse_result`
  (`"could not decode agent result: …"`).
- **Subprocess adapters:** an idle-process pool per agent; write a JSON line,
  read the result under a timeout (tolerating preamble and pretty-printed JSON),
  SIGTERM on timeout, discard a process on failure. `max_instances` caps the
  retained pool.

Every path returns a `TaskResult`. Each API adapter takes a request `timeout`
from `AgentConfig.timeout` (the Gemini SDK takes milliseconds; the adapter
converts). `Session.close()` calls `agent_runner.shutdown()`, and on the abnormal
path shuts the thread pool down with `cancel_futures=True`; the per-request
timeout is what bounds a call already in flight.

`sandbox.py::SandboxConfig.wrap(argv, working_dir)` builds a `docker run` argv
that bind-mounts the work dir and defaults to `--network none`. `--sandbox`
applies it to CLI adapters; `docker_available()` fails fast when Docker is
missing.

### 7.8 Local transports

- **`local_api`** — the `OpenAiApiAdapter` class registered under its own type,
  `base_url` required (MAK never guesses a port).
- **Credentials are never leaked.** With a `base_url`, the adapter sends the
  configured `api_key_env` value, or the placeholder `"local"` — always
  *something*, so the SDK can never fall back to an ambient `OPENAI_API_KEY` and
  post it to an unrelated host. `test_a_real_openai_key_in_the_environment_is_never_forwarded`
  guards this.
- **Parse → repair → retry** (`repair.py::repair_loop`, shared by the
  OpenAI-compatible and Ollama adapters). A reply that fails to decode gets one
  short follow-up turn containing the model's own reply plus
  `protocol.REPAIR_INSTRUCTION`, bounded by `agents[].repair_attempts` (default
  `1`, `0` disables). Never after a truncation or refusal. Usage is summed across
  turns and `TaskResult.repairs` records the count, so the spend ceiling sees
  every token.
- **Malformed bodies are `protocol` failures**, so the retry note restates the
  schema.
- **Health.** With a `base_url`, `health_check` probes the server once (5s) and
  `health_detail()` explains a failure ("Ollama is not running at …", "model …
  is not pulled").

**`ollama_api`** (`ollama_api_adapter.py`) is a native adapter because Ollama's
runtime context defaults to a few thousand tokens and **silently truncates** an
over-long prompt. The adapter:

1. reads the model's real context length from `/api/show` (cached per instance);
2. sizes `options.num_ctx` to the bundle — `min(model_context_length,
   round_up(estimate_tokens(prompt) * 1.25 + num_predict))`, floor 4096, where
   `estimate_tokens` is `len(prompt) / 4`;
3. raises `AgentContextExceededError` (non-retryable) when the bundle cannot fit,
   naming the sizes and the settings that fix it. A configured `num_ctx` is used
   verbatim as a hard ceiling.

`keep_alive` keeps the model resident between tasks; `health_check` verifies both
the server (`version()`) and the model (`list_models()`).

## 8. Endpoints

`mak/endpoints/` lets MAK talk to any OpenAI-compatible service and to several of
them in one run. Four identities are kept separate:

| Identity | What it is |
|---|---|
| **transport** | the wire protocol an adapter speaks (`openai_chat`, `anthropic`, `ollama_native`, …) |
| **profile** | a named set of documented defaults — URL, key variable, capabilities |
| **endpoint** | one configured service: URL, credential *reference*, capability settings |
| **agent id** | the routing key every scheduler, planner, log line, and git trailer uses |

`type` only selects a constructor. Every agent — endpoint-backed or built-in —
resolves through the same path, via synthesized built-in endpoints for the three
hosted providers and the two local transports (`builtin.py`). Reserved ids
(`anthropic`, `openai`, `gemini`, `google`, `local`, `ollama`) cannot be taken by
a user endpoint, so a spec prefix has exactly one meaning.

| Module | Role |
|---|---|
| `types.py` | leaf module: `Transport`, `Location`, `ModelDiscovery`, `HealthPolicy`, `StructuredOutput`, `TokenParameter`, `ProviderRouting`, `EndpointConfig`. No I/O |
| `profiles.py` | the presets (`nvidia`, `openrouter`, `deepseek`, `zai-general`, `zai-coding`, `custom`) — **the only place** preset URLs and key names may appear; a test enforces it |
| `parse.py` | YAML → `EndpointConfig`; materializes identity, **defers** capabilities so a corrected profile default reaches everyone on `auto` |
| `resolution.py` | precedence at use time: explicit field > profile default > transport default. `None` falls through; an explicit `"none"` stops the walk |
| `agents.py` | `resolve_agents`, `derive_agent_id`, `unique_agent_id` |
| `store.py` | per-user `~/.config/mak/endpoints.json`, atomic `0600`, schema-versioned; merged with project `endpoints:` (project wins on a matching id) |
| `health.py` | failure classification (credentials, not found, rate limited, incompatible, unreachable, SDK missing, model missing, unknown) with secrets redacted; policies `models` / `chat` / `none`. "Not probed" is distinct from "healthy" |
| `capabilities.py` | the structured-output ladder and `CapabilityCache` |
| `error_classification.py` | why a structured request was refused |

**Credentials are names, never values.** `api_key_env` names an environment
variable. An endpoint with none gets the non-secret placeholder. Extra `headers`
take `value` or `value_env`, never both, and may not set `Authorization`,
`Content-Type`, `Host`, or `User-Agent`.

### Structured-output negotiation

The ladder is `json_schema → json_object → none`. Which rung to use is decided by
two kinds of evidence, held in two separate fields of the session-scoped
`CapabilityCache` and never merged:

- **Reported** capabilities — from the model catalog (`supported_parameters`,
  §15). A claim: it lowers the **starting** rung and leaves descent available.
  `structured_outputs` authorizes `json_schema`; `response_format` authorizes
  `json_object`.
- **Proven** mode — a real successful call. It pins the rung exactly.

Both are clamped to the agent's configured `structured_output`: **a seed lowers,
never raises.** The reported set is **tri-state**: `None` (nothing published —
unknown), `frozenset()` (published and empty — also unknown), or a non-empty set
(a non-empty report that omits a parameter is a known negative). Cache keys use
the **whole** model id; `model` and `model:free` can have opposite capabilities.

`CapabilityCache` is owned and injected by the composition root — not a module
global — so two sessions never share what one learned. `discovering()` makes
discovery single-flight: the first caller for an unknown `(endpoint, model)` owns
the ladder, others wait on a bounded per-key event and start from what it proved;
no network call happens under the cache lock; a failed owner does not poison the
key.

**Descending requires a classified refusal.** `error_classification.py` parses,
in order, the HTTP status, `exc.body`, `response.json()`, the provider's
`error.message` / `code` / `metadata`, `error.metadata.raw` (JSON-decoded if it is
a JSON string), and `str(exc)` last. Text is NFKC-normalized, casefolded, and
non-alphanumeric runs collapse to one space, so `structured outputs`,
`structured-outputs`, and `structured_outputs` match one marker. A capability
refusal needs **all three**: a 400/422 status, a marker naming the reply format,
and language asserting an absent capability. Invalid schemas, auth, missing
models, quota, context limits, safety, transport errors, and 5xx all propagate.
The verdict is a typed `RejectionAnalysis`.

**Provider routing guard.** `provider_routing: openrouter` (set only by the
`openrouter` profile, never inferred from a hostname) adds OpenRouter's
`provider.require_parameters` via the SDK's `extra_body` — only on a rung the
catalog **positively** confirms. Its failure is a 404
(`failed_routing_step: "Filter by Parameters"`), so a guard 404 retries the
**same** rung without the guard rather than descending.

**Invariants to preserve:**

- Never widen the marker tuples to match a new provider's wording; fix the
  normalization or the parse order instead.
- An exception without an HTTP status is never a capability rejection.
- Unknown, empty, and reported are three states; `None` must never become
  `frozenset()` on any hop.
- Only positive confirmation sends the routing guard.
- Raw provider bodies are read in memory and never logged; only the short,
  `redact_secrets`-filtered reason (≤200 characters) reaches a log line.

**Debugging a structured-output problem:** `INFO` logs the chosen rung per
endpoint/model with its evidence (`catalog` or `runtime_rejection`); `DEBUG` logs
a dropped routing guard. To see what OpenRouter claims, read
`supported_parameters` for the exact id from `GET /api/v1/models` (per-route
detail at `/models/<id>/endpoints`). `MAK_NO_MODEL_REFRESH=1` freezes the catalog
to separate seed problems from negotiation problems.

## 9. Planner

`mak/planner/` decomposes a task into a validated `SubTask` DAG.

**`planner.py`** — `Planner.decompose(user_task, node_inventory)` builds a prompt
with the task, the node inventory (qualified names only, never source), and the
configured agent types, calls an injected `PlannerLLM`
(`complete(prompt) -> str`), and validates the result with `parse_plan`. The
parser accepts a bare array or `{"subtasks": …}`, strips code fences, validates
each `SubTask`, and rejects duplicate ids and unknown dependencies. The prompt's
**cascade prevention** rule asks the model to include a sub-task for every caller
of any function whose signature it changes. Target rules (each raises
`ValueError`, which is fed back to the model on retry):

- **Containment** first — a target must resolve inside the work dir
  (`unsafe_node_id_reason`).
- **Python-only targets** (`is_python_target`) — MAK has no AST node for other
  files.
- **One whole file, one task** — a whole-file target is owned by exactly one task.
- **One granularity per file** — a file is targeted whole or by symbols, never
  both.
- **Declarations** (`_coerce_subtask`) — `api_targets` / `contract` /
  `registry_keys` must name the task's own targets, a contract must parse and name
  the right symbol, and `changes_api: false` cannot accompany a contract. Setting
  `api_targets` or `contract` with `changes_api` null means `true`.

Retries (`max_retries`) cover the LLM call too: transient provider failures back
off exponentially (1s, 2s, 4s, capped at 8s); a `PlannerFailedError` from the
backend (missing SDK, refusal) is re-raised immediately; a rejected plan is
re-asked at once; a truncated reply is re-asked for a **smaller** plan.

Two opt-in refinements, off by default:

- `planner.strategy: outline` — a file-level outline call, then one detail call
  per step with the inventory restricted to that step's files. Ids are namespaced
  `s<k>.<id>`; an outline edge S1 → S2 makes every S2 task depend on every S1 task.
- `planner.self_critique: true` — one reflection call that returns
  `{"verdict": "ok"}` or a corrected plan. A failed critique keeps the original.

The order in `Session.plan()` is decompose (+ critique) → validate → review.

**`response.py`** — `loads_json` strips a fence found anywhere, skips framing
prose, and parses with `raw_decode`. On failure, `repair_truncated` distinguishes
**truncated** from **malformed** by closing open delimiters; a truncation raises
`TruncatedResponseError` with `complete_elements`. The repaired text is never
used as a plan.

**`llm.py`** — `PlannerLLM` backends: Anthropic (streamed), OpenAI, Gemini, and
`OllamaPlannerLLM`. Each reports a provider-signalled cut as
`TruncatedResponseError` before parsing; an Anthropic `refusal` raises
`PlannerFailedError`. `resolve_max_tokens(model)` delegates to
`resolve_output_budget` with a 4096–32000 clamp and 16384 fallback.
`OllamaPlannerLLM` sizes `num_ctx` like the agent adapter and raises when the
inventory cannot fit. `build_planner_llm(model, *, backend, base_url, api_key,
timeout)` picks the backend: explicit `backend` first, then `base_url` →
OpenAI-compatible, then the model-id prefix. `Planner.token_usage` records what
each call reported.

**`depgraph.py`** — `build_dep_graph(sources)` produces a `DepGraph` of
`references` (node → nodes it calls or reads) and `definers` (symbol → defining
nodes). Resolution is shallow and conservative: same-file calls, and cross-file
references only through a parsed import table to a uniquely resolvable file.
Anything ambiguous yields no edge. `dep_graph_from_store` rebuilds it from
committed state on every `install_plan`; `resolve_module_file` is the strict
module resolver the wave-end checks share.

**`validation.py`** — `validate_plan(plan, graph, inventory, semantics=None)`
returns a corrected copy plus `PlanFinding`s. The policy is asymmetric:

| Finding | Action |
|---|---|
| `missing_dep` | add the edge if acyclic-safe (mutual pairs are reported, not applied); also added when a task's context names a node another task creates |
| `corrected_node` | fix a hallucinated id only on one confident match (wrong kind segment, case/underscore variant, missing `Class.` prefix, or `difflib` ratio ≥ 0.9) |
| `unknown_node` | weak or multiple candidates — suggest, do not change |
| `spurious_dep` | flag a declared edge with no reference either way; never remove |
| `context_dropped` | drop an unknown context node — unless another task in the plan targets it |
| `relaxed_dep`, `declared_api_dep`, `shared_structure`, `ordered_table`, `registry_key_collision` | the declaration-driven rules from §5.3 |

A correction that would violate a `parse_plan` invariant is downgraded to a
suggestion.

**`contracts.py`** — `parse_contract`, `contract_stub`, and
`implementation_mismatch` (§5.3).

**`review.py`** — `display_plan_for_review` renders subtasks, edges, findings,
and repair obligations, and loops **approve / edit (paste JSON) / abort**
(`PlanReviewAborted`). I/O is injected. An optional `header` labels cascade waves.
`--no-review` skips it.

## 10. Git integration

`mak/git_integration/git.py` treats Git as an **audit log**. Lock discipline
already prevents conflicting writes, so commits go directly to the working branch.

- **`commit_task(task_id, files, description, agent_type, session_id)`** commits
  exactly `files` with a `[MAK-<task_id>]` subject and a Files/Status/Agent/Session
  body, or returns `None` if they match HEAD. The commit is built in a **private
  index** (`GIT_INDEX_FILE` = `.git/mak-index-<uuid>`, seeded from HEAD, or empty
  on a repo with no HEAD), so the user's staged changes are never swept in and
  the user's index is never touched. Afterwards `git update-index --add` re-stats
  only the committed paths so `git status` stays clean.
- **`validate_clean_state()`** — enforced when `git.require_clean_tree` is on
  (off by default).
- **`ensure_initialized()`** — when `auto_commit` is on, guarantees the work dir
  is its **own** repo (running `git init` if it is nested in an outer repo or in
  none), setting a local identity only if git has none.
- **`get_session_commits(session_id)`** and **`push(branch, remote)`**.

Every operation raises `GitIntegrationError` with stderr on failure.

## 11. Session lifecycle

`mak/session.py` wires everything together behind a `SessionState` machine:
`CREATED → INITIALIZED → PLANNED → RUNNING → {COMPLETED | FAILED | ABORTED}`.
All collaborators are injected behind `Protocol`s.

### Initialize

Four ordered steps, then ingestion:

1. **`_acquire_project()`** — take the project lease before anything reads or
   mutates `.mak/`.
2. **`_recover_journal()`** — resolve any commit an interrupted run left in
   flight.
3. **`lock_table.clear()`** — sound because the lease proves the prior owner is
   gone.
4. **`_reconcile_work_dir()`** — load `.makignore`, prune stored nodes whose file
   is no longer ingestable (reported as `pruned_nodes`), and synchronize the store
   with the working tree via `NodeStore.sync_file`: changed fragments advance to
   their next version, unchanged ones are left alone, symbols that disappeared
   are **retired**, and a file gone from disk retires all its nodes.

MAK's own `mak_dir` is skipped unconditionally (`_is_store_path`), independent
of `exclude_patterns`. A file whose content differs from the digest MAK recorded
when it last wrote it was edited by someone else; `session.on_external_edit`
decides: `adopt` (default — the working tree is the newer truth) or `conflict`
(raise `WorkTreeConflictError` before planning).

### Plan

`propose_plan(user_task)` runs the planner and deterministic validation and
returns a `PlanProposal(subtasks, findings)` without reviewing or installing it —
the public entry point for a front end that reviews the plan its own way (the
interactive app). `plan()` is `propose_plan` + optional review + `install_plan`.
Nothing outside `mak/` touches a `Session` private attribute.
**`install_plan` always re-validates** — it is the one entry point shared by
`plan()`, the interactive app, cascade waves, and edited review plans:

- containment check on every target (`_reject_unsafe_targets`), naming every
  offender;
- `validate_plan` against a freshly built dependency graph when
  `planner.validate` is on; findings go to `session.last_plan_findings` and one
  `PLAN_VALIDATED` event;
- `agent_type` normalization (`_apply_default_agent`): unassigned tasks are
  distributed **round-robin across the healthy agent pool**; an unconfigured type
  is remapped to the pool's first agent (`AGENT_REMAPPED`);
- an optional `objective=` keyword carries the user's request for repair
  descriptions when the planner is bypassed.

### Run

Dispatch lock-satisfiable ready tasks onto the thread pool, enriching each bundle
(§3.3) and capturing its read set. As results arrive, stage returned sources
(within the grant), batch concurrent completions, run the commit pipeline (§5.3,
§5.1), and commit transactionally.

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
exist and the assembled file compiles (`_is_asserted_noop`). `_noop_refusal`
additionally refuses the assertion when it cannot be true: for a target absent
when the wave was installed, if a task this one directly depends on targets the
same file, or if it is a whole-file grant on the first attempt. An accepted no-op
logs `ACCEPTED_NOOP` and counts in `tasks_noop`, never inflating
`tasks_completed`. A live repair obligation also blocks the no-op path.

**Retries differ from the attempt they follow.** `Session._retry_note` chooses a
note by `error_kind`: a truncation asks for the same work in less output; a
protocol slip restates the schema in full; a stale read carries the diff; anything
else names the reason and asks not to repeat it. A result with `retryable=False`
fails the task immediately rather than spending the remaining attempts.

**Empty results are explained.** `_describe_empty_result` names the actual cause:
a truncation stop reason, ids outside the grant, ids with no source, a missing
target, a file still invalid after "no changes", or an unasserted empty success.

**The spend ceiling.** `session.max_total_tokens` is checked between run-loop
iterations (`_budget_breach`). On a breach MAK stops dispatching, lets in-flight
work finish and commit (`_finish_in_flight`), and reports
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
`stale_redispatches`.

**Wave bookkeeping** for post-wave analysis: `_wave_committed` (old/new source
per node, `None` for a superseded fragment), `_wave_file_before`,
`_wave_file_writers`, `_wave_node_writer`, `_wave_fragments_before`,
`_wave_commit_log`.

### Cascade waves and repair obligations

`mak/cascade.py::run_cascade_waves(session, approve, announce=…)` is the one
post-wave loop both front ends call: detect (§5.3) → announce → approve → install
→ run, until nothing remains, the approver declines, or `max_waves` is reached.

Every cross-module fix-up carries kernel-generated **`RepairObligation`s**. Each
records the finding's exact identity and a stable *family* identity for its
syntactic site (so swapping one nonexistent name for another at the same site is
not progress). Obligations are persisted with the task graph, rendered in review
(`must resolve=`), preserved by `_merge_fixups`, and reattached after an edited
review plan; an edited plan that keeps neither the caller nor the provider is
rejected as unrepairable. Before a repair commits,
`_prospective_semantic_reasons` substitutes the staged sources into a
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
unsanitized subject (`_fixup_task_id`), so two files that sanitize alike cannot
collide.

`CascadeOutcome` carries every wave plus `declined`, `limit_reached`, `stalled`,
`oscillating`, `unrepairable`, a deterministic `stop_reason`, and `unresolved`.
**`ExecutionResult`** (`mak/execution_result.py`) aggregates the initial wave and
the cascade outcome and answers two questions separately: `tasks_completed` (a
statistic) and `request_satisfied` (the verdict that gates exit codes and the
push). **A later wave never clears an earlier failure.** Task ids are namespaced
by wave index.

### Teardown and recovery

`teardown()` runs `session.test_command` through the `TestRunner`
(`mak/test_runner.py`) and returns a `TeardownResult` (`mak/teardown.py`) with
outcome `passed`, `failed`, `skipped`, or `error`. The push gate requires
`git.auto_push`, a git helper, a **satisfied aggregate outcome**, and
`session.test_policy` — `require_pass` (default) or `allow_skip`.
`push_skipped_reason` names whichever gate refused.

`recover()` takes the lease, resolves the journal, expires stale leases, and
rebuilds the scheduler from `task_graph.json`. `mak run --recover` calls it instead
of `initialize()`/`plan()`. A missing or corrupt graph logs
`SESSION_ENDED(recover_failed=True)` and reports "nothing to resume" rather than
raising.

## 12. Configuration

`mak/config.py` loads YAML into a frozen `MakConfig` dataclass tree. The
annotated reference for every key is the packaged default,
[`mak/config.yaml`](mak/config.yaml): each setting is listed with its default and
what it does. The sections it leaves out are shown in the packaged examples under
[`mak/examples/`](mak/examples/) (`mak examples <name>` prints one):

| Example | Shows |
|---|---|
| `custom-endpoint.yaml`, `hosted-openai-compatible.yaml` | `endpoints:` and every capability setting (§8), agents routed by `endpoint` |
| `local-ollama.yaml`, `local-openai-compatible.yaml` | `ollama_api` / `local_api` agents and their fields (§7.8) |
| `hybrid-cloud-planner-local-agents.yaml`, `fully-local-offline.yaml` | planner `backend` / `base_url` / `endpoint` routes |

`tests/test_example_configs.py` loads and validates every example, so they cannot
drift from the schema. When you add or change a config key, document it in
`mak/config.yaml` (or an example), not here.

Rules:

- **Discovery.** Without `--config` / `/config`,
  `discover_config_path(work_dir)` picks the first file that exists:
  1. `<work_dir>/.mak/config.yaml` — the project's own config
     (`project_config_path(work_dir)`);
  2. `~/.config/mak/config.yaml` — the user's config (`user_config_path()`;
     honours `$XDG_CONFIG_HOME`);
  3. the packaged `mak/config.yaml`, when neither exists.

  `work_dir` is the project being edited, not the launch directory: `mak run`
  passes `--work-dir` (else the CWD), `mak gc` its argument, and the app its
  current work dir (`CliState.config_file()`). `seed_config_path()` is the file
  a new project config is copied from: the user's config, else the packaged
  default.
- **`agents`** is required and non-empty. Optional per-agent fields default to
  `None`, meaning "the adapter decides"; an explicit value is validated at load.
  `max_tokens` must be a positive integer; `repair_attempts` may be `0`.
  `structured_output` and `planner.backend` are checked against fixed choices at
  load. `base_url` goes through `normalize_base_url` (scheme and host required,
  trailing slash stripped) — the same rule the command line and the app use.
  `validate_config` rejects local-transport fields on a type that ignores them,
  and `local_api` without `base_url`.
- **Model choice lives in config.** `PlannerConfig.model` defaults to `""` and
  `AgentConfig.model` to `None`; no model name is hardcoded in the dataclasses.
  The app writes config only on an explicit model change.
- **Keys are never stored in config.** `api_key_env` names a variable read at
  composition time from an explicit `env` mapping (§13). Keys live in
  `~/.config/mak/.env` (created `0600`) or the environment; exported variables
  win. The in-package `mak/.env` is deprecated —
  it is still read with a warning (Wave R in [`TASKS.md`](TASKS.md)).
- **`session.max_total_tokens`** is the only spend ceiling (§11). `0` or negative
  is a `ConfigError`.
- **`node_store.version_retention`** must be `-1` or ≥ 2.
- **`session.mak_dir`** is anchored to `work_dir` by `config.anchor_mak_dir`, used
  by both front ends. `config.stale_mak_dir` reports a `.mak` left at a
  CWD-relative location that holds run state (`node_store/`, `task_graph.json`
  or `lock_table.json`); it is never adopted. A `.mak/` holding only a
  `config.yaml` is a configured project, not an orphan.
- **`exclude_patterns`** replaces the default list when set; per-project ignores
  belong in `.makignore`, which adds to it.
- **`semantic:`** — `stale_read` and `type_check` are validated at load;
  `impact_tests` / `import_smoke` accept a boolean or `"on"`/`"off"`;
  `adjudicator` is `"off"` or `"<backend>:<model>"` (built through
  `build_planner_llm`); counts must be non-negative and `gate_timeout_s`
  positive. Locking flags default **on** (they cost nothing extra); gates default
  **off** (each costs subprocesses, test runs, or a model call).
  `Session(gate_runner=…, adjudicator_llm=…)` makes the subsystem testable without
  tools or keys.
- **Model caveats.** `model_caveat(model_id)` returns a warning for models with
  footguns; every surface that selects a model prints it.
- Type coercion is strict (`"false"` parses to `False`) and wrapped in
  `ConfigError`.

## 13. Command line (`mak`)

`cli/__main__.py` is the `mak` console script:

| Command | Action |
|---|---|
| `mak` | launch the interactive app (§14) |
| `mak run --task "..."` | one non-interactive run (`mak/__main__.py`) |
| `mak gc [work_dir]` | apply version retention and remove orphaned fragment directories (takes the project lease) |
| `mak examples [name]` | list or print a packaged config from `mak/examples/` (`config.example_path` refuses names that resolve outside that directory) |
| `mak update` | reinstall the newest release tag via `uv tool install git+…@<tag>` |
| `mak --version` / `--help` | |

`mak update` resolves tags with `git ls-remote --tags` (peeled entries win),
orders them with `_version_key` (a plain release above its pre-releases), skips
the reinstall when already current (PEP 610 `direct_url.json`), and falls back to
`main` — and says so — while the repo publishes no tags. See
Wave R in [`TASKS.md`](TASKS.md) for its open problems.

### The application API (`mak/application/`)

Both front ends turn settings into a session through this package and nothing
else, so the same settings cannot behave differently depending on which one
launched the run. Neither front end writes `os.environ`; every credential read
goes through an explicit `env` mapping.

| Module | Contents |
|---|---|
| `request.py` | `RunRequest` (frozen): `config_path`, `work_dir`, `model_specs`, `planner` (a `PlannerRoute`, or a spec parsed against the loaded config's endpoints), `max_agents`, `default_agent`, `sandbox`, `verbose`, `api_keys`, `no_review` |
| `config.py` | `build_config(request, *, env, anchor=True)` — discover or load the file, apply the overrides, validate, anchor `mak_dir`. `anchor=False` lets `mak run` look for a stale store first |
| `route.py` | `PlannerRoute` — the planner's route as one value (below) |
| `keys.py` | `resolve_planner_key(config, env)` — the **only** planner-key resolver; `planner_endpoint(config, *, env)` |
| `session.py` | `build_session(config, *, env, sandbox, default_agent)` — assembles every collaborator and runs the health preflight |
| `env.py` | `read_env_files()` — MAK's `.env` files as a mapping, nothing exported; `load_env_file()` — the same files `setdefault` into `os.environ`, for the one-shot `mak run` only |

**`PlannerRoute`** is frozen and complete by construction: `kind` is `hosted`
(`provider`, plus an optional gateway `base_url` for `openai`), `endpoint`
(`endpoint_id`), or `local` (`backend` `ollama`/`openai` and `base_url`);
`__post_init__` refuses a missing field or a field of another kind. Build one
with `hosted()`, `endpoint()`, `local()` or `from_spec()`; `spec()` renders it
back. `apply(planner_config)` rewrites **every** route field (`endpoint`,
`backend`, `base_url`, `api_key_env`) and keeps the rest: a direct hosted route
names its provider's key variable, a gateway names none.

**Planner key resolution** (`resolve_planner_key`), in order: the endpoint's
credential; `planner.api_key_env`; the conventional variable of a hosted
`planner.backend` with no `base_url`. A config that names any other backend or a
`base_url` (a local runtime, a gateway) gets `None` — never a cloud key. Only a
config naming no route at all falls back to the model-id prefix.

`tests/application/test_parity.py` builds the same logical settings (hosted,
endpoint, local and gateway planners, a roster with an endpoint,
`--max-agents`) through `mak run` and through the app and asserts an identical
`MakConfig` and planner key.

### `mak run`

`mak/__main__.py` keeps argparse, `main`, reporting and warnings:

- `load_env_file()` loads `~/.config/mak/.env` (and the deprecated `mak/.env`)
  into `os.environ` with `setdefault`.
- `parse_args` — `--task` (required unless `--recover`), `--config`,
  `--work-dir`, `--models`, `--planner`, `--max-agents`, `--agent`,
  `--no-review`, `--recover`, `--sandbox`, `-v/-vv`. `request_from_args(args)`
  turns them into a `RunRequest`.
- `main(argv, *, session_builder=…)` builds the config with
  `build_config(request, anchor=False)`, reports a stale store
  (`stale_mak_dir`), anchors `mak_dir`, builds the session through
  `application.build_session`, then drives initialize → plan → run → cascade
  loop → teardown. `session_builder(args, config, sandbox)` is the test seam.
  Exit codes: `0` success; `1` aborted review, planner failure, unsatisfied run,
  failing tests; `2` config error or missing Docker under `--sandbox`. The
  summary prints completed / failed / skipped / blocked with each failure's
  reasons. Under `--no-review`, cascade waves are skipped with a warning.
  `warn_model_caveats` and `warn_local_planner_mismatch` (all agents local but
  the planner hosted) print on stderr. `mak run` never prompts.

### Spec grammar: `--models` and `--planner`

Both take `provider[:model][@base_url]`. `_split_spec` splits on the **first**
`@` (a URL may carry `user:pass@`) and then the **first** `:` (Ollama tags contain
a colon). The provider position is resolved in order: a configured endpoint id,
then the hosted providers, then `ollama` / `local`.

| Provider | Adapter `type` | Key variable | Default model |
|---|---|---|---|
| `anthropic` | `anthropic_api` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` |
| `openai` | `openai_api` | `OPENAI_API_KEY` | `gpt-5.6-sol` |
| `gemini` (alias `google`) | `gemini_api` | `GEMINI_API_KEY` | `gemini-3.5-flash` |
| `ollama` | `ollama_api` | none | required |
| `local` | `local_api` | none | required |
| `<endpoint id>` | from the endpoint's transport | the endpoint's `api_key_env` | required |

- `@base_url` is accepted on `openai` (a gateway), `local`, and `ollama`; on
  `anthropic`/`gemini` it is a `ConfigError`. `ollama` defaults to
  `$MAK_LOCAL_BASE_URL`, then `http://localhost:11434`; `local` has no default.
- `--models` replaces the whole roster; the first entry is the default agent
  (`--agent` overrides). Several models on one endpoint, and several endpoints on
  one transport, coexist — uniqueness is by **agent id**.
- `--planner` uses the same grammar through `PlannerRoute.from_spec`, with two
  differences: the **model is required**, and **every route field is
  rewritten** by `PlannerRoute.apply`, keeping non-route settings. The provider
  is mandatory because one model id can be served by several providers with
  different keys and bills.
- **`--max-agents N`** sets `max_concurrent_agents` (live concurrency).
  `max_instances` is a different knob: the idle CLI-subprocess pool size.

```bash
mak run --task "..." --work-dir ./proj \
  --models anthropic:claude-opus-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash
mak run --task "..." --work-dir ./proj --models anthropic --max-agents 5
mak run --task "..." --work-dir ./proj \
  --models ollama:qwen2.5-coder:14b --planner openrouter:anthropic/claude-opus-5
```

### Using a local CLI agent

CLI agents are configured by `type` in `agents:` — there is no `--models`
shorthand:

```yaml
agents:
  - type: claude_code       # drives `claude` through the bridge wrapper
    max_instances: 2
    timeout: 300
  # - type: codex
  # - type: copilot         # `gh copilot` — the weakest fit for node rewrites
```

The binary must be on `PATH` (the health preflight runs the wrapper's
`--health-check`). Override the invocation with `MAK_CLAUDE_CODE_CMD`,
`MAK_CODEX_CMD`, or `MAK_COPILOT_CMD`.

## 14. Interactive app (`cli/`)

`cli/` is an inline REPL on `prompt_toolkit` + `rich`. It uses MAK as a library
through public entry points only — the application API (§13) to build the
session, then `session.initialize()`, `session.propose_plan()`,
`session.install_plan()`, `session.run()` — and runs the same cascade loop as
`mak run` (`mak.cascade`), honouring `/no-review`.

**UX.** One accent colour (`ACCENT` in `cli/ui.py`), flat indented lists. A
compact welcome box; a bottom toolbar with live state (models, planner, agents,
work dir, approval, mode, session tokens), so commands print only a one-line
`✓`/`⚠`/`✗`; a `/` completion menu driven by `MakCompleter`; Ctrl+J for a newline.
A task: capture HEAD → build and initialize a session → plan (spinner) → show the
plan → optional approval → run (progress bar) → results → per-file `+N -N` diff
of `{pre_hash}..HEAD`. The token counter reads `Session.total_tokens`.

| Command | Description |
|---|---|
| `/models [spec …]` | select agents with the `--models` grammar; in local/hybrid mode a bare `/models` lists the runtime's models live |
| `/planner [spec]` | set the planner with the `--planner` grammar; a bare id is refused with every matching spec suggested; a hosted model is checked against that provider's catalog |
| `/refresh-models` | re-fetch the cloud catalog now, printing a per-provider diff |
| `/local [sub-command]` | local runtime setup (below) |
| `/mode [cloud\|local\|hybrid]` | show or switch how the session gets models |
| `/endpoint [sub-command]` | manage OpenAI-compatible endpoints (below) |
| `/max-agents N` | concurrency limit |
| `/work-dir <path>` | working directory; offers a project config (below) |
| `/apikey` | add or update API keys |
| `/config [path]` | use a config file; bare returns to discovery from the work dir |
| `/no-review [true\|false]` | toggle plan approval |
| `/status`, `/help`, `/clear`, `/exit`, `/quit` | |

**Planner route state.** `CliState.planner` is one `PlannerRoute` (§13),
defaulting to `anthropic:claude-opus-5`. Every setter —
`set_cloud_planner(provider, model)`, `set_endpoint_planner(endpoint_id, model)`,
`set_local_planner(backend, model, base_url)` — assigns a whole new route, so
there is nothing to clear and no stale field can misroute the planner.
`planner_model`, `planner_backend`, `planner_base_url` and `planner_endpoint_id`
are read-only views for display code. `planner_spec()` renders the route as
`provider:model[@url]` everywhere it is displayed (a local planner on the active
host omits its `@url`). `/local off` with a local planner falls back to
`cli.core.models.default_planner_route` — the recommended planner of the first
provider with a key. The retired-model warning matches on `provider:model`.
`/local planner <model>` takes a bare name because the active runtime already
names the provider. `tests/application/test_route.py` asserts that any sequence
of setters leaves exactly one route kind.

**Modes.** `CliState.mode` is `cloud`, `local`, or `hybrid`, shown in the toolbar
and `/status`. It decides which surfaces validate against keys and which against a
local runtime; the roster is always `selected_models`, parsed through
`agents_from_specs`. First run (`cli/setup.py::run_setup`) runs a background
`discover()` and asks which mode to use: **Cloud** runs the key wizard, **Local**
runs the `/local` wizard with no key, **Hybrid** asks for a planner key then runs
`/local` for agents. The app reaches the prompt with no API key at all.

**`/local`** (`cli/local.py`): bare `/local` detects runtimes, picks one, picks an
agent model (installed models with size and quantization, or curated suggestions
pulled with a progress bar), picks a planner (recommended from the curated
table's `is_small()`), reports context fit against the configured context budgets,
and confirms. It then offers `Save this setup to .mak/config.yaml? [y/N]`
(default no), writing `<work dir>/.mak/config.yaml` with the planner's route
(`backend`/`base_url`, or `endpoint:`) and round-tripping it through
`load_config` + `validate_config`. Sub-commands: `status`, `models`,
`use <model> […]`, `planner <model>`, `pull <model>` (interruptible, resumable),
`url <base_url>`, `off`. Connected hosts persist in
`~/.config/mak/local_hosts.json` (`cli/core/local_hosts.py`). An unreachable server
is one red line, never a traceback. Discovery, the Ollama client and the
saved-host probe are a `LocalSeams` value (`cli/core/local_seams.py`) held on
`CliState.local_seams`; a test builds a state with fake seams, and no module
state changes.

**`/mode`** refuses to switch into a mode that is not usable yet ("run `/local`",
"run `/apikey`").

**`/endpoint`** (`cli/endpoints/`): `list`, `add`, `show`, `edit`, `test`,
`models`, `remove`, `export`, `help`. `add` is a gather-then-commit wizard — a
`CANCELLED` sentinel at any step discards the whole draft. Presets prefill from
`profiles.py`. `export <id>` prints secret-free YAML. `test <id>` runs the health
policy on demand (the only place `/endpoint` spends a request). Endpoints persist
in `~/.config/mak/endpoints.json`. Credentials are asked for as **variable
names**; `/apikey` writes values.

**Project config bootstrap** (`cli/project_config.py`). On startup, and after a
successful `/work-dir` (which returns the `"work_dir"` action to the loop), the
app calls `offer_project_config`: if the work dir has no `.mak/` and no explicit
`/config` is set, it asks `Create .mak/config.yaml here from <seed>? [y/N]`.
Yes copies `seed_config_path()` (the user's `~/.config/mak/config.yaml`, else
the packaged default) and never overwrites an existing file; no writes nothing, and
the run creates `.mak/` for its state on demand. The question is an injectable
`confirm` callable.

**Session-only configuration.** Slash-command changes live in `CliState` and are
never written to a config file, with three explicit exceptions, each behind a
yes: the project-config offer, `/local`'s save prompt, and `/endpoint`
(endpoints are reusable infrastructure). `cli/runner.py` is the bridge:
`request_from_state(state)` describes the run as a `RunRequest` — always an
explicit, resolved work dir, the session's `PlannerRoute`, `selected_models` as
the roster, `max_agents` — and `config_for(state)` / `build_session(task,
state)` run it through `build_config` and `application.build_session`. New CLI
state that affects a run belongs in `request_from_state`. The app's planner
route always replaces the config file's. `plan_in_thread` calls
`session.propose_plan`; `run_session_in_thread` runs `session.run()` on a thread
and joins it.

**Services on the state.** `CliState` is the one object the app passes to every
handler, the completer, setup and the UI, so it also carries the app's services:
`local_seams` and a lazily built `ModelRegistry` (`state.models()`, or an
injected `model_registry`). `state.config_file()` is the session's config —
explicit or discovered from the work dir — and every app-side config read
(endpoints, context budgets, auto-refresh) goes through it.

**API keys** (`cli/core/api_keys.py`): loaded from `~/.config/mak/.env`, then the
deprecated `mak/.env`, with exported variables winning. A session never exports
them: `cli/runner.session_env(state)` passes `read_env_files()` overlaid with
`os.environ` and the session's keys to the application API as `env`. `save_keys` accepts any variable name — it
parses the existing file, changes only the names it was asked to, and writes
atomically at `0600`. `key_names_for(endpoints)` lists the names configured
endpoints need.

**Adding a slash command:** add a handler in `commands.py` (print a one-line
`print_ok` / `print_warn` / `print_error` if it mutates state), register it in
`handle_command()`, add it to `COMMANDS` in `completer.py` (drives both the menu
and `/help`), and add argument completions to `MakCompleter`. Return `"exit"` or
`"clear"` (or `"work_dir"`) for commands the main loop acts on. The loop runs
each command through `MakCli._dispatch_command`: an unexpected exception prints
one `✗ /cmd failed: …` line and returns to the prompt, with the traceback logged
at `DEBUG` on `cli.app`; `KeyboardInterrupt` and `EOFError` still end the
session.

## 15. Model catalog

`mak/models/` answers *which models each provider currently offers*. It never
decides *which model MAK uses* — that is `planner.model` / `agents[].model` — and
a refresh never writes `config.yaml` (an acceptance test byte-compares it).

- **Facts vs. judgment.** A `ModelEntry` carries facts from the provider
  (`display_name`, `context_window`, `max_output`, `supported_parameters`) and
  judgment (`recommended`, `planner_ok`, `planner_recommended`) that comes **only**
  from the hand-maintained exact-id table `curation.py::CURATED`. No heuristics;
  an uncurated model gets a neutral `Judgment()`.
  `test_only_curated_ids_carry_stars` pins this. `curation.py::DENY` separately
  filters non-chat models and collapses dated snapshots onto their aliases.
- **Persistence.** `manifest.py` caches entries in `~/.config/mak/models.json`
  (atomic write), keyed by `(endpoint_id, model_id)`, schema version 3. Older
  schemas are migrated on read with no refetch.
- **Schedule.** `is_refresh_due` implements catch-up scheduling on the 1st and
  15th of each month; a 6-hour cooldown follows a failed attempt.
- **Failure isolation.** `refresh.py` replaces a provider's (or endpoint's)
  entries only on its own successful fetch. A model a provider stops offering is
  marked `retired`, not deleted; `recommended_planner` skips retired entries.
- **Endpoint catalogs.** `sources_for_endpoints` builds an
  `OpenAiCompatibleSource` per configured endpoint. `providers.py::reported_parameters`
  reads `supported_parameters` (from the SDK's `model_extra`) into the tri-state
  field §8 relies on. `evaluated` (whether curation applies) is derived from the
  provider at load time.
- **Runtime registry.** `ModelRegistry` composes the packaged `seed.json` (the
  offline floor), the manifest, and `judgment_for()` on every load. The snapshot
  is an immutable tuple swapped in one assignment, so reads need no lock.
  Lookups: `for_endpoint(endpoint_id)`, `find(model, endpoint_id)`.
- **Triggering.** `maybe_auto_refresh` runs on a daemon thread from
  `cli/app.py::_init_state`, gated by `models.auto_refresh` and
  `MAK_NO_MODEL_REFRESH`, and never prints. `/refresh-models` is the synchronous
  counterpart. Listing a local runtime during a refresh is capped at 2 seconds.
- **Credential and adapter identity.** `ModelEntry.api_key_env` /
  `.adapter_type` answer for the three built-in providers and are `None` for any
  endpoint's entry — its endpoint owns both. They never raise and never guess a
  variable name.
- **`cli/core/models.py`** is a thin adapter: `ModelInfo` is `ModelEntry`, and
  every helper (`all_models`, `models_for_provider`,
  `recommended_planner_for_provider`, `default_planner_route`) takes the registry
  explicitly. There is no module-level registry and no import-time I/O.

## 16. Local runtimes

`mak/local/` asks a running local server what it has, live, every time — no
cache, no manifest, no retirement — because a local model list is authoritative
and changes the moment the user pulls a model. It is separate from `mak/models/`,
whose machinery is keyed by API-key variables and hosted list endpoints. It
shares one principle: the fact/judgment split.

- **`ollama_client.py`** — a dependency-free client over Ollama's native API
  (`urllib.request` + `json`), so a fully local install needs no provider SDK
  (which is why the `[local]` extra is empty). `version()`, `list_models()`,
  `show(model)` (context length read by key suffix across `model_info`),
  `running()`, `chat(...)` (non-streaming), `pull(model)` (streamed, interruptible,
  resumable). Every failure is a typed `OllamaError` naming the endpoint. Stateless
  and injectable — no test in `tests/local/` opens a socket.
- **`runtime.py`** — `LocalRuntime` value object (`kind`, `name`, `base_url`,
  `version`, `models`). Kinds: `ollama` (probed via `/api/version`, `/api/tags`)
  and `openai_compatible` (via `/v1/models`: LM Studio, vLLM, llama.cpp, LocalAI).
- **`discovery.py`** — `discover(*, extra_urls=(), timeout=0.4, prober=None)`
  scans well-known ports concurrently, returns what answered (Ollama first,
  deduplicated by URL), and **never raises**. `$MAK_LOCAL_BASE_URL` and
  `extra_urls` are scanned too.
- **`recommended.py`** — the judgment half: exact Ollama tags with size and
  purpose, **smallest first**. `RecommendedModel.is_small()` drives the planner
  recommendation. A human edits this list; MAK does not rank local models.

MAK never manages the Ollama daemon, and there is no native adapter for other
runtimes — `local_api` covers them. Ollama gets one because only its native API
can read and set the real context window.

---

# Part III — Benchmarks and research

Each benchmark and study keeps its full documentation — method, fairness
controls, commands, and results — next to its code. This part says what each one
measures and what to keep in mind when changing it.

## Benchmark: MAK vs. git worktrees

[`benchmark/README.md`](benchmark/README.md) (per-run detail in
[`benchmark/STATS.md`](benchmark/STATS.md)) is a head-to-head between MAK and the
git-worktree model on four generated targets (Basic, Template 2, Template 3,
Template 4). Both sides use the same agents, prompts, task assignment and test
oracle; only the coordination model differs. Every target shares one registry
that every task must append to, so the workloads are **maximally contended**.

When you change it:

- Targets are generated from `benchmark/harness/template*_spec.py` by
  `benchmark/tools/gen_template*.py`; edit the spec, never the generated files.
- The MAK side is given an **oracle plan** (exact targets, `changes_api=False`,
  precomputed registry keys) and the worktree side is a **simulated** pipeline.
  The numbers therefore measure the kernel, not the planner or agent quality;
  label them that way. Wave 33 in [`TASKS.md`](TASKS.md) adds end-to-end and
  real-baseline arms.
- `python benchmark/run_benchmark.py --mode mock` is the keyless self-test;
  `--render-only` regenerates the reports without spending tokens.

## Semantic conflict corpus

[`benchmark/semantic/`](benchmark/semantic/) measures whether each coordination
model **catches** a semantic conflict. Each scenario is a tiny project, two
scripted edits A and B (`ScriptedAgent`, with a hook that holds B until A commits
when they run concurrently), and an oracle that must pass on the base, on A
alone, and on B alone, and **fail** on the naive combination
(`evaluate.validate`). `evaluate.run_mak` drives a real `Session`;
`evaluate.run_worktrees` merges the two single-edit states.

| shape | scenario | MAK | worktrees |
|---|---|---|---|
| 1 | stale read (write skew) | detected (commit) | missed |
| 2 | signature change vs. new call | detected (wave end) | missed |
| 3 | behaviour change, same signature | missed; detected with `impact_tests` | detected (tests) |
| 4 | deletion / rename | detected (wave end) | missed |
| 5 | override against a changed base | detected (wave end) | missed |
| 6 | duplicate registration key | detected (commit) | detected (textual) |
| 7 | order-dependent table | **prevented** | detected (textual) |
| 8 | new required field vs. new construction | detected (wave end) | missed |
| 9 | duplicate implementation | detected (wave end) | missed |
| 10 | out-of-store artifacts (config, SQL, docs) | not representable | not representable |

Zero false positives across single-edit runs, and at most one extra agent call
per detected shape. `tests/test_semantic_corpus.py` gates both the table and the
corpus's validity contract.

```bash
python benchmark/semantic/run_semantic.py          # markdown table
python benchmark/semantic/run_semantic.py --json   # one JSON object per shape
```

## Simulated-agent scaling sweep

`benchmark/sweep.py` drives the real kernel and real git with simulated model
calls to measure scaling without spending tokens. Its design, calibration and
threats to validity are in [`benchmark/sim/README.md`](benchmark/sim/README.md)
and the "Simulated agent scaling" section of `benchmark/README.md`. Two rules:
generated targets come from `benchmark/harness/synthetic_spec.py` (keep additions
there so target code and oracle cannot drift), and the bundled
`profiles/default.json` is a placeholder — never describe it as calibrated.

```bash
python benchmark/sweep.py --config benchmark/sweeps/smoke.yaml   # keyless
```

## Research: real-world contention

[`research/contention_study/`](research/contention_study/) measures how often real concurrent
changes in six large Python projects touch the same file versus the same MAK
node. Its findings, method and threats to validity are in
[`CONTENTION_STUDY.md`](research/contention_study/CONTENTION_STUDY.md); setup, commands
and the pipeline stages are in
[`research/contention_study/README.md`](research/contention_study/README.md); generated tables are
in `research/contention_study/data/RESULTS.md` — check that file before quoting any number.

Rules for changing the study:

- Keep the kernel read-only. The study imports
  `mak.node_store.ingestion.parse_file_into_fragments` so a measured node is
  exactly a node MAK would lock; a question that needs `mak/` changed is a
  separate wave.
- Pairs require **base overlap**, not just lifetime overlap, and merges are
  judged against a shared base (`mining/rebase.py`). Never use
  `git merge-tree A B` — it charges mainline commits to one PR.
- Keep the failure taxonomy distinct: source conflict, rebase conflict, invalid
  Git operation, missing head, unparseable file.
- Add columns through the cache migration layer; record excluded populations in
  named buckets.
- Regenerate `data/RESULTS.md`, `data/results.json`, profiles, CSVs and plots
  from code; never hand-edit aggregates.
- Research dependencies stay in `research/contention_study/.venv`; never add NumPy or
  Matplotlib to the kernel's `pyproject.toml`. The study has its own gates:
  `./run.sh pytest tests -q`, `ruff check .`, and strict mypy on `mining`.

---

# Part IV — Developing

## Prerequisites

- **Python ≥ 3.11** (CI runs 3.11 and 3.13).
- **git** on `PATH`.
- Provider SDKs (`anthropic`, `openai`, `google-genai`) install as dependencies
  and are imported lazily; the test suite never needs a key.

## Setup

```bash
git clone <repo-url>
cd multi-agent-kernel

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"             # mypy, pytest, types-PyYAML; ruff is a base dependency

pre-commit install                  # optional (its paths differ from CI's; see Wave R)
```

For real calls, put keys in `~/.config/mak/.env` (or run `mak` and use
`/apikey`); `mak/.env.example` lists the variable names.

## Project layout

```
cli/                          # interactive app (prompt_toolkit + rich) and `mak` script
├── __main__.py               # `mak` dispatcher: TUI, run, gc, examples, update
├── app.py                    # MakCli: REPL loop, task execution
├── commands.py               # slash-command handlers
├── completer.py              # COMMANDS list + MakCompleter
├── local.py                  # /local wizard and sub-commands
├── project_config.py         # offer to create <work dir>/.mak/config.yaml
├── runner.py                 # CliState → RunRequest bridge, token counter, git diff
├── setup.py                  # first-run mode and key wizard
├── ui.py                     # rich rendering
├── core/
│   ├── api_keys.py           # ~/.config/mak/.env parse/merge/write
│   ├── local_hosts.py        # remembered local hosts
│   ├── local_seams.py        # LocalSeams: discover / client / host probe
│   ├── models.py             # thin adapter over mak/models/
│   └── state.py              # CliState
└── endpoints/                # /endpoint: commands, wizard, prompts, render

mak/
├── __main__.py               # `mak run`
├── _version.py               # the single version source
├── application/              # request, config, route, keys, session, env — both front ends
├── bootstrap.py              # composition root
├── config.py / config.yaml   # config schema, loading, discovery; packaged default
├── examples/                 # packaged example configs (`mak examples`)
├── session.py                # session lifecycle
├── cascade.py                # post-wave fix-up loop, CascadeOutcome
├── execution_result.py       # whole-run outcome
├── teardown.py               # suite outcome, push policy
├── test_runner.py            # teardown TestRunner
├── core/                     # types, exceptions, logging, atomic, paths, budget, task_codec
├── node_store/               # store, ingestion, makignore, reconstruction, transaction,
│                             #   journal, registrar, api_digest
├── lock_manager/             # rwlock, lock_table, conflicts, deadlock_detector,
│                             #   project_lease, resources
├── scheduler/                # dag, scheduler, lock_policy
├── conflict_detector/        # detector, node_ids, module_index, and one module per check
├── semantic/                 # read_set, stale, interface, contracts, locking, symbols,
│                             #   cascade_graph, registry_merge, sources, project_files,
│                             #   gates, gate_types, type_gate, impact_tests, import_smoke,
│                             #   overlay, adjudicator
├── planner/                  # planner, llm, response, review, depgraph, validation, contracts
├── agent_runner/
│   ├── runner.py, registry.py, protocol.py, sandbox.py, stop_signals.py
│   ├── adapters/             # base, budget, result_schema, repair, anthropic/openai/
│   │                         #   gemini/ollama API adapters, cli_adapter + three CLI adapters
│   └── wrappers/             # CLI bridge: bridge, claude_code, codex, copilot
├── endpoints/                # types, profiles, parse, resolution, builtin, agents, store,
│                             #   capabilities, error_classification, health
├── models/                   # catalog, curation, manifest, providers, refresh, registry, seed.json
├── local/                    # ollama_client, runtime, discovery, recommended
└── git_integration/git.py

tests/                        # mirrors mak/ and cli/; tests/support/ holds a fake OpenAI server
benchmark/                    # Part III
research/
└── contention_study/         # Part III (own venv, tests, and gates)
demo/                         # a small demo project and config
diagram/                      # Mermaid architecture and sequence diagrams
```

## Quality gates

Three gates must be green for every change:

```bash
pytest -q                  # full suite
mypy --strict mak cli      # zero errors
ruff check mak cli tests   # zero findings
```

CI (`.github/workflows/ci.yml`) runs all three on Python 3.11 and 3.13, on
pushes to `main` and on pull requests. Focused runs while iterating: `pytest tests/node_store/ -q`,
`pytest tests/test_session.py -q`.

- Add tests with every feature.
- Ingestion or reconstruction changes: `tests/node_store/test_roundtrip.py` is
  mandatory.
- Locking changes: keep `tests/lock_manager/test_concurrency.py` green.
- Conflict-check changes: extend `tests/conflict_detector/test_false_positive_corpus.py`.
- Performance rewrites of subtle behaviour: write a **differential** test against
  the implementation being replaced.
- **No test may read the real user configuration.** `tests/conftest.py`'s
  `pytest_configure` points `HOME` and `XDG_CONFIG_HOME` at a throwaway directory
  before any test module is imported, and a per-test fixture isolates the `.env`
  lookups. `tests/test_hermetic.py` fails if the manifest, endpoint store,
  `local_hosts.json`, user `.env` or user `config.yaml` resolve outside it. Never read
  per-user state at import time.
- **Behaviour must not depend on the host Python.** When a standard-library
  answer differs between versions (as `Path.glob`'s trailing `**` does), MAK
  decides the meaning and the test states it explicitly.
- No test touches the network — provider fetches use fake sources, local-runtime
  tests inject the client, and endpoint tests use `tests/support/fake_openai_server.py`
  on loopback.

## Coding standards

Enforced by `ruff` and `mypy --strict`:

- **Naming:** `snake_case` for variables, functions, modules; `PascalCase` for
  classes; `UPPER_SNAKE_CASE` for constants; `_leading_underscore` for private
  members.
- **Structure:** one module, one responsibility. Functions do one thing; past ~40
  lines, consider splitting. **No global mutable state** — pass state explicitly
  (this is why the registry and `CapabilityCache` are injected instances). Use
  dataclasses for structured data, not raw dicts as arguments. Type annotations on
  every signature.
- **Imports:** standard library, third-party, then internal (`mak.*`), separated
  by blank lines. No wildcard imports.
- **Errors:** explicit exceptions with descriptive messages, defined in
  `mak/core/exceptions.py`. Never swallow an exception — log and re-raise, or
  handle deliberately.
- **Docs:** public functions and classes need docstrings (ruff enforces this in
  `mak/`). Comments explain *why*. **No TODO comments** — open an issue instead.
  Comments must be self-contained: do not reference planning documents that are
  not part of the committed tree.

## Workflow: branches, commits, pull requests

- **Branch off `main`.** Feature work uses `feat/<number>-<feature-name>`; do not
  commit directly to `main`.
- **Keep PRs scoped** to one logical change, with all three gates green in the
  final state.
- **Commit messages explain the *why*.** The `[MAK-<id>]` subject format is what
  the *kernel* writes for audit commits; your own commits follow ordinary practice.
- **Behaviour changes need tests.**
- **Update the docs you change the truth of, in one place each:** this file for
  architecture and workflow; `mak/config.yaml` or `mak/examples/` for config
  keys; `benchmark/` and `research/contention_study/` docs for their own method and
  results; [`TASKS.md`](TASKS.md) for planned work (mark a wave done, add new
  ones); `README.md` for user-facing behaviour; `CHANGELOG.md` for every release. The version lives in `mak/_version.py` and the
  README badge (line 6) must match it.

---

# Part V — Where to contribute

## Open issues

All planned work lives in [`TASKS.md`](TASKS.md): a priority-ordered index of
**waves** (each one branch, `feat/<wave>-<name>`), the rules that apply to every
wave, and for each wave the evidence, design decisions, implementation steps,
test matrix and acceptance criteria. Pick a wave or a step from there, and open
an issue to coordinate before starting a large one. Ideas not yet assigned to a
wave are listed at the end of that file.

## Known limitations

Deliberate, fail-safe tradeoffs — not bugs:

- **Class shells are not independently parseable.** A task returning only a
  `class` shell is rejected by the parse gate, never written.
- **Checks are shallow by design.** Calls through an untyped receiver are not
  checked; module-level functions are keyed by bare name across a batch, so an
  unrelated same-named function in another file can cause a false positive (a
  bounded retry, not corruption). The wave-end checks skip metaclasses,
  unrecognised decorators, multiple resolved bases, and dynamic module bindings
  for the same reason.
- **Behaviour changes behind an unchanged signature** need `impact_tests`, which
  is off by default because it runs the project's tests in subprocesses.
- **Impacted-test selection is static** (import closure), so a test reaching a
  module only through late binding or dependency injection is missed.
- **Out-of-store files** (config, SQL, docs) are not nodes; MAK neither locks nor
  checks them. Structured text files are planned in Wave 8 ([`TASKS.md`](TASKS.md)).
- **The deadlock watchdog finds nothing by design** — atomic pre-allocation makes
  the wait graph acyclic. Wave 32 ([`TASKS.md`](TASKS.md)) turns it into a rare assertion.
- **Abandoning a wedged agent call is cooperative.** Python cannot kill a thread
  mid-call; the per-request timeout bounds it, and non-blocking shutdown stops the
  session waiting for it. Wave 29 ([`TASKS.md`](TASKS.md)) adds killable per-call processes.
- **One MAK per project.** Concurrent runs on one checkout are refused with
  `ProjectBusyError`; distinct projects run fine.
- **The Windows project lease is weaker** than the POSIX `flock` (see §4.4 and
  Wave R in [`TASKS.md`](TASKS.md)).
- **Reconciliation adopts the working tree by default**, so MAK builds on edits
  it never saw made. Use `on_external_edit: conflict` to stop instead.

## Good first contributions

- Test coverage for edge cases: ingestion corners, splat handling in signature
  checks, config coercion.
- Error messages that point at the fix.
- Clarifying a subsystem in this file, or adding module-level examples.
- Hardening a CLI bridge wrapper (`mak/agent_runner/wrappers/`) for a specific
  `claude` / `codex` / `gh copilot` version, or extending the sandbox (host
  allowlisting).
- Small, self-contained steps from [`TASKS.md`](TASKS.md): matching
  `.pre-commit-config.yaml` to CI, numeric pre-release tag ordering, and removing
  the legacy `mak/.env` (Wave R).

---

# Part VI — Design principles

- **Shared memory over message passing.** Resolve conflicts at scheduling time,
  while the dependency graph is explicit.
- **The node store is the source of truth; files are derived.** This enables
  symbol-level locking and position-independent identity.
- **LLMs only where language understanding is needed.** Planning and fragment
  rewriting; everything else is deterministic.
- **Raw-source span tiling.** Comments and formatting survive by construction,
  with no CST dependency.
- **API-first adapters with forced structured output.** The agent stays a
  constrained fragment transform, not an autonomous editor.
- **Atomic lock pre-allocation.** A waiting task holds nothing, so the pipeline is
  deadlock-free by construction.
- **Human-in-the-loop plan review.** A few seconds of review remove the single
  point of failure in one-shot DAG generation.
- **One transaction per commit, with one commit point.** Validate before
  advancing the store; journal before touching disk; recover by comparison, not
  by hope.
- **`compile()`, not `ast.parse()`, at every gate** — reject what Python would
  reject.
- **Precision over recall in conflict detection.** A false conflict costs more
  than a missed one.
- **Positive assertion, never absence of evidence.** When a positive and a
  negative outcome can look identical (a no-op vs. a truncated reply), require
  the positive case to assert itself — and only accept assertions about things
  that existed to be assessed.
- **Enforce declarations; never trust them.** Contracts, body-only claims, and
  registry keys are checked at commit.
- **Bound what a run scans, not only what it sends.** A filter downstream of the
  work is not a bound.
- **An event names what happened.** Outcomes get their own event types, not flags.
- **Evidence has provenance.** Catalog claims and runtime proof are kept apart;
  unknown is never collapsed into "unsupported".
- **No global mutable state.** Caches and registries are owned and injected.
- **Differential tests for "identical, only better" rewrites.**

---

# Glossary

- **Node** — the smallest independently lockable unit of code.
- **NodeId** — `<file>::<kind>::<qualified_name>`, or a bare path for a whole-file
  node.
- **Fragment (`NodeFragment`)** — a node's raw source with version and order
  metadata.
- **TaskBundle / TaskResult** — the wire objects sent to / returned from an agent.
- **SubTask** — a planned unit of work: write targets, read context,
  dependencies, optional declarations.
- **Wave** — one run of a plan; cascade waves are the fix-up plans that follow.
- **EditRound** — the staged fragments the structural checks validate together.
- **Read set** — the digests of every node a task was shown, checked at commit.
- **Registrar** — a function whose body is a flat list of keyed registration
  calls.
- **Contract** — a declared signature, enforced at commit.
- **Repair obligation** — a defect a fix-up task must resolve before committing.
- **Endpoint** — a configured model service; **profile** — its preset defaults;
  **transport** — its wire protocol.
- **Adapter** — the translator between MAK's protocol and one agent backend.
- **Composition root** — `mak/bootstrap.py`; **application API** —
  `mak/application/`, which both front ends use to build a run.
- **PlannerRoute** — the planner's route (hosted, endpoint or local) as one
  complete value.
- **`.mak/`** — the project's MAK directory: its optional `config.yaml`, and
  runtime state (node store, journal, lock table, lease, task graph, session
  log). The user-level config is `~/.config/mak/config.yaml`.
- **`.makignore`** — the project's gitignore-style list of paths MAK never
  ingests.

---

# License

[MIT](LICENSE) © 2026 Seungjoon Cha

By contributing, you agree that your contributions are licensed under the
project's MIT License.
