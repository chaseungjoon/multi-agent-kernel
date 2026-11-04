# Contributing to the Multi Agent Kernel (MAK)

Welcome, and thank you for considering a contribution to MAK. By participating
you agree to uphold the [Code of Conduct](CODE_OF_CONDUCT.md).

MAK implements an unusual idea: a shared-memory concurrency kernel for coding
agents. Contributing well takes the architecture, not just the file layout.
This file is where you start. It says where each topic is documented, what to
read for the change you have in mind, and where help is wanted. The
documentation itself lives in [`docs/`](docs/).

> Jump to: [Current status](#current-status) · [Quick start](#quick-start) ·
> [Reading paths](#reading-paths) · [Documentation map](#documentation-map) ·
> [Where to contribute](#where-to-contribute)

---

## Current status

MAK **0.10.0 Beta** (`mak/_version.py`). The kernel, the semantic-conflict layer,
endpoint support, local runtimes, and the interactive app are all implemented.

| Gate | State |
|---|---|
| `mypy --strict mak cli` | clean |
| `ruff check mak cli tests` | clean |
| `pytest -q` | green on Python 3.11 and 3.13, locally and in CI; hermetic (never reads the real `~/.config/mak/`) |

Packaging for a public release, growable write sets, agent tools and
multi-language support are not done yet. They are planned in
[`TASKS.md`](TASKS.md).

## Quick start

```bash
git clone https://github.com/chaseungjoon/multi-agent-kernel
cd multi-agent-kernel
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q && mypy --strict mak cli && ruff check mak cli tests
```

No API key is needed to develop or test. The full setup is in
[Getting started](docs/getting-started.md), and what each gate guards is in
[Testing](docs/testing.md).

## Reading paths

If you are new, read [Overview](docs/overview.md) first. It takes about ten
minutes, and every other page assumes it. After that, read what fits the change:

| You want to… | Read |
|---|---|
| Understand the idea and its rules | [Overview](docs/overview.md) → [Design principles](docs/design-principles.md) → [Glossary](docs/glossary.md) |
| Land your first change | [Getting started](docs/getting-started.md) → [Testing](docs/testing.md) → [Workflow](docs/workflow.md) |
| Change ingestion, reconstruction or node identity | [§2 Node store](docs/subsystems/02-node-store.md) → [§3 AST pipeline](docs/subsystems/03-ast-pipeline.md) |
| Change what an agent is shown | [§3.3 Dispatch](docs/subsystems/03-ast-pipeline.md#33-dispatch-bundle-enrichment) → [§5.3 Read sets](docs/subsystems/05-conflict-detection.md#prevention) |
| Change locking or scheduling | [§4 Lock manager](docs/subsystems/04-lock-manager.md) → [§6 Scheduler](docs/subsystems/06-scheduler.md) |
| Add or change a conflict check | [§5 Conflict detection](docs/subsystems/05-conflict-detection.md) → [Testing](docs/testing.md#guard-tests-by-area) |
| Add a commit-time check | [§5.3 Detection at commit](docs/subsystems/05-conflict-detection.md#detection-at-commit) → [§11 Run](docs/subsystems/11-session-lifecycle.md#run) |
| Add an agent backend or a provider | [§7 Agent runner](docs/subsystems/07-agent-runner.md) → [§8 Endpoints](docs/subsystems/08-endpoints.md) → [§12 Configuration](docs/subsystems/12-configuration.md) |
| Change the planner | [§9 Planner](docs/subsystems/09-planner.md) → [§11 Plan](docs/subsystems/11-session-lifecycle.md#plan) → [Planner input benchmark](docs/benchmarks-and-research.md#planner-input-offline) |
| Change the run loop, recovery or teardown | [§11 Session lifecycle](docs/subsystems/11-session-lifecycle.md) → [§10 Git integration](docs/subsystems/10-git-integration.md) |
| Add a CLI flag or slash command | [§13 Command line](docs/subsystems/13-command-line.md) → [§14 Interactive app](docs/subsystems/14-interactive-app.md) ("Adding a slash command") |
| Add a config key | [§12 Configuration](docs/subsystems/12-configuration.md) (the key itself is documented in `mak/config.yaml`) |
| Work on models or local runtimes | [§15 Model catalog](docs/subsystems/15-model-catalog.md) → [§16 Local runtimes](docs/subsystems/16-local-runtimes.md) |
| Run or change a benchmark or the study | [Benchmarks and research](docs/benchmarks-and-research.md) |

## Documentation map

### Understanding MAK

| Document | Covers |
|---|---|
| [Overview](docs/overview.md) | What MAK is, why not Git worktrees, the architecture at a glance, the end-to-end data flow, the mental model, and which package holds which piece |
| [Design principles](docs/design-principles.md) | The rules the code follows, each linked to the subsystem it shapes |
| [Glossary](docs/glossary.md) | Node, fragment, wave, read set, contract, repair obligation, endpoint, and the rest |
| [Diagrams](diagram/README.md) | Component architecture, execution sequence, and the commit pipeline (Mermaid sources and PNGs) |

### Subsystems

One reference per subsystem in [`docs/subsystems/`](docs/subsystems/). Each
one can be read alone, and each starts with the code and tests it covers. The
section numbers are stable: code comments and [`TASKS.md`](TASKS.md) cite them
as `CONTRIBUTING §N` or `§N.M`, and this table resolves them.

| § | Subsystem | Covers |
|:-:|---|---|
| [1](docs/subsystems/01-core.md) | Core types, exceptions, logging | `NodeId`, `TaskBundle`, `TaskResult`, `SubTask`; the exception tree; the event log and its event types; atomic writes, path containment |
| [2](docs/subsystems/02-node-store.md) | Node store | Node identity and whole-file nodes, the `.mak/` layout, the `NodeStore` API, the five kinds of undo, transactions, retention |
| [3](docs/subsystems/03-ast-pipeline.md) | The AST pipeline | 3.1 ingestion by span tiling · 3.2 `.makignore` · 3.3 bundle enrichment · 3.4 collection · 3.5 reconstruction · 3.6 the round-trip invariant |
| [4](docs/subsystems/04-lock-manager.md) | Lock manager | 4.1 lock modes · 4.2 the lock table · 4.3 deadlock detection · 4.4 the project lease · 4.5 derived lock resources and the lock policy |
| [5](docs/subsystems/05-conflict-detection.md) | Conflict detection | 5.1 structural checks · 5.2 wave-end checks · 5.3 semantic conflicts: prevention, the commit pipeline and its verdicts, parked commits, wave-end fix-ups and gates |
| [6](docs/subsystems/06-scheduler.md) | Scheduler | The DAG, atomic lock pre-allocation, persistence for recovery |
| [7](docs/subsystems/07-agent-runner.md) | Agent runner and adapters | 7.1 adapter interface · 7.2 API adapters · 7.3 output budget and stop signals · 7.4 CLI adapters · 7.5 registry and composition root · 7.6 wire protocol · 7.7 runner and sandbox · 7.8 local transports |
| [8](docs/subsystems/08-endpoints.md) | Endpoints | Transport, profile, endpoint and agent id; credential handling; structured-output negotiation and its invariants |
| [9](docs/subsystems/09-planner.md) | Planner | Strategies, prompt layout and caching, target rules, the inventory view, retrieval and expansion, caller tasks, telemetry, plan validation, review |
| [10](docs/subsystems/10-git-integration.md) | Git integration | Git as an audit log, private-index commits |
| [11](docs/subsystems/11-session-lifecycle.md) | Session lifecycle | The `Session` state machine and its collaborators, `WaveState`; initialize, plan, run, cascade waves and repair obligations, teardown and recovery |
| [12](docs/subsystems/12-configuration.md) | Configuration | Config discovery, validation rules, keys and the spend ceiling, packaged examples |
| [13](docs/subsystems/13-command-line.md) | Command line (`mak`) | The `mak` commands, the application API, `PlannerRoute`, `mak run`, the `--models` / `--planner` spec grammar, CLI agents |
| [14](docs/subsystems/14-interactive-app.md) | Interactive app (`cli/`) | UX, plan review, slash commands, config sync, planner route state, modes, `/local`, `/endpoint`, session-only configuration, API keys |
| [15](docs/subsystems/15-model-catalog.md) | Model catalog | Facts vs. judgment, the manifest, refresh scheduling and failure isolation, the runtime registry |
| [16](docs/subsystems/16-local-runtimes.md) | Local runtimes | The native Ollama client, runtime discovery, recommended local models |

### Developing

| Document | Covers |
|---|---|
| [Getting started](docs/getting-started.md) | Prerequisites, setup, keys for real calls, the annotated project layout |
| [Testing](docs/testing.md) | The three quality gates, golden event logs, the guard test for each area, hermetic-test rules |
| [Workflow](docs/workflow.md) | Coding standards; branches, commits and PRs; which document to update for which change |
| [Known limitations](docs/known-limitations.md) | Deliberate, fail-safe tradeoffs, and the waves that address them |

### Benchmarks and research

| Document | Covers |
|---|---|
| [Benchmarks and research](docs/benchmarks-and-research.md) | MAK vs. git worktrees, planner input size, the semantic conflict corpus, the simulated scaling sweep, the real-world contention study, and the rules for changing each |

### Elsewhere in the repository

| File | Covers |
|---|---|
| [`mak/config.yaml`](mak/config.yaml), [`mak/examples/`](mak/examples/) | The reference for every config key |
| [`TASKS.md`](TASKS.md) | Planned work, as waves |
| [`CHANGELOG.md`](CHANGELOG.md) | Every release |
| [`AGENTS.md`](AGENTS.md) | Rules for coding agents working in this repository |
| [`benchmark/README.md`](benchmark/README.md), [`research/contention_study/`](research/contention_study/) | Full method and results of each benchmark and the study |

## Where to contribute

### Open issues

All planned work lives in [`TASKS.md`](TASKS.md). It has a priority-ordered index
of **waves** (each one a branch, `feat/<wave>-<name>`), the rules that apply to
every wave, and, for each wave, the evidence, design decisions, implementation
steps, test matrix and acceptance criteria. Pick a wave or a step from there,
and open an issue to coordinate before you start a large one. Ideas not yet
assigned to a wave are listed at the end of that file.

Before working around something that looks like a bug, check
[Known limitations](docs/known-limitations.md). It may be deliberate.

### Good first contributions

- Test coverage for edge cases: ingestion corners, splat handling in signature
  checks, config coercion.
- Error messages that point at the fix.
- Clarifying a subsystem reference in [`docs/subsystems/`](docs/subsystems/),
  or adding module-level examples.
- Hardening a CLI bridge wrapper (`mak/agent_runner/wrappers/`) for a specific
  `claude` / `codex` / `gh copilot` version, or extending the sandbox (host
  allowlisting).
- Small, self-contained steps from [`TASKS.md`](TASKS.md): matching
  `.pre-commit-config.yaml` to CI, numeric pre-release tag ordering, and removing
  the legacy `mak/.env` (Wave R).

### Keeping the docs true

Every fact is documented in exactly one place. If your change makes a page
wrong, fix that page in the same change. [Workflow](docs/workflow.md#keeping-the-docs-true)
has the table of what lives where. If you add or move a document, update the
map above.

## License

[MIT](LICENSE) © 2026 Seungjoon Cha

By contributing, you agree that your contributions are licensed under the
project's MIT License.
