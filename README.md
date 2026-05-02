## `THIS PROJECT IS CURRENTLY WORK IN PROGRESS`

# Multi Agent Kernel (MAK)

> A shared-memory-inspired orchestration kernel for concurrent multi-agent software development.

---

## Motivation

Current multi-agent development systems mirror how humans collaborate via Git: each agent works in isolation on a separate branch or worktree, then merges results later. This is fundamentally **message-passing** architecture — agents are loosely coupled, communicate asynchronously, and reconcile diverging states after the fact.

This works. But it inherits all the costs of message-passing: coordination overhead, merge conflicts as a synchronization primitive, and an inability to express true concurrent edits on shared state.

Operating systems solved an analogous problem decades ago. When multiple threads need to collaborate on a single process, the OS doesn't force them to maintain separate memory spaces and reconcile diffs — it provides **shared memory** with concurrency primitives (mutexes, semaphores, read-write locks) that allow fine-grained, low-latency coordination. Shared memory is harder to reason about, but it's faster and expressive enough to support true parallelism.

**Multi Agent Kernel** applies this insight to agentic workflows. Instead of serializing agent collaboration through Git branches, MAK manages a shared codebase as a first-class resource — assigning file-level and symbol-level locks, tracking inter-agent dependencies, and scheduling concurrent edits without corruption.

---

## Core Design Principles

**1. The Kernel Model** — `MAK` is the OS; agents are processes (or threads). `MAK` owns the scheduler, the resource table, and the conflict-detection logic. Agents do not coordinate directly with each other — they request resources from `MAK`, which arbitrates.

**2. Shared State Over Message Passing** — All agents operate on the same working directory. There are no worktrees, no per-agent forks. Concurrency is managed through lock primitives at the file and symbol level, not through branch isolation.

**3. Dependency-Aware Scheduling** — Before assigning a subtask to an agent, `MAK` resolves its dependencies: which files it reads, which symbols it writes, which other subtasks must complete first. This produces a DAG that drives the execution schedule.

**4. Deterministic Where Possible** — Task decomposition is the one place where probabilistic reasoning (LLM) is unavoidable. All other `MAK` subsystems — the scheduler, lock manager, dependency resolver, conflict detector — are implemented as deterministic, rule-based modules. No LLM is involved in runtime arbitration.

**5. Git as an Audit Log** — Git integration is automated and handled by agents themselves. Commits are structured, atomic, and keyed to task IDs. `MAK` does not use branches for isolation; it uses Git as a ledger of completed, validated work.

---

## Concurrency Model

`MAK` uses a **reader-writer lock** scheme per resource (file or symbol):

- Multiple agents may hold a **read lock** simultaneously.
- A **write lock** is exclusive — no other agent may read or write the resource while it is held.
- Lock requests are queued by the scheduler. An agent blocks (or is reassigned) if its required locks are unavailable.
- Deadlock detection runs as a background cycle-detection pass over the lock dependency graph.

This is directly analogous to `pthread_rwlock_t` in POSIX systems.

---

## Design Decisions & Known Tensions

**Why not just use Git worktrees?** Because the merge step is where complexity accumulates. Worktree-based systems push conflict resolution to the end of the pipeline; MAK pushes it to the scheduler, where dependency information is still explicit and resolvable deterministically.

**Why keep LLM only in the planner?** Because LLMs are probabilistic and slow. Every additional LLM call in the runtime hot path adds latency and unpredictability. Task decomposition is the one step that genuinely requires language understanding — everything downstream is graph traversal and lock management, which should be fast and correct.

**Can agents really edit the same file concurrently?** Yes, at the symbol level (e.g., two agents editing different functions in the same file). MAK tracks edits at the AST node level, not the line level, so non-overlapping symbol edits in the same file can proceed in parallel. Line-level concurrent edits to the same region are serialized.
