# Multi Agent Kernel (MAK)

> **Work in progress.**

A kernel for concurrent multi-agent software development. Agents edit a shared
codebase simultaneously — no worktrees, no merge conflicts, no reconciliation step.

## The idea

Most multi-agent coding systems give each agent a Git branch and merge at the end —
a **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

MAK takes the **shared-memory** approach. All agents operate on one working
directory; the kernel owns a symbol-level lock table and arbitrates concurrent
access the way an OS arbitrates shared memory between threads — reader-writer locks,
dependency tracking, deadlock detection. Conflicts are resolved at scheduling time,
where the dependency graph is still explicit. Git is used only as an audit log.

The codebase is decomposed into independently lockable AST nodes; files on disk are
derived artifacts reconstructed from the node store. Each agent receives only the
nodes it has write locks on, edits them in isolation, and returns the modified
fragments. The kernel reassembles the file.

## Status

The kernel builds and is well-tested (full suite + `mypy --strict` + `ruff` green).
Execution is currently **sequential** — the concurrent shared-memory path is the
next milestone, not yet exercised end-to-end. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the detailed status and roadmap.

## Contributing

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
