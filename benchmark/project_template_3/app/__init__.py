"""app (template 3) — a small storefront backend used as the real-world benchmark target.

58 feature tasks across 8 feature modules (accounts, catalog, cart, orders, payments, shipping, reviews, search), each an unimplemented stub
modelled on real service code. Unlike the fully-contended toolkit templates, tasks here
register into ZERO, ONE, or TWO of four cross-cutting shared tables — ``routes``,
``events``, ``errors``, ``settings`` — the files real feature teams collide on. A
worktree-per-agent workflow conflicts on every shared table at merge time; MAK
serializes only same-table edits under node-level write locks and runs everything
else in parallel.
"""
