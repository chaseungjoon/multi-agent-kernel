# Workflow

> [Contributing guide](../CONTRIBUTING.md) · ← [Testing](testing.md)

This page covers how code is written, how a change reaches `main`, and which
document to update when a change makes one of them wrong.

## Coding standards

`ruff` and `mypy --strict` enforce these:

- **Naming:** `snake_case` for variables, functions and modules; `PascalCase` for
  classes; `UPPER_SNAKE_CASE` for constants; `_leading_underscore` for private
  members.
- **Structure:** one module, one responsibility. A function does one thing; past
  ~40 lines, consider splitting it. **No global mutable state**: pass state
  explicitly (this is why the registry and `CapabilityCache` are injected
  instances). Use dataclasses for structured data, not raw dicts as arguments.
  Annotate every signature.
- **Imports:** standard library, third-party, then internal (`mak.*`), separated
  by blank lines. No wildcard imports.
- **Errors:** explicit exceptions with descriptive messages, defined in
  `mak/core/exceptions.py` ([§1](subsystems/01-core.md)). Never swallow an
  exception: log and re-raise, or handle it deliberately.
- **Docs:** public functions and classes need docstrings (ruff enforces this in
  `mak/`). Comments explain *why*. **No TODO comments**: open an issue or add
  the work to [`TASKS.md`](../TASKS.md) instead. Comments must be
  self-contained, so do not reference planning documents that are not part of
  the committed tree.

## Branches, commits, pull requests

- **Branch off `main`.** Do not commit directly to `main`.
  - Planned feature work is done in **waves**, one branch each:
    `feat/<wave-number>-<feature-name>`. The waves and their steps are in
    [`TASKS.md`](../TASKS.md).
  - A small fix outside any wave uses `Hotfix/<hotfix-name>`.
- **Keep PRs scoped** to one logical change, with all three
  [gates](testing.md#quality-gates) green in the final state.
- **Commit messages explain the *why*.** The `[MAK-<id>]` subject format is what
  the *kernel* writes for audit commits ([§10](subsystems/10-git-integration.md));
  your own commits follow ordinary practice.
- **Behaviour changes need tests** ([Testing](testing.md)).

Coding agents working in this repository also follow
[`AGENTS.md`](../AGENTS.md), which defines the wave lifecycle (plan, execute,
document, version) and the hotfix rules in detail.

## Keeping the docs true

Each fact is documented in exactly one place. When your change makes a document
wrong, fix that document in the same change:

| What changed | Where it is documented |
|---|---|
| How a subsystem works | its reference under [`docs/subsystems/`](subsystems/) |
| What MAK is, the data flow, the module map | [`docs/overview.md`](overview.md) |
| Setup, layout, gates, standards, workflow | [`docs/getting-started.md`](getting-started.md), [`docs/testing.md`](testing.md), this page |
| A deliberate limitation | [`docs/known-limitations.md`](known-limitations.md) |
| A new doc, or a moved one | the documentation map in [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| A config key | [`mak/config.yaml`](../mak/config.yaml) or [`mak/examples/`](../mak/examples/), never the docs |
| A benchmark's or study's method and results | its own docs under `benchmark/` or `research/contention_study/` |
| The component structure or run sequence | [`diagram/`](../diagram/README.md), re-rendering the PNGs |
| Planned work | [`TASKS.md`](../TASKS.md): mark a wave done, add new ones |
| User-facing behaviour | [`README.md`](../README.md) |
| Every release | [`CHANGELOG.md`](../CHANGELOG.md) |

The version lives in [`mak/_version.py`](../mak/_version.py). The README's
`Version` badge and the status line in [`CONTRIBUTING.md`](../CONTRIBUTING.md#current-status)
must match it.

**Section numbers are stable.** Code comments and `TASKS.md` cite subsystems as
`CONTRIBUTING §N` or `§N.M`, and the subsystem files are numbered to match. A
new subsystem gets the next free number. An existing number is never reused or
reassigned.

---

[Contributing guide](../CONTRIBUTING.md) · ← [Testing](testing.md)
