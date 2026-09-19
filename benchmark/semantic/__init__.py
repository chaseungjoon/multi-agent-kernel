"""Wave 20.

1: a seeded corpus of semantic conflicts, one scenario per shape.

Each scenario is a tiny project, two scripted agent edits (A and B) and an
oracle that passes on the base project, with A alone, and with B alone — and
fails when both land without coordination. ``evaluate`` runs every scenario
through MAK and through a git-worktree baseline and classifies what each
mechanism did: prevented, detected (and where), or missed.
"""
