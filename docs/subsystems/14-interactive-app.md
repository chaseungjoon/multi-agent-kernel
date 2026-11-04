# 14. Interactive app (`cli/`)

> [Contributing guide](../../CONTRIBUTING.md) · ← [13. Command line (`mak`)](13-command-line.md) · [15. Model catalog](15-model-catalog.md) →
>
> **Code:** `cli/` · **Tests:** `tests/test_cli_*.py`

`cli/` is an inline REPL on `prompt_toolkit` + `rich`. It uses MAK as a library
through public entry points only — the application API ([§13](13-command-line.md)) to build the
session, then `session.initialize()`, `session.propose_plan()`,
`session.install_plan()`, `session.run()` — and runs the same cascade loop as
`mak run` (`mak.cascade`), honouring `/no-review`.

**UX.** One accent colour (`ACCENT` in `cli/ui.py`), flat indented lists. A
compact welcome box; a bottom toolbar with live state (models, planner, agents,
work dir, approval, mode, session tokens), so commands print only a one-line
`✓`/`⚠`/`✗`; a `/` completion menu driven by `MakCompleter`; Ctrl+J for a newline.
A task: capture HEAD → build and initialize a session → plan (spinner) → show the
plan → optional approval → run (progress bar) → results → per-file `+N -N` diff
of `{pre_hash}..HEAD`. The token counter reads `Session.total_tokens`.

**Plan review.** `show_plan(console, subtasks, proposed=…, findings=…)` marks the
caller tasks MAK added (`proposed by MAK`), then prints one summary line ("MAK
added 3 caller-update tasks for 2 signature changes") and the number of advisory
findings. With proposed tasks the approval prompt (`MakCli._confirm_proposal`)
accepts **`y`** (run all), **`o`** (run without MAK's tasks — `drop_tasks`) or
N; otherwise it is the plain y/N `_confirm_plan`, which cascade waves also use.

| Command | Description |
|---|---|
| `/models [spec …]` | select agents with the `--models` grammar; in local/hybrid mode a bare `/models` lists the runtime's models live |
| `/planner [spec]` | set the planner with the `--planner` grammar; a bare id is refused with every matching spec suggested; a hosted model is checked against that provider's catalog |
| `/refresh-models` | re-fetch the cloud catalog now, printing a per-provider diff |
| `/local [sub-command]` | local runtime setup (below) |
| `/mode [cloud\|local\|hybrid]` | show or switch how the session gets models |
| `/endpoint [sub-command]` | manage OpenAI-compatible endpoints (below) |
| `/max-agents N` | concurrency limit |
| `/work-dir <path>` | working directory; offers a project config (below) |
| `/apikey` | add or update API keys |
| `/config [path]` | use a config file; bare returns to discovery from the work dir |
| `/no-review [true\|false]` | toggle plan approval |
| `/status`, `/help`, `/clear`, `/exit`, `/quit` | |

**The config is the starting point** (`cli/config_sync.py`).
`sync_with_config(state)` runs at startup and whenever the config in effect can
change (`/work-dir`, `/config`, creating a project config). It reads
`state.config_file()` and sets `state.planner` to
`PlannerRoute.from_planner_config(config.planner)` — the inverse of `apply`,
inferring the provider from the model id when the section names no route — and
`state.config_roster` to the config's agents rendered in the `/models` grammar
(`agent_label`). Until the user chooses, the app shows exactly what the config
says and a run uses the config's `planner:` and `agents:` sections verbatim, with
every per-agent setting (`max_instances`, `timeout`, …) intact. A config whose
planner a route cannot hold pins `cli.core.models.default_planner_route`
instead, so a run always has a planner; an unreadable config leaves the state
unchanged for the run to report.

**Planner route state.** `CliState.planner` is one `PlannerRoute` ([§13](13-command-line.md)).
`planner_pinned` is false while it only mirrors the config. Every setter —
`pin_planner(route)`, `set_cloud_planner(provider, model)`,
`set_endpoint_planner(endpoint_id, model)`,
`set_local_planner(backend, model, base_url)` — assigns a whole new route and
pins it, so there is nothing to clear and no stale field can misroute the
planner; a pinned planner survives `/work-dir` and `/config`.
`planner_model`, `planner_backend`, `planner_base_url` and `planner_endpoint_id`
are read-only views for display code. `planner_spec()` renders the route as
`provider:model[@url]` everywhere it is displayed (a local planner on the active
host omits its `@url`). `/local off` with a local planner falls back to
`cli.core.models.default_planner_route` — the recommended planner of the first
provider with a key. The retired-model warning matches on `provider:model`.
`/local planner <model>` takes a bare name because the active runtime already
names the provider. `tests/application/test_route.py` asserts that any sequence
of setters leaves exactly one route kind.

**Modes.** `CliState.mode` is `cloud`, `local`, or `hybrid`, shown in the toolbar
and `/status`. It decides which surfaces validate against keys and which against a
local runtime; the roster is always `selected_models`, parsed through
`agents_from_specs`. First run (`cli/setup.py::run_setup`) runs a background
`discover()` and asks which mode to use: **Cloud** runs the key wizard, **Local**
runs the `/local` wizard with no key, **Hybrid** asks for a planner key then runs
`/local` for agents. The app reaches the prompt with no API key at all.

**`/local`** (`cli/local.py`): bare `/local` detects runtimes, picks one, picks an
agent model (installed models with size and quantization, or curated suggestions
pulled with a progress bar), picks a planner (recommended from the curated
table's `is_small()`), reports context fit against the configured context budgets,
and confirms. It then offers `Save this setup to .mak/config.yaml? [y/N]`
(default no), writing `<work dir>/.mak/config.yaml` with the planner's route
(`backend`/`base_url`, or `endpoint:`) and round-tripping it through
`load_config` + `validate_config`. Sub-commands: `status`, `models`,
`use <model> […]`, `planner <model>`, `pull <model>` (interruptible, resumable),
`url <base_url>`, `off`. Connected hosts persist in
`~/.config/mak/local_hosts.json` (`cli/core/local_hosts.py`). An unreachable server
is one red line, never a traceback. Discovery, the Ollama client and the
saved-host probe are a `LocalSeams` value (`cli/core/local_seams.py`) held on
`CliState.local_seams`; a test builds a state with fake seams, and no module
state changes.

**`/mode`** refuses to switch into a mode that is not usable yet ("run `/local`",
"run `/apikey`").

**`/endpoint`** (`cli/endpoints/`): `list`, `add`, `show`, `edit`, `test`,
`models`, `remove`, `export`, `help`. `add` is a gather-then-commit wizard — a
`CANCELLED` sentinel at any step discards the whole draft. Presets prefill from
`profiles.py`. `export <id>` prints secret-free YAML. `test <id>` runs the health
policy on demand (the only place `/endpoint` spends a request). Endpoints persist
in `~/.config/mak/endpoints.json`. Credentials are asked for as **variable
names**; `/apikey` writes values.

**Project config bootstrap** (`cli/project_config.py`). On startup, and after a
successful `/work-dir` (which returns the `"work_dir"` action to the loop), the
app calls `offer_project_config`: if the work dir has no `.mak/` and no explicit
`/config` is set, it asks `Create .mak/config.yaml here from <seed>? [y/N]`.
Yes copies `seed_config_path()` (the user's `~/.config/mak/config.yaml`, else
the packaged default) and never overwrites an existing file; no writes nothing, and
the run creates `.mak/` for its state on demand. The question is an injectable
`confirm` callable.

**Session-only configuration.** Slash-command changes live in `CliState` and are
never written to a config file, with three explicit exceptions, each behind a
yes: the project-config offer, `/local`'s save prompt, and `/endpoint`
(endpoints are reusable infrastructure). `cli/runner.py` is the bridge:
`request_from_state(state)` describes the run as a `RunRequest` — always an
explicit, resolved work dir; the session's `PlannerRoute` only when pinned (else
the config's planner); `selected_models` as the roster when non-empty (else the
config's agents); `max_agents` — and `config_for(state)` / `build_session(task,
state)` run it through `build_config` and `application.build_session`. New CLI
state that affects a run belongs in `request_from_state`. `models_display()`
shows the selection, else `config_roster`. `plan_in_thread` calls
`session.propose_plan` and returns the whole `PlanProposal` (or the error);
`run_session_in_thread` runs `session.run()` on a thread and joins it.

**Services on the state.** `CliState` is the one object the app passes to every
handler, the completer, setup and the UI, so it also carries the app's services:
`local_seams` and a lazily built `ModelRegistry` (`state.models()`, or an
injected `model_registry`). `state.config_file()` is the session's config —
explicit or discovered from the work dir — and every app-side config read
(endpoints, context budgets, auto-refresh) goes through it.

**API keys** (`cli/core/api_keys.py`): loaded from `~/.config/mak/.env`, then the
deprecated `mak/.env`, with exported variables winning. A session never exports
them: `cli/runner.session_env(state)` passes `read_env_files()` overlaid with
`os.environ` and the session's keys to the application API as `env`. `save_keys` accepts any variable name — it
parses the existing file, changes only the names it was asked to, and writes
atomically at `0600`. `key_names_for(endpoints)` lists the names configured
endpoints need.

**Adding a slash command:** add a handler in `commands.py` (print a one-line
`print_ok` / `print_warn` / `print_error` if it mutates state), register it in
`handle_command()`, add it to `COMMANDS` in `completer.py` (drives both the menu
and `/help`), and add argument completions to `MakCompleter`. Return `"exit"` or
`"clear"` (or `"work_dir"`) for commands the main loop acts on. The loop runs
each command through `MakCli._dispatch_command`: an unexpected exception prints
one `✗ /cmd failed: …` line and returns to the prompt, with the traceback logged
at `DEBUG` on `cli.app`; `KeyboardInterrupt` and `EOFError` still end the
session.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [13. Command line (`mak`)](13-command-line.md) · [15. Model catalog](15-model-catalog.md) →
