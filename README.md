# Multi Agent Kernel

> **Work in progress, as of JUN26**

A kernel for concurrent multi-agent software development. Agents edit a shared codebase simultaneously — no worktrees, no merge conflicts, no reconciliation step.

---

## The Idea

Most multi-agent coding systems give each agent a Git branch and merge at the end. That's message-passing: **agents work in isolation and synchronize at boundaries.**

**The multi agent kernel** takes the **shared-memory approach instead.** All agents operate on the same working directory. The kernel owns a symbol-level lock table and arbitrates concurrent access the way an OS arbitrates shared memory between threads — with reader-writer locks, dependency tracking, and deadlock detection. Git is used only as an audit log.

---

## How It Works

The kernel manages four things:

**Node Store** — The codebase is decomposed into independently lockable AST nodes (functions, classes, module headers). Files on disk are derived artifacts; the node store is the source of truth.

**Lock Manager** — Each node has a reader-writer lock. Multiple agents can hold read locks simultaneously; write locks are exclusive. Lock acquisition is atomic — an agent either gets all its required locks or waits.

**Scheduler** — The planner (one LLM call) decomposes the user's task into a DAG of subtasks. The scheduler walks the DAG, dispatches ready tasks to available agents, and unblocks downstream tasks as dependencies complete.

**Agent Runner** — Agents are called through a swappable adapter interface. Primary adapters use direct API calls (Anthropic SDK, OpenAI SDK) for structured, reliable JSON output. CLI subprocess adapters are supported as a fallback. The kernel never ties to a specific agent backend.

```
User Task → Planner (LLM) → DAG → Scheduler → Agent Runner → Agents
                                       ↑                         |
                                  Lock Manager ← Node Store ←────┘
```

---

## Design Decisions

> **Why not use Git worktrees?** 

Worktree based systems defer conflict resolution to merge time, where dependency information is already lost. ***MAK*** resolves conflicts at scheduling time, where the dependency graph is explicit.

> **Why is LLM only in the planner?** 

Every LLM call in the runtime path adds latency and unpredictability. Task decomposition genuinely requires language understanding. Everything downstream — graph traversal, lock arbitration, AST reconstruction — is deterministic.

> **How does concurrent file editing actually work?** 

Each agent receives only the **AST nodes** it has write locks on, edits them in isolation, and returns the **modified fragments**. The kernel reconstructs the file from all committed node versions. Agents never see or touch the full file.

---

## Status

| Module | Status |
|---|---|
| `mak/core/` | Complete | 
| `mak/config.py` | Complete | 
| `mak/node_store/` | Complete | 
| `mak/lock_manager/` | Complete | 
| `mak/agent_runner/` | Complete | 
| `mak/scheduler/` | In Progress| 
| `mak/conflict_detector/` | In Progress | 
| `mak/planner/` | In Progress |
| `mak/git_integration/` | Complete | 
| `mak/session.py` | In Progress |

---

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
