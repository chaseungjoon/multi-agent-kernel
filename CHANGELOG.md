# Changelog

All notable changes to **Multi Agent Kernel (MAK)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
All releases to date are pre-1.0 beta releases (PEP 440 `bN` suffix); the public
API is not yet stable and minor versions may carry breaking changes.

Versions are defined in `mak/_version.py`, which is the single source of truth
for packaging metadata and `mak.__version__`.

## [Official Release]

Nothing yet.

## [0.9.3b] — 2026-09-25

### Changed
- **Config discovery follows the project, and the project's config lives in
  `.mak/`.** Without `--config` or `/config`, MAK now uses
  `<work dir>/.mak/config.yaml`, then your user config
  `~/.config/mak/config.yaml` (or `$XDG_CONFIG_HOME/mak/config.yaml`), then the
  built-in default. The work dir is `--work-dir` (or the launch directory) for `mak run`
  and `mak gc`, and the app's current work dir — so `/work-dir` now switches the
  config too. A `mak.yaml` in the project directory is no longer read; move it
  to `.mak/config.yaml`.
- The `/local` wizard's "save this setup" writes `<work dir>/.mak/config.yaml`
  (was `./mak.yaml`), and the `mak examples` templates suggest that location.
- `mak run` and the interactive app now build their config, planner route,
  planner key and session through one shared application API
  (`mak/application/`), so the same settings behave the same in both. As part
  of that, the app always applies its own planner and work dir over the config
  file's, and shows the validated plan (the same one `mak run` reviews).
- `/local off` with a local planner switches the planner to the default hosted
  model for your keys, instead of leaving a local model with no route.

### Added
- **The app offers to create a project config.** When it starts in, or
  `/work-dir` moves to, a directory without a `.mak/` folder, it asks whether to
  create `.mak/config.yaml`, copied from `~/.config/mak/config.yaml` (or the
  built-in default). Answering no writes nothing; `.mak/` is created on the first run as
  before. `mak run` never asks.
- CI runs on Python 3.13 as well as 3.11.

### Fixed
- **A failing slash command no longer ends the session.** An unexpected error
  prints one line and returns to the prompt, keeping the work dir, planner,
  models, mode and keys; the traceback goes to the debug log.
- **The same settings could route the planner's key differently in the app and
  in `mak run`.** There is now one resolver. A local or gateway planner named
  in a config file no longer receives a cloud key guessed from its model name.
- The app no longer exports your API keys into its process environment when it
  builds a session.
- Model catalog entries from third-party endpoints (e.g. OpenRouter) no longer
  raise when asked for their key variable or adapter type; they answer "none"
  and the endpoint decides.
- `include_patterns` ending in `**` (e.g. `src/**`) now match every file below
  them on every Python version; on Python 3.11/3.12 they matched nothing.
- A `.mak/` folder that holds only a `config.yaml` is no longer reported as a
  leftover state directory from an older MAK.
- A `/local` setup saved with an endpoint planner now keeps the endpoint.
- The test suite no longer reads the developer's real configuration and is
  green on Python 3.13.

## [0.9.2b] — 2026-09-23

### Changed
- **`/planner` now takes `<provider>:<model>`, the same format as `/models`.**
  Agents were chosen as `/models anthropic:claude-opus-5` but the planner as
  `/planner claude-opus-5`. Besides the mismatch, a bare id was ambiguous once
  one model could be served by several providers (e.g. `anthropic:` directly
  or through an `openrouter:` endpoint). The provider is now required and
  recorded, so the planner goes to the route, key and bill that were named.
  `/planner` accepts `anthropic|openai|gemini:<model>`, `<endpoint>:<model>`,
  `ollama:<model>` and `local:<model>[@url]`. A hosted model is checked against
  that provider's catalog, and a bare id is refused with the matching specs
  suggested (`did you mean anthropic:claude-opus-5?`).
- The planner is shown as `provider:model` everywhere: `/status`, the bottom
  toolbar, the `/planner` list and Tab completion, the setup wizard, and the
  `/local` and `/mode` pickers.

### Added
- **`mak run --planner PROVIDER:MODEL[@URL]`** sets the planner from the
  command line with the same format as `--models` (the model is required),
  replacing the config's planner route for that run. Before this, the planner
  could only be changed by editing the config file.

### Fixed
- A planner moved off an endpoint (e.g. from `openrouter:…` to a hosted or
  local model) no longer keeps routing through that endpoint. The old
  endpoint route was never cleared.
- A cloud planner chosen in the app no longer fails validation when the config
  file names a `planner.endpoint`. The app's choice now replaces it.
- The "no longer offered" warning matches on provider and model, so a model
  retired by one provider no longer flags the same id served by another.
- `/refresh-models` no longer hangs when a saved local host is unreachable.
  Listing a local runtime is now capped at 2 seconds (it used the 60-second
  generation timeout), and the Gemini model-list fetch now has a timeout.

## [0.9.1b] — 2026-09-22

### Fixed
- **A cascade repair can no longer report success without repairing anything
  (Wave 26).** An agent's success is only a transport claim — it says the model
  returned usable source, not that the defect is gone. A repair that swapped
  one nonexistent import for another nonexistent import at the same site was
  therefore accepted, committed, and re-detected on the next wave.

  Every generated fix-up now carries kernel-owned `RepairObligation`s that the
  planner and the coding agent cannot declare or weaken. Before a repair
  commits, MAK substitutes the staged sources into a whole-repository view and
  reruns the deterministic cross-module checks; an obligation still true in that
  prospective repository rolls the edit back **before** the node-store
  transaction and the git audit commit, so a hallucinated fix never becomes
  durable. Each obligation records both the finding's exact identity and a
  stable *family* identity for its syntactic site, so substituting a different
  guessed name at the same import site is not mistaken for progress.
- **Cascade loops that cannot converge now stop instead of asking again.**
  Before presenting another wave, the loop fingerprints the implicated source,
  the repair scope, and the obligation families. An immediate repeat of the
  previous broken state stops as `stalled`; a non-adjacent repeat (A → B → A)
  stops as `oscillating`. Fingerprints are persisted alongside the task graph,
  so a session recovered after a crash cannot resume the same approval loop from
  the beginning.
- **`no_changes_required` can no longer close a live repair.** The same
  prospective predicate gates the no-op acceptance path, so an agent cannot
  discharge a fix-up task by declaring there was nothing to do.
- **An unresolved import against an empty provider no longer invites a
  caller-only rename.** When the provider exports no statically visible symbol
  that could satisfy the use, the provider becomes a writable target of the
  repair and the originally requested binding must exist when the edit commits —
  deleting the caller's use is not a valid fix.
- **Ordinary caller edits can no longer introduce a new cross-module defect
  against an untouched provider.** Prospective validation compares the candidate
  repository against the committed baseline and rejects newly introduced
  defects, including newly created import cycles.
- **A repair no longer optimizes for the latest diagnostic at the expense of the
  request.** The original user objective — supplied through `plan()` or through
  `install_plan(..., objective=...)` — is carried in the scheduler annotations,
  survives crash recovery, and is repeated in every generated repair
  description, so deleting the feature that caused a defect is not a
  structurally clean way out.

### Added
- **`RepairObligation`** (`mak/core/types.py`, exported from `mak.core`) and the
  `SubTask.repair_obligations` field. Obligations are serialized with the task
  graph for crash recovery, shown during plan review as a `must resolve=` line,
  preserved when `_merge_fixups` folds several fix-ups into one task, and
  reattached to the right downstream task after a reviewer edits a generated
  plan. An edited plan that keeps neither the caller nor the provider is
  rejected outright as unrepairable.
- **`CascadeOutcome.stalled`, `.oscillating`, `.unrepairable`, and
  `.stop_reason`**, joining the existing `declined` and `limit_reached`. Each
  non-clean stop is now reported by name with deterministic evidence — in the
  `mak` CLI on stderr and in the interactive app's cascade summary — and counts
  against `ok`, so the run exits non-zero instead of returning the last
  successful wave as if the work had finished.
- `Session.cascade_state_fingerprint()`, `.cascade_history()`, and
  `.remember_cascade_state()`, the session-side API behind stall and oscillation
  detection. A state is remembered only once a concrete repair plan is about to
  run, so a declined wave does not poison a later recovery.
- 19 new tests across cascade control flow, prospective repair validation, plan
  review rendering, and crash recovery.

### Changed
- `install_plan()` accepts an optional `objective=` keyword so a caller that
  bypasses the planner can still supply the durable repair context.
- `CrossModuleDefect` gained `subject` and `site` fields plus `exact_key` and
  `family_key` properties. `site` identifies the syntactic location of an import
  or call and survives a name substitution there, which is what makes
  "a different wrong name in the same place" detectable as a non-repair.
- A clean finish and the wave ceiling still get one final confirmation pass; the
  other stop paths now retain the batch that forced the stop, so `unresolved`
  names what was actually outstanding at that moment.

## [0.9.0b] — 2026-09-21

### Fixed
- **OpenRouter models that do not support structured outputs now work as
  coding agents (Wave 24).** Agent tasks against such a model failed every
  time: MAK asks for a JSON-schema reply to obtain a `TaskResult`, the
  upstream provider refuses the parameter, and the refusal was not recognized
  — so MAK never fell back and the scheduler re-dispatched the task until it
  failed. The 0.8.1b fix matched the literal text `structured outputs`, but
  the same provider relays the same refusal as `structured-outputs` for a
  different request, and only one of the two spellings was caught.

  Refusals are now identified by parsing the provider's structured error body
  — including OpenRouter's nested `error.metadata.raw` — with punctuation and
  casing normalized, so all spellings of one refusal are recognized as one
  fact. Verified against the live API for
  `inclusionai/ling-3.0-flash-vl:free`.
- Unrelated provider failures are no longer answered by asking for a weaker
  reply format. An expired key, an unknown model, a quota breach, a context
  overflow, a safety rejection, a 5xx, a transport error, and an invalid
  schema authored by MAK now each surface as themselves instead of becoming a
  second, more confusing failure one rung down.

### Added
- **Model capabilities are read from the endpoint instead of discovered by
  failing.** MAK now keeps the `supported_parameters` list that OpenRouter (and
  any compatible service) publishes for each model, and asks for the strongest
  reply format that model actually supports. For a model known not to accept
  structured output this removes the wasted request entirely: one call per
  task instead of three. A model that publishes nothing is negotiated at
  runtime exactly as before.

  The distinction is per **exact** model id: `inclusionai/ling-3.0-flash-vl`
  supports structured outputs and `inclusionai/ling-3.0-flash-vl:free` does
  not, so a variant suffix is never canonicalized away.
- `response_format` and `structured_outputs` are treated as the separate
  parameters they are — the former authorizes JSON-object mode, the latter
  strict JSON-schema mode. 30 models in OpenRouter's current catalog support
  one and not the other, and they now start at the rung they can actually
  serve.
- **`provider_routing` endpoint setting** (`none` | `openrouter`). On the
  OpenRouter preset, MAK asks that requests be routed only to upstream
  providers that honor the parameters it sent, but only for a model whose
  support is published — sending that hint speculatively causes a routing
  failure rather than a recoverable one. It is never sent to any other
  endpoint, and never inferred from a hostname, so a proxied or renamed
  endpoint is not guessed at.
- Concurrent agents sharing one endpoint and model now perform **one** shared
  capability discovery between them rather than each paying for the same
  rejected requests.
- One log line per endpoint and model naming the reply format chosen and
  whether the evidence came from the endpoint's catalog or from a refusal at
  runtime. It carries no API key, request header, provider response body, or
  prompt content.

### Changed
- **Model manifest schema 2 → 3**, adding per-model capability data. Existing
  caches migrate in place with no model loss and no refetch; the new field is
  simply unknown until the next refresh fills it in.

## [0.8.1b] — 2026-09-20

### Fixed
- Selecting a newly added endpoint for agents now replaces the previous agent
  roster, matching the CLI confirmation instead of silently retaining older
  models.
- Configured OpenAI-compatible endpoints now appear in `/models`, `/planner`,
  and model completion menus, and are restored when the CLI restarts.
- `/refresh-models` now fetches model catalogs from configured endpoints in
  addition to the built-in providers.
- OpenRouter upstream errors that report unsupported "structured outputs" are
  recognized as response-format rejections, allowing agents to fall back from
  JSON Schema through JSON Object to prompt-only JSON.

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
