"""toolkit — a small operation library used as the benchmark target.

Every operation is an unimplemented stub, and every operation must register itself
in ``registry._register_all`` (the shared dispatch table). That shared function is
the contention point: many agents must edit it, which is exactly where a
worktree-merge workflow collides and MAK's node-level locking does not.
"""
