"""The ``/local`` command: detect a runtime, install a model, point MAK at it.

Nothing about running a local model should require hand-editing YAML, which is
what this module is for. Bare ``/local`` is a read-only overview — this
machine's runtimes, then every remembered remote host, each with its status and
models; it asks nothing. The first-run setup's wizard (detect → choose runtime →
choose model, pulling one if none is installed → choose planner → check the
context window → confirm) lives here too, and the sub-commands are its
individual steps.

Two rules govern the whole module:

- **MAK never manages the daemon.** It detects, reports, and instructs. Starting
  ``ollama serve`` on someone's machine is not a code editor's business, so a
  machine with nothing running gets install guidance, not a background process.
- **Nothing is written to disk without an explicit yes.** The wizard ends by
  *offering* to save to ``./mak.yaml``, defaulting to **no**. MAK's standing rule
  is that it never writes a config file except when the user explicitly changes a
  model (CONTRIBUTING §11); an interactive "Save this setup? [y/N]" is that rule,
  not an exception to it. Declining leaves a session-only setting, which is all
  ``CliState`` ever was.
  The one thing remembered without asking is *which hosts* were connected
  (``~/.config/mak/local_hosts.json``, a cache like ``models.json``), so a
  ``/local url`` survives a restart; ``/local forget`` removes one.

Every sub-command must survive an unreachable server: an ``OllamaError`` becomes
one red line naming the endpoint, never a traceback and never a crash of the
prompt loop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console

from cli.core.local_hosts import load_hosts, save_hosts
from cli.core.state import (
    MODE_CLOUD,
    MODE_HYBRID,
    MODE_LOCAL,
    CliState,
    LocalHost,
)
from cli.ui import ACCENT, print_error, print_ok, print_warn
from mak.config import normalize_base_url
from mak.core.exceptions import ConfigError
from mak.local import (
    RECOMMENDED,
    LocalRuntime,
    OllamaClient,
    OllamaError,
    OllamaModel,
    discover,
    recommended_for,
)
from mak.local.runtime import (
    KIND_OLLAMA,
    probe_ollama,
    probe_openai_compatible,
)

# Injectable seams. Module-level so tests replace them once, and so the wizard
# and every sub-command go through the same two functions.
DiscoverFn = Callable[[], list[LocalRuntime]]
ClientFactory = Callable[[str], OllamaClient]
HostProbe = Callable[[LocalHost], "LocalRuntime | None"]

# A saved remote host is asked whether it is up on a UI path, so the answer
# must come back in seconds rather than after the client's generation timeout.
_HOST_PROBE_TIMEOUT_S = 2.0

_INSTALL_GUIDANCE = (
    "  No local runtime is listening.\n\n"
    "  Install Ollama, then start it:\n"
    "    macOS:  brew install ollama\n"
    "    Linux:  curl -fsSL https://ollama.com/install.sh | sh\n"
    "    then:   ollama serve\n\n"
    "  Already running one somewhere else? Point MAK at it:\n"
    "    /local url http://<host>:<port>\n\n"
    "  MAK does not start or stop the daemon itself."
)

# The Ollama adapter's own estimate, imported rather than re-guessed so this
# preview and the runtime check agree about what "fits".
_CHARS_PER_TOKEN = 4

_SUBCOMMANDS = (
    ("status", "endpoint, version, models installed, models loaded"),
    ("models", "list what the runtime offers, live"),
    ("use <model> …", "set the agent model(s)"),
    ("planner <model>", "set the planner to a local model"),
    ("pull <model>", "download a model with a progress bar"),
    ("url <base_url>", "connect to a (remote) endpoint and remember it"),
    ("forget <base_url>", "remove a remembered host"),
    ("off", "drop back to cloud mode (keeps your API keys)"),
)


def default_discover() -> list[LocalRuntime]:
    """Scan the well-known local endpoints. Never raises."""
    return discover()


def default_client(base_url: str) -> OllamaClient:
    """Build a client for ``base_url``."""
    return OllamaClient(base_url)


def default_probe_host(host: LocalHost) -> LocalRuntime | None:
    """Ask a saved host whether it is up and what it has. Never raises."""
    if host.kind == KIND_OLLAMA:
        return probe_ollama(host.url, timeout=_HOST_PROBE_TIMEOUT_S)
    return probe_openai_compatible(
        host.url, timeout=_HOST_PROBE_TIMEOUT_S, name="OpenAI-compatible server"
    )


_discover_fn: DiscoverFn = default_discover
_client_factory: ClientFactory = default_client
_probe_host_fn: HostProbe = default_probe_host


def set_seams(
    *,
    discover_fn: DiscoverFn | None = None,
    client_factory: ClientFactory | None = None,
    probe_host_fn: HostProbe | None = None,
) -> None:
    """Replace the discovery / client / host-probe seams (tests only)."""
    global _discover_fn, _client_factory, _probe_host_fn
    if discover_fn is not None:
        _discover_fn = discover_fn
    if client_factory is not None:
        _client_factory = client_factory
    if probe_host_fn is not None:
        _probe_host_fn = probe_host_fn


def reset_seams() -> None:
    """Restore the real discovery, client factory, and host probe."""
    global _discover_fn, _client_factory, _probe_host_fn
    _discover_fn = default_discover
    _client_factory = default_client
    _probe_host_fn = default_probe_host


# ── remembered hosts ──────────────────────────────────────────────────────────


def restore_saved_hosts(state: CliState) -> None:
    """Load remembered hosts onto ``state``, reconnecting the active one.

    Offline by design: the cached model lists are used as-is, so a host that
    is down cannot slow startup. ``/refresh-models`` or ``/local`` re-asks.
    """
    active_url, hosts = load_hosts()
    state.local_hosts = hosts
    for host in hosts:
        if host.url == active_url:
            state.local_kind = host.kind
            state.local_base_url = host.url
            state.local_models = list(host.models)


def remember_hosts(state: CliState) -> None:
    """Record the active host among the known ones and save them all."""
    active = state.active_local_host()
    if active is not None:
        state.local_hosts = [
            active,
            *(h for h in state.local_hosts if h.url != active.url),
        ]
    save_hosts(state.local_base_url, state.local_hosts)


def activate_host(state: CliState, url: str, kind: str = "") -> None:
    """Make ``url`` the active runtime, reusing what is known about it."""
    if url == state.local_base_url:
        if kind:
            state.local_kind = kind
        return
    remember_hosts(state)  # keep the outgoing host's latest model list
    known = next((h for h in state.local_hosts if h.url == url), None)
    state.local_base_url = url
    state.local_kind = kind or (known.kind if known else KIND_OLLAMA)
    state.local_models = list(known.models) if known else []
    remember_hosts(state)


# ── helpers ───────────────────────────────────────────────────────────────────


def spec_for(state: CliState, model: str) -> str:
    """Return the ``--models`` spec for ``model`` on the configured runtime.

    ``ollama:<tag>@<url>`` for the native runtime, ``local:<id>@<url>``
    otherwise — the same grammar ``mak run --models`` takes, so the TUI and the
    command line configure a run identically.
    """
    return f"{state.local_provider()}:{model}@{state.local_base_url}"


def _client(state: CliState) -> OllamaClient:
    return _client_factory(state.local_base_url)


def _require_runtime(state: CliState, console: Console) -> bool:
    """Report and return False when no runtime has been configured yet."""
    if state.has_local_runtime():
        return True
    print_error(
        console,
        "No local runtime configured — run [bold]/local[/bold] to detect one, "
        "or [bold]/local url <endpoint>[/bold] to name it.",
    )
    return False


def _installed(state: CliState, console: Console) -> list[OllamaModel] | None:
    """List the runtime's models, or report the failure as one line."""
    try:
        return _client(state).list_models()
    except OllamaError as exc:
        print_error(console, str(exc))
        return None


def _describe_model(model: OllamaModel) -> str:
    """Return a model's one-line form: tag, parameter size, quantization."""
    bits = [bit for bit in (model.parameter_size, model.quantization) if bit]
    return f"{model.name}" + (f"  [dim]{' · '.join(bits)}[/dim]" if bits else "")


def refresh_local_models(state: CliState) -> tuple[list[str], list[str]]:
    """Re-list the configured runtime's models onto ``state``.

    Returns ``(added, removed)`` relative to the previously known list. Raises
    ``OllamaError`` when the runtime cannot be reached, leaving the cached
    list untouched.
    """
    previous = list(state.local_models)
    # Same bound as a saved-host probe: an unreachable host (a VPN peer that is
    # offline) otherwise blocks /refresh-models for the full generation timeout.
    listed = _client(state).list_models(timeout=_HOST_PROBE_TIMEOUT_S)
    current = [model.name for model in listed]
    state.local_models = current
    remember_hosts(state)
    added = [name for name in current if name not in previous]
    removed = [name for name in previous if name not in current]
    return added, removed


def refresh_saved_host(
    state: CliState, host: LocalHost
) -> tuple[list[str], list[str]] | None:
    """Re-list a remembered, inactive host; None when it did not answer."""
    runtime = _probe_host_fn(host)
    if runtime is None:
        return None
    current = list(runtime.models)
    added = [name for name in current if name not in host.models]
    removed = [name for name in host.models if name not in current]
    for known in state.local_hosts:
        if known.url == host.url:
            known.models = current
    remember_hosts(state)
    return added, removed


def adopt_runtime(state: CliState, runtime: LocalRuntime) -> None:
    """Record a detected runtime on the state, without choosing a model."""
    activate_host(state, runtime.base_url, runtime.kind)
    state.local_models = list(runtime.models)
    remember_hosts(state)


def apply_agent_models(state: CliState, models: Sequence[str]) -> None:
    """Point the agent roster at ``models`` on the configured runtime."""
    state.selected_models = [spec_for(state, model) for model in models]
    if state.mode == MODE_CLOUD:
        state.mode = MODE_LOCAL


def apply_local_planner(state: CliState, model: str) -> None:
    """Point the planner at a local model (so the whole run is local)."""
    state.planner_model = model
    state.planner_backend = (
        "ollama" if state.local_kind == KIND_OLLAMA else "openai"
    )
    state.planner_base_url = state.local_base_url
    state.planner_endpoint_id = ""
    if state.mode == MODE_HYBRID:
        state.mode = MODE_LOCAL


def apply_cloud_planner(state: CliState, provider: str, model: str) -> None:
    """Keep a hosted planner beside local agents — mode ``hybrid``."""
    state.set_cloud_planner(provider, model)
    state.mode = MODE_HYBRID


def go_cloud(state: CliState) -> None:
    """Drop back to cloud mode, forgetting the local roster but keeping keys."""
    state.mode = MODE_CLOUD
    # The host stays remembered (``/local forget`` removes it); it just stops
    # being the one a new session connects to.
    active = state.active_local_host()
    if active is not None:
        state.local_hosts = [
            active,
            *(h for h in state.local_hosts if h.url != active.url),
        ]
    state.local_kind = ""
    state.local_base_url = ""
    state.local_models = []
    state.planner_backend = ""
    state.planner_base_url = ""
    state.selected_models = [
        spec for spec in state.selected_models
        if not spec.startswith(("local:", "ollama:"))
    ]
    save_hosts("", state.local_hosts)


# ── the dispatcher ────────────────────────────────────────────────────────────


def cmd_local(args: list[str], state: CliState, console: Console) -> None:
    """Handle ``/local`` and its sub-commands."""
    if not args:
        show_overview(state, console)
        return
    sub, rest = args[0].lower(), args[1:]
    if sub == "status":
        _sub_status(state, console)
    elif sub == "models":
        _sub_models(state, console)
    elif sub == "use":
        _sub_use(rest, state, console)
    elif sub == "planner":
        _sub_planner(rest, state, console)
    elif sub == "pull":
        _sub_pull(rest, state, console)
    elif sub == "url":
        _sub_url(rest, state, console)
    elif sub == "forget":
        _sub_forget(rest, state, console)
    elif sub == "off":
        _sub_off(state, console)
    elif sub == "help":
        print_local_help(console)
    else:
        print_error(console, f"Unknown /local sub-command: {sub}")
        print_local_help(console)


def print_local_help(console: Console) -> None:
    """List the ``/local`` sub-commands."""
    console.print()
    console.print("  [dim]/local[/dim]  shows every runtime and host. Sub-commands:")
    width = max(len(name) for name, _ in _SUBCOMMANDS)
    for name, desc in _SUBCOMMANDS:
        console.print(
            f"    [bold {ACCENT}]/local {name.ljust(width)}[/bold {ACCENT}]  "
            f"[dim]{desc}[/dim]"
        )
    console.print()


# ── sub-commands ──────────────────────────────────────────────────────────────


def _sub_status(state: CliState, console: Console) -> None:
    if not _require_runtime(state, console):
        return
    client = _client(state)
    console.print()
    console.print(f"  [dim]endpoint[/dim]  {state.local_base_url}")
    try:
        console.print(f"  [dim] version[/dim]  {client.version() or 'unknown'}")
        installed = client.list_models()
        loaded = client.running()
    except OllamaError as exc:
        # The whole point of /local status is to say what is wrong when the
        # server has gone away mid-session.
        print_error(console, str(exc))
        return
    console.print(f"  [dim]  models[/dim]  {len(installed)} installed")
    for model in installed:
        mark = "[green]●[/green]" if model.name in loaded else "[dim]○[/dim]"
        console.print(f"            {mark} {_describe_model(model)}")
    console.print(
        f"  [dim]  loaded[/dim]  {', '.join(loaded) if loaded else 'none right now'}"
    )
    console.print()


def _sub_models(state: CliState, console: Console) -> None:
    if not _require_runtime(state, console):
        return
    installed = _installed(state, console)
    if installed is None:
        return
    state.local_models = [model.name for model in installed]
    remember_hosts(state)
    if not installed:
        console.print("\n  [dim]No models installed. /local pull <model>[/dim]")
        _print_recommendations(console)
        return
    console.print(
        f"\n  [dim]Usage: /local use <model> — at {state.local_base_url}[/dim]"
    )
    chosen = set(state.selected_models)
    for model in installed:
        selected = spec_for(state, model.name) in chosen
        active = "[green]●[/green]" if selected else "[dim]○[/dim]"
        console.print(f"    {active} {_describe_model(model)}")
    console.print()


def _sub_use(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /local use <model> [<model> ...][/dim]")
        return
    if not _require_runtime(state, console):
        return
    installed = _installed(state, console)
    if installed is None:
        return
    names = {model.name for model in installed}
    missing = [model for model in args if model not in names]
    if missing:
        print_error(
            console,
            f"not installed: {', '.join(missing)} — "
            f"run [bold]/local pull {missing[0]}[/bold]",
        )
        return
    if state.max_agents < len(args):
        print_error(
            console,
            f"max-agents ({state.max_agents}) < number of models ({len(args)}) — "
            f"run [bold]/max-agents {len(args)}[/bold] first.",
        )
        return
    apply_agent_models(state, args)
    print_ok(console, f"Agents: {', '.join(state.selected_models)}")
    if len(args) > 1:
        print_warn(
            console,
            "One local server serializes these requests anyway — more models "
            "here buys variety, not parallelism.",
        )


def _sub_planner(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /local planner <model>[/dim]")
        return
    if not _require_runtime(state, console):
        return
    installed = _installed(state, console)
    if installed is None:
        return
    if args[0] not in {model.name for model in installed}:
        print_error(
            console,
            f"not installed: {args[0]} — run [bold]/local pull {args[0]}[/bold]",
        )
        return
    apply_local_planner(state, args[0])
    print_ok(console, f"Planner: {args[0]}  [dim]at {state.local_base_url}[/dim]")


def _sub_pull(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /local pull <model>[/dim]")
        _print_recommendations(console)
        return
    if not _require_runtime(state, console):
        return
    pull_model(state, args[0], console)


def _sub_url(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /local url http://host:port[/dim]")
        return
    try:
        url = normalize_base_url(args[0], where="/local url")
    except ConfigError as exc:
        print_error(console, str(exc))
        return
    # Validated, then *probed*: a well-formed URL nothing answers at is a
    # setting that fails at dispatch instead of here.
    try:
        version = _client_factory(url).version()
        models = [model.name for model in _client_factory(url).list_models()]
    except OllamaError as exc:
        print_error(console, str(exc))
        return
    activate_host(state, url, KIND_OLLAMA)
    state.local_models = models
    remember_hosts(state)
    if state.mode == MODE_CLOUD:
        state.mode = MODE_LOCAL
    print_ok(
        console,
        f"Runtime: {url}  [dim]v{version or '?'} · {len(models)} model(s) · "
        "remembered for next time[/dim]",
    )


def _sub_forget(args: list[str], state: CliState, console: Console) -> None:
    if not args:
        console.print("  [dim]Usage: /local forget http://host:port[/dim]")
        return
    url = args[0].rstrip("/")
    known = {h.url for h in state.all_local_hosts()}
    if url not in known:
        print_error(console, f"not a remembered host: {url}")
        return
    if url == state.local_base_url:
        go_cloud(state)
    state.local_hosts = [h for h in state.local_hosts if h.url != url]
    save_hosts(state.local_base_url, state.local_hosts)
    print_ok(console, f"Forgot {url}")


def _sub_off(state: CliState, console: Console) -> None:
    go_cloud(state)
    print_ok(
        console,
        "Back to cloud mode. [dim]Your API keys are unchanged; /local returns.[/dim]",
    )


# ── pulling ───────────────────────────────────────────────────────────────────


def _print_recommendations(console: Console) -> None:
    """Print the curated suggestions, smallest first."""
    console.print("\n  [dim]Suggested coding models (smallest first):[/dim]")
    for entry in RECOMMENDED:
        console.print(f"    {entry.describe()}")
    console.print()


def pull_model(state: CliState, model: str, console: Console) -> bool:
    """Download ``model``, rendering progress. Returns whether it completed.

    Interruptible: Ctrl-C stops the stream and leaves Ollama's partial blob
    alone, so re-running the pull resumes rather than restarting.
    """
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    console.print(f"\n  [dim]Pulling {model}…[/dim]")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(model, total=None)
            for record in _client(state).pull(model):
                if record.total:
                    progress.update(
                        task,
                        total=record.total,
                        completed=record.completed or 0,
                        description=record.status or model,
                    )
                else:
                    progress.update(task, description=record.status or model)
    except OllamaError as exc:
        print_error(console, str(exc))
        return False
    except KeyboardInterrupt:
        print_warn(
            console,
            f"Pull interrupted. [dim]Re-run /local pull {model} to resume.[/dim]",
        )
        return False
    print_ok(console, f"Pulled {model}")
    if model not in state.local_models:
        state.local_models = [*state.local_models, model]
        remember_hosts(state)
    return True


# ── the wizard ────────────────────────────────────────────────────────────────


def _ask(console: Console, prompt: str, default: str = "") -> str:
    """Ask one question, returning ``default`` on Ctrl-C / EOF.

    The single input seam of the wizard, so a test drives the whole flow by
    replacing this one function.
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    style = Style.from_dict({"": "#c9d1d9", "prompt": f"{ACCENT} bold"})
    try:
        answer = pt_prompt(f"  {prompt}", default=default, style=style)
    except (KeyboardInterrupt, EOFError):
        console.print()
        return default
    return answer.strip()


def _choose(
    console: Console, options: Sequence[str], prompt: str, default_index: int = 0
) -> int:
    """Print a numbered menu and return the chosen index (default on anything else)."""
    for index, option in enumerate(options, 1):
        console.print(f"    [dim]{index:>2})[/dim]  {option}")
    console.print()
    raw = _ask(console, f"{prompt} (1–{len(options)}) [{default_index + 1}]: ")
    try:
        index = int(raw) - 1
    except ValueError:
        return default_index
    return index if 0 <= index < len(options) else default_index


def _yes(console: Console, question: str) -> bool:
    """Ask a yes/no question defaulting to **no** (D13)."""
    return _ask(console, f"{question} [y/N]: ").lower() in ("y", "yes")


def run_wizard(state: CliState, console: Console) -> bool:
    """Detect → choose → check → confirm. Returns whether a runtime was set up."""
    console.print("\n  [dim]Looking for local model runtimes…[/dim]")
    local_runtimes, remotes = survey_hosts(state)
    print_host_overview(state, console, local_runtimes, remotes)
    runtimes = [
        *local_runtimes,
        *(runtime for _host, runtime in remotes if runtime is not None),
    ]
    if not runtimes:
        console.print(_INSTALL_GUIDANCE)
        console.print()
        return False

    runtime = runtimes[0]
    if len(runtimes) > 1:
        runtime = runtimes[
            _choose(console, [r.describe() for r in runtimes], "Use which runtime?")
        ]
    adopt_runtime(state, runtime)

    model = _choose_agent_model(state, console)
    if model is None:
        return False
    apply_agent_models(state, [model])

    _choose_planner(state, console, model)
    _report_context_fit(state, console, model)

    console.print()
    console.print(f"  [dim]agents [/dim] {', '.join(state.selected_models)}")
    console.print(f"  [dim]planner[/dim] {state.planner_spec()}")
    console.print(f"  [dim]mode   [/dim] {state.mode}")
    console.print()
    if _yes(console, "Save this setup to ./mak.yaml?"):
        _save_config(state, console)
    else:
        print_ok(console, "Kept for this session only.")
    return True


def show_overview(state: CliState, console: Console) -> None:
    """Print this machine's runtimes and every remote host. Asks nothing."""
    console.print("\n  [dim]Looking for local model runtimes…[/dim]")
    local_runtimes, remotes = survey_hosts(state)
    print_host_overview(state, console, local_runtimes, remotes)
    if not local_runtimes and not remotes:
        console.print(_INSTALL_GUIDANCE)
        console.print()
        return
    console.print(
        "  [dim]/models or /planner to pick one · /local url http://host:port "
        "to add a host · /local help for more[/dim]"
    )
    console.print()


def survey_hosts(
    state: CliState,
) -> tuple[list[LocalRuntime], list[tuple[LocalHost, LocalRuntime | None]]]:
    """Scan this machine and probe every remembered remote host, concurrently.

    Returns ``(this_machine, remotes)``; a remote whose probe failed is paired
    with None so it can still be listed as unreachable.
    """
    remote_hosts = [h for h in state.all_local_hosts() if not h.is_this_machine()]
    with ThreadPoolExecutor(max_workers=len(remote_hosts) + 1) as pool:
        local_future = pool.submit(_discover_fn)
        probes = [pool.submit(_safe_probe, host) for host in remote_hosts]
        local_runtimes = local_future.result()
        remotes = [
            (host, future.result())
            for host, future in zip(remote_hosts, probes, strict=True)
        ]
    remote_urls = {host.url for host in remote_hosts}
    local_runtimes = [r for r in local_runtimes if r.base_url not in remote_urls]
    for host, runtime in remotes:
        if runtime is not None:
            # Keep the remembered list current while we have the answer.
            for known in state.local_hosts:
                if known.url == host.url:
                    known.models = list(runtime.models)
            if host.url == state.local_base_url:
                state.local_models = list(runtime.models)
    if remotes:
        remember_hosts(state)
    return local_runtimes, remotes


def _safe_probe(host: LocalHost) -> LocalRuntime | None:
    try:
        return _probe_host_fn(host)
    except Exception:  # noqa: BLE001 - a probe on a UI path must not raise
        return None


def print_host_overview(
    state: CliState,
    console: Console,
    local_runtimes: Sequence[LocalRuntime],
    remotes: Sequence[tuple[LocalHost, LocalRuntime | None]],
) -> None:
    """Print this machine's runtimes, then each remote host's status and models."""
    console.print("\n  [bold]This machine[/bold]")
    if not local_runtimes:
        console.print("    [dim]○ no runtime running[/dim]")
    for found in local_runtimes:
        _print_runtime(state, console, found)

    console.print("\n  [bold]Remote hosts[/bold]")
    if not remotes:
        console.print(
            "    [dim]none — /local url http://host:port connects one[/dim]"
        )
    for host, runtime in remotes:
        if runtime is None:
            active = "  [dim](active)[/dim]" if host.url == state.local_base_url else ""
            console.print(
                f"    [red]○[/red] {host.url}  [dim]unreachable · "
                f"{len(host.models)} model(s) cached[/dim]{active}"
            )
            continue
        _print_runtime(state, console, runtime)
    console.print()


def _print_runtime(state: CliState, console: Console, runtime: LocalRuntime) -> None:
    active = "  [dim](active)[/dim]" if runtime.base_url == state.local_base_url else ""
    console.print(f"    [green]●[/green] {runtime.describe()}{active}")
    for model in runtime.models:
        console.print(f"        [dim]·[/dim] {model}")


def _choose_agent_model(state: CliState, console: Console) -> str | None:
    """Pick (or pull) the model the agents will run on."""
    installed = _installed(state, console)
    if installed is None:
        return None
    if installed:
        console.print("  [bold]Which model should the agents use?[/bold]")
        names = [model.name for model in installed]
        index = _choose(console, [_describe_model(m) for m in installed], "Model")
        return names[index]

    console.print("  [bold]No models installed.[/bold] Suggested, smallest first:")
    options = [entry.describe() for entry in RECOMMENDED]
    entry = RECOMMENDED[_choose(console, options, "Pull which model?")]
    if not pull_model(state, entry.tag, console):
        return None
    return entry.tag


def _choose_planner(state: CliState, console: Console, agent_model: str) -> None:
    """Pick the planner: the same local model, another local one, or a cloud one."""
    entry = recommended_for(agent_model)
    console.print("\n  [bold]Which model should plan?[/bold]")
    if entry is not None and entry.is_small():
        # A judgment, so it reads off the curated table rather than off a size
        # heuristic computed here.
        console.print(
            f"  [dim]{agent_model} is small; a cloud planner (hybrid) usually "
            "produces better plans.[/dim]"
        )
    options = [
        f"the same local model ({agent_model})",
        "another local model",
        f"a cloud planner ({state.planner_spec()}) — hybrid mode",
    ]
    default = 2 if (entry is not None and entry.is_small() and _any_key(state)) else 0
    choice = _choose(console, options, "Planner", default_index=default)
    if choice == 2 and _any_key(state):
        apply_cloud_planner(
            state, state.planner_cloud_provider(), state.planner_model
        )
        return
    if choice == 1 and state.local_models:
        index = _choose(console, state.local_models, "Local planner model")
        apply_local_planner(state, state.local_models[index])
        return
    apply_local_planner(state, agent_model)


def _any_key(state: CliState) -> bool:
    return any(value.strip() for value in state.api_keys.values())


def _report_context_fit(state: CliState, console: Console, model: str) -> None:
    """Show the model's context window beside MAK's bundle budgets.

    D11's footgun, made visible **before** the first run rather than after a bad
    one: a window too small for a bundle is the failure that otherwise produces
    a confident wrong answer with nothing in the log to explain it.
    """
    from mak.config import discover_config_path, load_config

    try:
        context_length = _client(state).show(model).context_length
    except OllamaError:
        return
    if context_length is None:
        return
    try:
        session = load_config(discover_config_path()).session
    except ConfigError:
        return
    budget_bytes = (
        max(session.dependency_context_bytes, 0)
        + max(session.cross_file_context_bytes, 0)
    )
    # The same ~4-characters-per-token figure the Ollama adapter sizes with, so
    # what the wizard promises and what the adapter enforces cannot disagree.
    needed = budget_bytes // _CHARS_PER_TOKEN
    console.print(
        f"\n  [dim]context[/dim]  {model} holds {context_length:,} tokens; MAK's "
        f"context budgets are about {needed:,}."
    )
    if needed >= context_length:
        print_warn(
            console,
            "A full bundle may not fit. MAK refuses rather than truncating — "
            "lower session.dependency_context_bytes / "
            "session.cross_file_context_bytes, or use a larger model.",
        )


_CONFIG_TEMPLATE = """\
# Written by MAK's /local wizard. Edit freely — MAK never rewrites this file
# except when you explicitly ask it to.
session:
  max_concurrent_agents: {max_agents}

planner:
  model: "{planner_model}"
{planner_extra}
agents:
  - type: "{agent_type}"
    model: "{agent_model}"
    base_url: "{base_url}"
    structured_output: "json_schema"
    # Local generation is measured in minutes, and the per-agent timeout is what
    # bounds the call.
    timeout: 1800
"""


def _save_config(state: CliState, console: Console) -> None:
    """Write the chosen setup to ``./mak.yaml``, then verify it loads."""
    from pathlib import Path

    from mak.bootstrap import validate_config
    from mak.config import load_config

    agent_type = "ollama_api" if state.local_kind == KIND_OLLAMA else "local_api"
    model = state.selected_models[0].split("@")[0].partition(":")[2]
    if state.planner_base_url:
        planner_extra = (
            f'  backend: "{state.planner_backend}"\n'
            f'  base_url: "{state.planner_base_url}"\n'
        )
    elif state.planner_cloud_provider():
        planner_extra = f'  backend: "{state.planner_cloud_provider()}"\n'
    else:
        planner_extra = ""
    body = _CONFIG_TEMPLATE.format(
        max_agents=state.max_agents,
        planner_model=state.planner_model,
        planner_extra=planner_extra,
        agent_type=agent_type,
        agent_model=model,
        base_url=state.local_base_url,
    )
    path = Path("mak.yaml").resolve()
    try:
        path.write_text(body, encoding="utf-8")
        validate_config(load_config(path))
    except (OSError, ConfigError) as exc:
        print_error(console, f"could not write {path}: {exc}")
        return
    state.config_path = str(path)
    print_ok(console, f"Saved {path}")
