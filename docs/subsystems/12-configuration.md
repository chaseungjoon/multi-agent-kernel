# 12. Configuration

> [Contributing guide](../../CONTRIBUTING.md) · ← [11. Session lifecycle](11-session-lifecycle.md) · [13. Command line (`mak`)](13-command-line.md) →
>
> **Code:** `mak/config.py`, `mak/config.yaml`, `mak/examples/` · **Tests:** `tests/test_config.py`, `tests/test_config_validation.py`, `tests/test_example_configs.py`

`mak/config.py` loads YAML into a frozen `MakConfig` dataclass tree. The
annotated reference for every key is the packaged default,
[`mak/config.yaml`](../../mak/config.yaml): each setting is listed with its default and
what it does. The sections it leaves out are shown in the packaged examples under
[`mak/examples/`](../../mak/examples/) (`mak examples <name>` prints one):

| Example | Shows |
|---|---|
| `custom-endpoint.yaml`, `hosted-openai-compatible.yaml` | `endpoints:` and every capability setting ([§8](08-endpoints.md)), agents routed by `endpoint` |
| `local-ollama.yaml`, `local-openai-compatible.yaml` | `ollama_api` / `local_api` agents and their fields ([§7.8](07-agent-runner.md#78-local-transports)) |
| `hybrid-cloud-planner-local-agents.yaml`, `fully-local-offline.yaml` | planner `backend` / `base_url` / `endpoint` routes |

`tests/test_example_configs.py` loads and validates every example, so they cannot
drift from the schema. When you add or change a config key, document it in
`mak/config.yaml` (or an example), not here.

Rules:

- **Discovery.** Without `--config` / `/config`,
  `discover_config_path(work_dir)` picks the first file that exists:
  1. `<work_dir>/.mak/config.yaml` — the project's own config
     (`project_config_path(work_dir)`);
  2. `~/.config/mak/config.yaml` — the user's config (`user_config_path()`;
     honours `$XDG_CONFIG_HOME`);
  3. the packaged `mak/config.yaml`, when neither exists.

  `work_dir` is the project being edited, not the launch directory: `mak run`
  passes `--work-dir` (else the CWD), `mak gc` its argument, and the app its
  current work dir (`CliState.config_file()`). `seed_config_path()` is the file
  a new project config is copied from: the user's config, else the packaged
  default.
- **`agents`** is required and non-empty. Optional per-agent fields default to
  `None`, meaning "the adapter decides"; an explicit value is validated at load.
  `max_tokens` must be a positive integer; `repair_attempts` may be `0`.
  `structured_output` and `planner.backend` are checked against fixed choices at
  load. `base_url` goes through `normalize_base_url` (scheme and host required,
  trailing slash stripped) — the same rule the command line and the app use.
  `validate_config` rejects local-transport fields on a type that ignores them,
  and `local_api` without `base_url`.
- **Model choice lives in config.** `PlannerConfig.model` defaults to `""` and
  `AgentConfig.model` to `None`; no model name is hardcoded in the dataclasses.
  The app writes config only on an explicit model change.
- **Keys are never stored in config.** `api_key_env` names a variable read at
  composition time from an explicit `env` mapping ([§13](13-command-line.md)). Keys live in
  `~/.config/mak/.env` (created `0600`) or the environment; exported variables
  win. The in-package `mak/.env` is deprecated —
  it is still read with a warning (Wave R in [`TASKS.md`](../../TASKS.md)).
- **`session.max_total_tokens`** is the only spend ceiling ([§11](11-session-lifecycle.md)). `0` or negative
  is a `ConfigError`.
- **`node_store.version_retention`** must be `-1` or ≥ 2.
- **Planner bounds** are checked at load, naming the key: `strategy` is one of
  `auto` / `oneshot` / `outline` / `full` / `retrieval`;
  `inventory_token_budget` 2,000–200,000; `max_expansions` 0–10;
  `max_caller_tasks` 0–200; `auto_caller_tasks` and `prompt_cache` are booleans.
- **`session.mak_dir`** is anchored to `work_dir` by `config.anchor_mak_dir`, used
  by both front ends. `config.stale_mak_dir` reports a `.mak` left at a
  CWD-relative location that holds run state (`node_store/`, `task_graph.json`
  or `lock_table.json`); it is never adopted. A `.mak/` holding only a
  `config.yaml` is a configured project, not an orphan.
- **`exclude_patterns`** replaces the default list when set; per-project ignores
  belong in `.makignore`, which adds to it.
- **`semantic:`** — `stale_read` and `type_check` are validated at load;
  `impact_tests` / `import_smoke` accept a boolean or `"on"`/`"off"`;
  `adjudicator` is `"off"` or `"<backend>:<model>"` (built through
  `build_planner_llm`); counts must be non-negative and `gate_timeout_s`
  positive. Locking flags default **on** (they cost nothing extra); gates default
  **off** (each costs subprocesses, test runs, or a model call).
  `Session(gate_runner=…, adjudicator_llm=…)` makes the subsystem testable without
  tools or keys.
- **Model caveats.** `model_caveat(model_id)` returns a warning for models with
  footguns; every surface that selects a model prints it.
- Type coercion is strict (`"false"` parses to `False`) and wrapped in
  `ConfigError`.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [11. Session lifecycle](11-session-lifecycle.md) · [13. Command line (`mak`)](13-command-line.md) →
