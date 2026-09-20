# Changelog

All notable changes to **Multi Agent Kernel (MAK)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
All releases to date are pre-1.0 beta releases (PEP 440 `bN` suffix); the public
API is not yet stable and minor versions may carry breaking changes.

Versions are defined in `mak/_version.py`, which is the single source of truth
for packaging metadata and `mak.__version__`.

## [Unreleased]

Nothing yet.

## [0.8.0b] — 2026-09-20

### Added
- **Universal OpenAI-compatible endpoint abstraction (Wave 22).** Any service
  speaking the OpenAI Chat Completions API — NVIDIA Build, OpenRouter,
  DeepSeek, Z.ai, or a self-hosted gateway — is now a first-class MAK
  endpoint. New `mak/endpoints/` package separates transport, provider
  profile, endpoint, and agent id, which were previously conflated into a
  single `AgentConfig.type` field.
- **`/endpoint` command** in the interactive CLI: `add` (a preset or fully
  custom wizard), `list`, `show`, `edit`, `test`, `models`, `remove`,
  `export`. Saved endpoints persist in `~/.config/mak/endpoints.json`.
- **`endpoints:` config section**, plus `id`/`endpoint` fields on `agents[]`
  and `planner`, so `mak.yaml` can name a configured endpoint instead of
  repeating its URL and capabilities.
- **Multiple simultaneous OpenAI-compatible endpoints in one run** —
  `--models nvidia:<model> openrouter:<model>` and several models on the same
  endpoint both now work; a configured endpoint id is accepted anywhere a
  built-in provider name is.
- Model discovery, health checks (`models`/`chat`/`none` policies), and a
  four-rung structured-output ladder (`json_schema → json_object → none`)
  that now actually reaches its bottom rung and remembers per-`(endpoint,
  model)` what worked for the rest of the session.
- Two new packaged examples: `mak examples hosted-openai-compatible` and
  `mak examples custom-endpoint`.

### Changed
- `AdapterRegistry` is now keyed by **agent id**, not adapter type — a
  duplicate id is a startup error instead of one agent silently replacing
  another in the registry.
- The model manifest moved to **schema v2**, keyed by `(endpoint_id,
  model_id)` instead of `model_id` alone; a v1 manifest migrates forward
  automatically.
- `cli/core/api_keys.py`'s `save_keys` now parses, merges, and renders
  `~/.config/mak/.env` instead of rewriting it from a fixed set of three
  names — a save no longer risks deleting an unrelated key.

## [0.7.1b] — 2026-09-20

### Added
- **Simulated agent-scaling benchmark (Wave 21).** A discrete-event simulation
  runtime (`benchmark/sim/runtime.py`) and parameter sweep driver
  (`benchmark/sweep.py`) model kernel behaviour across agent counts without
  spending real API tokens. Ships with three sweep configurations
  (`smoke`, `kernel_only`, `paper`), a synthetic workload generator, and
  published scaling results.
- Scaling-results graphics and accompanying documentation.

### Changed
- Session emits additional scheduling telemetry consumed by the sweep harness.

### Fixed
- CI failure in the Wave 20 semantic session contract tests.

## [0.7.0b] — 2026-09-19

### Added
- **Semantic conflict detection & prevention (Wave 20).** A new `mak/semantic/`
  subsystem raises conflict detection from the syntactic to the semantic layer.
  It introduces interface contracts declared at plan time
  (`mak/planner/contracts.py`), a symbol registrar and read-set tracker,
  staleness analysis, a cascade graph, semantic gates with type checking and
  import smoke tests, and an adjudicator that arbitrates competing agent claims.
- **New conflict checks**: constructor compatibility, attribute access,
  duplicate definitions, and import-cycle detection.
- **Semantic lock policy** (`mak/scheduler/lock_policy.py`) and richer lock
  resources, so the scheduler can reason about semantic — not just symbol —
  contention.
- **Semantic benchmark suite** (`benchmark/semantic/`) with scripted scenarios
  and an evaluation harness.

### Changed
- Planner validation and the scheduler DAG account for declared contracts.
- `CONTRIBUTING.md` substantially expanded to document the semantic layer.

## [0.6.6b] — 2026-09-17

### Added
- **`.makignore` support.** Ingestion now honours a `.makignore` file
  (gitignore-style syntax) so projects can exclude paths from the node store.

## [0.6.5b] — 2026-09-17

### Added
- Mode switching commands in the CLI app, with expanded local-mode handling.

### Changed
- Restyled the local-model overview display.

## [0.6.4b] — 2026-09-17

### Added
- Local host management (`cli/core/local_hosts.py`) for configuring and
  selecting local model endpoints.

### Changed
- Reworked local model handling, selection, and tab completion in the CLI app.

## [0.6.3b] — 2026-09-17

### Changed
- Improved the local model fetch method and its completion behaviour.
- README overhauled with architecture graphics and diagrams.

## [0.6.2b] — 2026-09-15

### Added
- Fourth-round benchmark suite and a comparison diagram for the README.
- Author signature in the CLI app.

## [0.6.1b] — 2026-09-10

### Added
- **State preservation & truthful outcomes (Wave 19).** Addresses six priority
  defects that shared one root cause: consistency was not maintained across
  lifecycle boundaries.
  - `NodeStore.transaction()` defers destructive effects until a single commit
    point, and a write-ahead journal (`mak/node_store/journal.py`) makes an
    interrupted commit recoverable by a later process.
  - `sync_file` reconciles the node store against the working tree at session
    start, replacing one-directional ingestion; external edits are detected via
    `file_state.json` and resolved by an `adopt` or `conflict` policy.
  - `commit_task` builds in a private `GIT_INDEX_FILE` seeded from HEAD, so MAK
    no longer consumes the user's Git index.
  - Added project leases (`mak/lock_manager/project_lease.py`), node retirement
    that records deletions without destroying history, and a teardown module.

### Fixed
- Wave completion is now recorded only past the commit point, so interrupted
  runs no longer report work they did not durably write.

## [0.6.0b] — 2026-09-03

### Added
- **Local model support (Wave 15).** Run MAK entirely against locally hosted
  models: a new Ollama API adapter, local model discovery, and a `cli/local.py`
  local-model front end with setup and configuration flows.
- Structured agent result schema (`result_schema.py`) with a repair path for
  malformed model output.

### Changed
- OpenAI, Anthropic, and Gemini API adapters reworked to share the new result
  schema and repair machinery.
- Bootstrap and configuration extended to validate local provider setups.

## [0.5.10b] — 2026-08-19

### Added
- **Proper ingestion (Wave 18).** Ingestion correctly walks and fragments a
  project into the node store, with configuration knobs and logging for the
  ingestion path.

### Changed
- Session and node store rewritten around the corrected ingestion model.

## [0.5.9b] — 2026-08-15

### Added
- **Write-path containment & state durability (Wave 17).**
  - New `mak/core/paths.py` gates every boundary where a model-supplied node id
    becomes a filesystem path, closing a path-traversal hole where
    `Path(work_dir) / "/etc/x.py"` escaped the work directory and `..` walked
    out of the store root. `install_plan` gained its own gate, since the TUI and
    cascade waves bypass `parse_plan` entirely.
  - `.mak` is now anchored under the work directory, so two projects driven from
    one shell no longer share a node store.
  - All three state files write atomically (temp + `fsync` + `os.replace`), and
    corrupt reads follow a per-store recovery policy instead of raising out of a
    constructor.
  - Per-request SDK timeouts from `AgentConfig.timeout`; `AgentRunner` takes the
    work directory at construction, fixing CLI agents spawning in the CWD and
    `--sandbox` mounting the wrong tree.

### Fixed
- Digest-suffixed fix-up task ids: two paths sanitising to the same id
  previously took down whole cascade waves.

## [0.5.8b] — 2026-08-07

### Fixed
- Agent fragment shaping: malformed fragment payloads from agents are now
  detected and reported through explicit domain exceptions rather than
  corrupting the node store.

## [0.5.7b] — 2026-08-06

### Changed
- **Bound the caller layer, and make the guards reachable (Wave 16).**
  - Whole-file targets derived cross-file search symbols from every top-level
    binding, so `__all__` counted as a symbol and pulled in every other module
    declaring one. A symbol is now restricted to names that can be node ids.
  - Context enrichment layer 4 gained a real ceiling: it ranks candidates by
    match count and spends a configured byte budget, dropping rather than
    digesting past it. Observed cross-file context fell from 151 KB to 16.4 KB.
  - The cross-module check moved to `mak/cascade.py` so both the `mak run`
    command and the interactive app drive it.

## [0.5.6b] — 2026-08-05

### Added
- **Dependency context propagation (Wave 13).** A task now receives the source
  of what its dependencies built.
  - Plan validation no longer deletes context nodes created by a sibling task in
    the same plan, and adds the ordering edge that makes them readable.
  - A fifth enrichment layer carries each direct dependency's committed output,
    bounded by `session.dependency_context_bytes` and degrading to a public API
    digest (`mak/node_store/api_digest.py`) rather than to nothing.
  - A post-wave cross-module check
    (`mak/conflict_detector/cross_module_check.py`) reports modules created in
    the same wave that disagree about each other's API, filed as fix-up tasks.
- `CODE_OF_CONDUCT.md`.

### Fixed
- The kernel refuses to dispatch an empty context bundle to a task that has
  dependencies, instead of silently letting agents guess at APIs.

## [0.5.5b] — 2026-07-31

### Fixed
- `AgentProtocolError` raised during session execution.

## [0.5.4b] — 2026-07-31

### Added
- **Token budgeting and stop signals** (issue #9): a budget module
  (`mak/core/budget.py`), per-adapter budget enforcement, and cooperative stop
  signals (`mak/agent_runner/stop_signals.py`) across the Anthropic, OpenAI, and
  Gemini adapters.
- New domain exceptions for budget and protocol failures.

## [0.5.3b] — 2026-07-30

### Fixed
- **Pipeline integrity (Wave 11).** Three defects that caused MAK to discard or
  corrupt correct work without being able to explain why:
  - The signature check rejected correct Python. `@staticmethod` and
    `@classmethod` are now read from the decorator list; attribute calls resolve
    by receiver rather than by bare name; methods are keyed `Class.method` so
    two classes in one file no longer shadow each other.
  - The node store ingested its own persistence directory — `glob("**/*.py")`
    descended into `.mak/`, re-ingesting the previous run's output (+325 nodes
    per run, 89% of the store garbage). The mak directory is now excluded.
- Anthropic SDK non-streaming requests respect a token ceiling.

### Added
- `ruff` added as a declared dependency.

## [0.5.2b] — 2026-07-27

### Fixed
- **Planner robustness.** New response-handling module
  (`mak/planner/response.py`) tolerates malformed, truncated, and
  fence-wrapped planner output instead of failing the run.

## [0.5.1b] — 2026-07-25

### Added
- **Automatic model list fetch (Wave 14).** A new `mak/models/` package
  (catalog, curation, manifest, providers, refresh, registry) discovers
  available models from each provider at runtime, replacing the hand-maintained
  registry. Ships with a seed manifest and CLI commands to refresh it.

## [0.5.0b] — 2026-07-23

### Added
- **Planner quality (Wave 10).** A dependency graph builder
  (`mak/planner/depgraph.py`) and a plan validation layer
  (`mak/planner/validation.py`) that checks plans before execution, plus a
  planner review pass.
- Third-round benchmark suite.

## [0.4.1b] — 2026-07-19

### Fixed
- Name collision detection in the node store.

## [0.4.0b] — 2026-07-18

### Added
- **Agent CLI adapters.** MAK can drive Claude Code, Codex, and GitHub Copilot
  as execution agents, via a new wrapper bridge
  (`mak/agent_runner/wrappers/bridge.py`) that translates between the kernel's
  protocol and each CLI's interface.
- `ensure update` command for keeping an installation current.

### Changed
- CLI update mechanism and UI refinements.

## [0.3.2b] — 2026-07-17

### Added
- Fable 5 model support in the model registry and configuration.

## [0.3.1b] — 2026-07-17

### Added
- `uv`-based installation path, with reworked API key handling and state
  management.

## [0.3.0b] — 2026-07-09

### Changed
- **CLI redesign.** Sleek Claude Code-style layout, purple accent, and refreshed
  documentation.

### Fixed
- Binary project-root mismatch launch bug.
- CLI configuration modification bug.

## [0.2.1b] — 2026-06-17

### Added
- Planner switch command in the CLI app, with tab completion.

## [0.2.0b] — 2026-06-17

### Added
- **Interactive CLI application** (`cli/`): app shell, command set, tab
  completion, setup wizard, API key handling, session state, and UI layer,
  launched via the `bin/mak` entry point.

### Fixed
- `list_nodes` returning empty fragments.
- Agents returning empty `modified_fragments`.
- Validation paths use `compile()` instead of `ast.parse()`.

## [0.1.7b] — 2026-06-16

### Added
- Codebase-wide node context search.
- Dynamic task generation, with cascading-task prevention enforced through
  planner prompt instructions.

## [0.1.6b] — 2026-06-14

### Fixed
- Node store persistence issue.

## [0.1.5b] — 2026-06-14

First release with a centralised version number; everything prior is summarised
under *Early development* below.

### Added
- Single-source-of-truth versioning via `mak/_version.py`, consumed by
  `pyproject.toml` and `mak.__version__`.

## Early development — 2026-05-03 to 2026-06-14

Pre-`0.1.5b` history, before versioning was centralised in `mak/_version.py`
(the README badge read `0.0.4 Beta` at the end of this period). Highlights, in
order:

- Initial project structure, node store, and task model (Waves 0–1).
- **AST concurrency fix**: the core bug in fragment-level concurrent editing
  (Wave H, hardening).
- Google Antigravity agent adapter.
- Conflict detection and the API-based agent runner; fixes for scheduler
  stalling and the "blind agent" problem.
- `AdapterRegistry` and composition-root fixes.
- CLI integration and sandboxing.
- **True concurrency** (Wave 5): multiple agents editing one shared working
  directory simultaneously.
- First benchmark suite and the demo project.
- MIT license, `CONTRIBUTING.md`, and initial documentation.
- Greenfield file creation, end-to-end; multi-run validation and the
  second-round benchmark suite.
- Fixes for the double fragmentation bug, broken decomposition being rejected at
  plan time, agent source not being carried, and the lock table not being
  cleared on a fresh start.

---

## Notes

- **No Git tags.** Releases have not been tagged in this repository; version
  history above was reconstructed from `mak/_version.py` across the commit log.
- **Wave numbering.** Feature work is organised into numbered *waves* (see
  `AGENTS.md` and `CUR_WAVE.md`). Wave numbers do not map onto version numbers,
  and waves were not always released in numeric order — Wave 15 shipped in
  `0.6.0b`, after Waves 16–18.
- **Version anomaly.** `0.5.0b` was briefly committed as `0.5.1b` and corrected
  in the following commit; the release is recorded here under `0.5.0b`.
