"""Benchmark harness: a fair MAK-vs-worktree comparison over one shared workload.

Both runners implement the *same* operations with the *same* agents (same models,
same per-operation prompt). The only thing that differs is the coordination model:

- ``mak_runner`` drives the real MAK kernel — node-level locks serialize the edits
  to the shared registry function, so none are lost.
- ``traditional`` gives each agent its own git worktree, then merges the branches;
  the shared registry function is where those merges collide.

Metrics (time, tokens, accuracy) are collected identically for both.
"""
