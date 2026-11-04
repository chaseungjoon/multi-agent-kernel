# 13. Command line (`mak`)

> [Contributing guide](../../CONTRIBUTING.md) · ← [12. Configuration](12-configuration.md) · [14. Interactive app (`cli/`)](14-interactive-app.md) →
>
> **Code:** `cli/__main__.py`, `mak/__main__.py`, `mak/application/` · **Tests:** `tests/test_main.py`, `tests/application/`

`cli/__main__.py` is the `mak` console script:

| Command | Action |
|---|---|
| `mak` | launch the interactive app ([§14](14-interactive-app.md)) |
| `mak run --task "..."` | one non-interactive run (`mak/__main__.py`) |
| `mak gc [work_dir]` | apply version retention and remove orphaned fragment directories (takes the project lease) |
| `mak examples [name]` | list or print a packaged config from `mak/examples/` (`config.example_path` refuses names that resolve outside that directory) |
| `mak update` | reinstall the newest release tag via `uv tool install git+…@<tag>` |
| `mak --version` / `--help` | |

`mak update` resolves tags with `git ls-remote --tags` (peeled entries win),
orders them with `_version_key` (a plain release above its pre-releases), skips
the reinstall when already current (PEP 610 `direct_url.json`), and falls back to
`main` — and says so — while the repo publishes no tags. See
Wave R in [`TASKS.md`](../../TASKS.md) for its open problems.

## The application API (`mak/application/`)

Both front ends turn settings into a session through this package and nothing
else, so the same settings cannot behave differently depending on which one
launched the run. Neither front end writes `os.environ`; every credential read
goes through an explicit `env` mapping.

| Module | Contents |
|---|---|
| `request.py` | `RunRequest` (frozen): `config_path`, `work_dir`, `model_specs`, `planner` (a `PlannerRoute`, or a spec parsed against the loaded config's endpoints), `max_agents`, `default_agent`, `sandbox`, `verbose`, `api_keys`, `no_review` |
| `config.py` | `build_config(request, *, env, anchor=True)` — discover or load the file, apply the overrides, validate, anchor `mak_dir`. `anchor=False` lets `mak run` look for a stale store first |
| `route.py` | `PlannerRoute` — the planner's route as one value (below) |
| `keys.py` | `resolve_planner_key(config, env)` — the **only** planner-key resolver; `planner_endpoint(config, *, env)` |
| `session.py` | `build_session(config, *, env, sandbox, default_agent)` — assembles every collaborator and runs the health preflight |
| `env.py` | `read_env_files()` — MAK's `.env` files as a mapping, nothing exported; `load_env_file()` — the same files `setdefault` into `os.environ`, for the one-shot `mak run` only |

**`PlannerRoute`** is frozen and complete by construction: `kind` is `hosted`
(`provider`, plus an optional gateway `base_url` for `openai`), `endpoint`
(`endpoint_id`), or `local` (`backend` `ollama`/`openai` and `base_url`);
`__post_init__` refuses a missing field or a field of another kind. Build one
with `hosted()`, `endpoint()`, `local()` or `from_spec()`; `spec()` renders it
back. `apply(planner_config)` rewrites **every** route field (`endpoint`,
`backend`, `base_url`, `api_key_env`) and keeps the rest: a direct hosted route
names its provider's key variable, a gateway names none.

**Planner key resolution** (`resolve_planner_key`), in order: the endpoint's
credential; `planner.api_key_env`; the conventional variable of a hosted
`planner.backend` with no `base_url`. A config that names any other backend or a
`base_url` (a local runtime, a gateway) gets `None` — never a cloud key. Only a
config naming no route at all falls back to the model-id prefix.

`tests/application/test_parity.py` builds the same logical settings (hosted,
endpoint, local and gateway planners, a roster with an endpoint,
`--max-agents`) through `mak run` and through the app and asserts an identical
`MakConfig` and planner key.

## `mak run`

`mak/__main__.py` keeps argparse, `main`, reporting and warnings:

- `load_env_file()` loads `~/.config/mak/.env` (and the deprecated `mak/.env`)
  into `os.environ` with `setdefault`.
- `parse_args` — `--task` (required unless `--recover`), `--config`,
  `--work-dir`, `--models`, `--planner`, `--max-agents`, `--agent`,
  `--no-review`, `--recover`, `--sandbox`, `-v/-vv`. `request_from_args(args)`
  turns them into a `RunRequest`.
- `main(argv, *, session_builder=…)` builds the config with
  `build_config(request, anchor=False)`, reports a stale store
  (`stale_mak_dir`), anchors `mak_dir`, builds the session through
  `application.build_session`, then drives initialize → plan → run → cascade
  loop → teardown. `session_builder(args, config, sandbox)` is the test seam.
  Exit codes: `0` success; `1` aborted review, planner failure, unsatisfied run,
  failing tests; `2` config error or missing Docker under `--sandbox`. The
  summary prints completed / failed / skipped / blocked with each failure's
  reasons. Under `--no-review`, cascade waves are skipped with a warning.
  `warn_model_caveats` and `warn_local_planner_mismatch` (all agents local but
  the planner hosted) print on stderr. `mak run` never prompts.

## Spec grammar: `--models` and `--planner`

Both take `provider[:model][@base_url]`. `_split_spec` splits on the **first**
`@` (a URL may carry `user:pass@`) and then the **first** `:` (Ollama tags contain
a colon). The provider position is resolved in order: a configured endpoint id,
then the hosted providers, then `ollama` / `local`.

| Provider | Adapter `type` | Key variable | Default model |
|---|---|---|---|
| `anthropic` | `anthropic_api` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` |
| `openai` | `openai_api` | `OPENAI_API_KEY` | `gpt-5.6-sol` |
| `gemini` (alias `google`) | `gemini_api` | `GEMINI_API_KEY` | `gemini-3.5-flash` |
| `ollama` | `ollama_api` | none | required |
| `local` | `local_api` | none | required |
| `<endpoint id>` | from the endpoint's transport | the endpoint's `api_key_env` | required |

- `@base_url` is accepted on `openai` (a gateway), `local`, and `ollama`; on
  `anthropic`/`gemini` it is a `ConfigError`. `ollama` defaults to
  `$MAK_LOCAL_BASE_URL`, then `http://localhost:11434`; `local` has no default.
- `--models` replaces the whole roster; the first entry is the default agent
  (`--agent` overrides). Several models on one endpoint, and several endpoints on
  one transport, coexist — uniqueness is by **agent id**.
- `--planner` uses the same grammar through `PlannerRoute.from_spec`, with two
  differences: the **model is required**, and **every route field is
  rewritten** by `PlannerRoute.apply`, keeping non-route settings. The provider
  is mandatory because one model id can be served by several providers with
  different keys and bills.
- **`--max-agents N`** sets `max_concurrent_agents` (live concurrency).
  `max_instances` is a different knob: the idle CLI-subprocess pool size.

```bash
mak run --task "..." --work-dir ./proj \
  --models anthropic:claude-opus-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash
mak run --task "..." --work-dir ./proj --models anthropic --max-agents 5
mak run --task "..." --work-dir ./proj \
  --models ollama:qwen2.5-coder:14b --planner openrouter:anthropic/claude-opus-5
```

## Using a local CLI agent

CLI agents are configured by `type` in `agents:` — there is no `--models`
shorthand:

```yaml
agents:
  - type: claude_code       # drives `claude` through the bridge wrapper
    max_instances: 2
    timeout: 300
  # - type: codex
  # - type: copilot         # `gh copilot` — the weakest fit for node rewrites
```

The binary must be on `PATH` (the health preflight runs the wrapper's
`--health-check`). Override the invocation with `MAK_CLAUDE_CODE_CMD`,
`MAK_CODEX_CMD`, or `MAK_COPILOT_CMD`.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [12. Configuration](12-configuration.md) · [14. Interactive app (`cli/`)](14-interactive-app.md) →
