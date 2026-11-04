# Overview

> [Contributing guide](../CONTRIBUTING.md) · Next: [Design principles](design-principles.md) →

MAK implements an unusual idea: a shared-memory concurrency kernel for coding
agents. The layout of the code won't tell you much until you have the model it
implements, so start here. This page covers what MAK is, why it doesn't use Git
worktrees, how the pieces fit together, and the one contract everything else
exists to protect. The [subsystem references](../CONTRIBUTING.md#subsystems)
cover each piece in depth.

## What MAK is

MAK is a **kernel for concurrent multi-agent software development**. It lets
several coding agents edit one shared codebase at the same time, without Git
worktrees, without merge conflicts, and without a reconciliation step at the end.

Most multi-agent coding systems give each agent its own Git branch and merge at
the end. That is a **message-passing** architecture: agents work in isolation
and synchronize only at boundaries, by which point the dependency information
needed to resolve conflicts has been lost.

MAK takes the **shared-memory** approach. All agents operate on the same working
directory. The kernel owns a symbol-level lock table and arbitrates concurrent
access the way an operating system arbitrates shared memory between threads,
with reader-writer locks, dependency tracking, and deadlock detection. Git is an
audit log, written *after* MAK validates an agent's output.

**Core constraint:** MAK is self-contained. There is no external orchestration
system. Planning, scheduling, lock arbitration, agent lifecycle, conflict
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
transforms). Task decomposition needs language understanding. Everything
downstream (graph traversal, lock arbitration, AST reconstruction, conflict
detection) is deterministic and stays that way.

## Architecture at a glance

[`diagram/`](../diagram/README.md) holds the component architecture, the
execution sequence, and the commit pipeline as editable Mermaid sources with PNG
exports.

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

- **Node store**: decomposes the codebase into independently lockable AST nodes
  (functions, methods, classes, module headers). It is the source of truth
  ([§2](subsystems/02-node-store.md)).
- **Lock manager**: reader-writer locks per resource, atomic all-or-nothing
  acquisition, deadlock detection, one owner per project
  ([§4](subsystems/04-lock-manager.md)).
- **Scheduler**: turns the planner's subtask DAG into running work,
  pre-allocating locks before dispatch and unblocking dependents as tasks finish
  ([§6](subsystems/06-scheduler.md)).
- **Agent runner**: calls agents through a swappable adapter interface. API
  adapters force structured JSON output and are the primary path
  ([§7](subsystems/07-agent-runner.md)).

## End-to-end data flow

```
User: "Implement topological sort in the scheduler module."
│
▼
Planner (LLM call over a bounded inventory view) → deterministic plan validation
  + caller tasks → (optional) human review
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

Each stage has its own reference: planning in [§9](subsystems/09-planner.md),
dispatch enrichment in [§3.3](subsystems/03-ast-pipeline.md#33-dispatch-bundle-enrichment),
the commit pipeline in [§5.3](subsystems/05-conflict-detection.md#53-semantic-conflicts-maksemantic),
and the run loop, cascade waves and teardown in [§11](subsystems/11-session-lifecycle.md).

## The mental model

> **The node store is the source of truth, and an agent is a pure fragment
> transform:** node source in, rewritten node source out.

An agent never roams the repo or edits disk. It returns the new source of each
node it was granted (`TaskResult.new_sources`). The kernel stages, validates,
conflict-checks, commits, reconstructs, and writes. Anything returned outside the
lock grant is refused and logged. The agent receives its write-target sources plus
an automatically enriched context window (same-file siblings, cross-file callers,
the committed output of the tasks it depends on, and any declared contracts), so it
arrives with the dependency picture even when the planner did not enumerate it.

Everything else (the lock table, scheduler, conflict detection, the commit
transaction) exists to make that fragment-transform contract safe under
concurrency. `Session.run` dispatches every lock-satisfiable ready task onto a
bounded thread pool (`max_concurrent_agents`), batches concurrently completing
results into one conflict-detection round, commits in a deterministic order,
re-validates lock ownership at commit time, and renews leases with a heartbeat.
The concurrency gate is `tests/test_concurrency_integration.py`.

## Where each piece lives

| Area | Modules | Reference |
|---|---|---|
| Core types, errors, logging, atomic writes, path containment | `mak/core/` | [§1](subsystems/01-core.md) |
| Node store, ingestion, reconstruction, commit transaction, journal | `mak/node_store/` | [§2](subsystems/02-node-store.md), [§3](subsystems/03-ast-pipeline.md) |
| Locks, project lease, derived lock resources | `mak/lock_manager/`, `mak/scheduler/lock_policy.py` | [§4](subsystems/04-lock-manager.md) |
| Structural conflict checks | `mak/conflict_detector/` | [§5.1](subsystems/05-conflict-detection.md#51-structural-checks-makconflict_detector), [§5.2](subsystems/05-conflict-detection.md#52-wave-end-checks) |
| Semantic conflicts (read sets, stale reads, contracts, cascade, gates) | `mak/semantic/` | [§5.3](subsystems/05-conflict-detection.md#53-semantic-conflicts-maksemantic) |
| Scheduler | `mak/scheduler/` | [§6](subsystems/06-scheduler.md) |
| Agent runner, API/CLI/local adapters, sandbox | `mak/agent_runner/` | [§7](subsystems/07-agent-runner.md) |
| OpenAI-compatible endpoints and capability negotiation | `mak/endpoints/` | [§8](subsystems/08-endpoints.md) |
| Planner (strategies, inventory view, retrieval, caller completion, telemetry), plan validation, human review | `mak/planner/` | [§9](subsystems/09-planner.md) |
| Git audit log | `mak/git_integration/` | [§10](subsystems/10-git-integration.md) |
| Session state machine and its collaborators, cascade loop, run outcome, teardown | `mak/session/`, `mak/cascade.py`, `mak/execution_result.py`, `mak/teardown.py` | [§11](subsystems/11-session-lifecycle.md) |
| Configuration schema, loading, discovery | `mak/config.py`, `mak/config.yaml`, `mak/examples/` | [§12](subsystems/12-configuration.md) |
| Application API: run request, config, planner route and key, session assembly | `mak/application/` | [§13](subsystems/13-command-line.md#the-application-api-makapplication) |
| `mak run` / `mak` console script | `mak/__main__.py`, `cli/__main__.py` | [§13](subsystems/13-command-line.md) |
| Interactive app | `cli/` | [§14](subsystems/14-interactive-app.md) |
| Model catalog | `mak/models/` | [§15](subsystems/15-model-catalog.md) |
| Local runtime discovery and native Ollama client | `mak/local/` | [§16](subsystems/16-local-runtimes.md) |

The file-by-file tree is in [Getting started](getting-started.md#project-layout).

---

[Contributing guide](../CONTRIBUTING.md) · Next: [Design principles](design-principles.md) →
