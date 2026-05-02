# Multi Agent Kernel

> **Work in progress.** Foundation and core subsystems are implemented and tested (148 tests passing). Scheduler, planner, and session orchestration are next.

A kernel for concurrent multi-agent software development. Agents edit a shared codebase simultaneously — no worktrees, no merge conflicts, no reconciliation step.

---

## The Idea

Most multi-agent coding systems give each agent a Git branch and merge at the end. That's message-passing: **agents work in isolation and synchronize at boundaries.**

***MAK takes the shared-memory approach instead.*** All agents operate on the same working directory. The kernel owns a symbol-level lock table and arbitrates concurrent access the way an OS arbitrates shared memory between threads — with reader-writer locks, dependency tracking, and deadlock detection. Git is used only as an audit log.

---

## How It Works

The kernel manages four things:

**Node Store** — The codebase is decomposed into independently lockable AST nodes (functions, classes, module headers). Files on disk are derived artifacts; the node store is the source of truth.

**Lock Manager** — Each node has a reader-writer lock. Multiple agents can hold read locks simultaneously; write locks are exclusive. Lock acquisition is atomic — an agent either gets all its required locks or waits.

**Scheduler** — The planner (one LLM call) decomposes the user's task into a DAG of subtasks. The scheduler walks the DAG, dispatches ready tasks to available agents, and unblocks downstream tasks as dependencies complete.

**Agent Runner** — Agents are external CLI subprocesses (Claude Code, Codex, Gemini, etc.), managed through a swappable adapter interface. The kernel never calls agent APIs directly.

```
User Task → Planner (LLM) → DAG → Scheduler → Agent Runner → Agents
                                       ↑                         |
                                  Lock Manager ← Node Store ←────┘
```

---

## Agent Adapters

Adding a new agent backend means writing one class:

```python
class MyAgentAdapter(AgentAdapter):
    def spawn(self, working_dir: str) -> subprocess.Popen: ...
    def format_task(self, task: TaskBundle) -> str: ...
    def parse_result(self, output: str) -> TaskResult: ...
    def health_check(self) -> bool: ...
```

The kernel dispatches tasks as structured JSON over stdin/stdout. Agents return results the same way. Everything else — which CLI tool, which model, which flags — is the adapter's concern.

---

## Design Decisions

**Why not Git worktrees?** Worktree-based systems defer conflict resolution to merge time, where dependency information is already lost. MAK resolves conflicts at scheduling time, where the dependency graph is explicit.

**Why is LLM only in the planner?** Every LLM call in the runtime path adds latency and unpredictability. Task decomposition genuinely requires language understanding. Everything downstream — graph traversal, lock arbitration, AST reconstruction — is deterministic.

**How does concurrent file editing actually work?** Each agent receives only the AST nodes it has write locks on, edits them in isolation, and returns the modified fragments. The kernel reconstructs the file from all committed node versions. Agents never see or touch the full file.

**Why `ast` + `ruff` instead of `libcst`?** `ast.unparse()` is simpler and fast. The tradeoff is that inline comments are not preserved across reconstruction. If comment preservation matters, the ingestion and reconstruction layer can be swapped for `libcst` without touching the rest of the kernel.

---

## Status

| Module | Status | Notes |
|---|---|---|
| `mak/core/` | Complete | Types, exceptions, structured session logger |
| `mak/config.py` | Complete | YAML config loading with validation |
| `mak/node_store/` | Complete | Ingestion, versioned store, AST reconstruction |
| `mak/lock_manager/` | Complete | RW locks, lock table, deadlock detection |
| `mak/agent_runner/` | Partial | Protocol + registry + base adapter done; subprocess pool and Claude adapter next |
| `mak/scheduler/` | Not started | DAG builder and dispatch loop |
| `mak/conflict_detector/` | Not started | Signature, import, and name collision checks |
| `mak/planner/` | Not started | LLM task decomposition |
| `mak/git_integration/` | Not started | Commit helpers and push coordination |
| `mak/session.py` | Not started | Full pipeline orchestration and crash recovery |

**148 tests passing.** `mypy --strict` clean on completed modules.
