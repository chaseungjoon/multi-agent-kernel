"""Semantic-conflict machinery.

Node write locks guarantee no two agents write the same AST node at once. They
say nothing about two edits on *different* nodes that are each correct and wrong
together — a stale read, a signature changed under a new call, a duplicated
registry key. This package holds the kernel-side pieces that close that gap:

- :mod:`~mak.semantic.locking` — the lock policy a wave runs under;
- :mod:`~mak.semantic.read_set` — what every bundle carried, and at which version;
- :mod:`~mak.semantic.stale` — classifying and deciding a stale read;
- :mod:`~mak.semantic.symbols` — per-file symbol diffs for post-wave analysis;
- :mod:`~mak.semantic.cascade_graph` — fix-up work from real reference edges;
- :mod:`~mak.semantic.gates` and friends — the optional heavy gates.

``mak.session`` wires them in; nothing here drives a run on its own.
"""
