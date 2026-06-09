# Multi Agent Kernel (MAK)

A kernel for **concurrent** multi-agent software development. Many agents edit one
shared working directory at the same time — no worktrees, no merge step, no
late-stage reconciliation. The kernel arbitrates concurrent access the way an OS
arbitrates shared memory between threads.

## The idea

Most multi-agent coding systems give each agent a Git branch and merge at the end —
a **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

MAK takes the **shared-memory** approach. The codebase is decomposed into
independently lockable AST nodes (functions, methods, classes, headers); files on
disk are derived artifacts reconstructed from a versioned node store. The kernel owns
a symbol-level lock table and resolves conflicts at *scheduling* time, where the
dependency graph is still explicit. Each agent receives only the nodes it holds write
locks on, edits them in isolation, and returns the modified fragments; the kernel
reassembles the file. Git is used only as an audit log.

## Status

The **kernel is functionally complete and well-tested**: 490 tests, `mypy --strict`
clean, `ruff` clean. Concretely:

- **Concurrent execution.** Agents are dispatched onto a bounded thread pool and edit
  the shared node store in parallel, arbitrated by the lock table, with batched
  cross-agent conflict detection, commit-time lock re-validation, a lease heartbeat,
  and a deadlock watchdog. A concurrency integration test over an overlapping-node
  corpus asserts no two conflicting holders coexist, no lost/corrupted fragments, no
  deadlock, and store–disk consistency.
- **Real agent edits land in the store.** An agent returns the rewritten source of
  each node it changed; the kernel stages, validates, and commits it (rejecting
  anything outside the agent's grant). `python -m mak --task "..."` can run against a
  real model given an API key. (A live hosted-model call isn't exercised in CI.)

The remaining work is research and tooling, not core mechanism — see
[CONTRIBUTING.md](CONTRIBUTING.md) (*Open problems*): evaluation against a worktree
baseline, planner token efficiency, and multi-language support.

## Contributing

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
