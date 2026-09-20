"""``/endpoint`` subcommand dispatch and the non-wizard subcommands.

House style throughout: an error is one actionable red line, never a traceback.
A user who mistypes an endpoint id should be told which ids exist, not shown a
``KeyError``.
"""

from __future__ import annotations

from rich.console import Console

from cli.core.models import registry
from cli.core.state import CliState
from cli.endpoints.prompts import confirm
from cli.endpoints.render import (
    MODEL_LIST_CAP,
    export_yaml,
    print_list,
    print_show,
)
from cli.endpoints.wizard import apply_edit, commit, existing_ids, run_add
from cli.ui import print_error, print_ok, print_warn
from mak.config import discover_config_path, load_config
from mak.core.exceptions import ConfigError
from mak.endpoints.profiles import profile_ids
from mak.endpoints.resolution import resolve_endpoint
from mak.endpoints.store import (
    load_user_endpoints,
    merge_endpoints,
    save_user_endpoints,
)
from mak.endpoints.types import EndpointConfig, HealthPolicy

SUBCOMMANDS: list[tuple[str, str]] = [
    ("list", "show every configured endpoint"),
    ("add", "set up a new endpoint (preset or custom)"),
    ("show", "all non-secret settings of one endpoint"),
    ("edit", "change an endpoint's URL, location or credential"),
    ("test", "probe one endpoint using its health policy"),
    ("models", "browse or refresh one endpoint's model list"),
    ("remove", "forget an endpoint"),
    ("export", "print a pasteable, secret-free YAML entry"),
    ("help", "list the /endpoint sub-commands"),
]


def cmd_endpoint(args: list[str], state: CliState, console: Console) -> None:
    """Dispatch ``/endpoint [subcommand] [args]``."""
    sub = args[0].lower() if args else "list"
    rest = args[1:]
    try:
        if sub in ("list", "ls"):
            _sub_list(console)
        elif sub == "add":
            _sub_add(console, state, rest[0] if rest else "")
        elif sub == "show":
            _sub_show(console, rest)
        elif sub == "edit":
            _sub_edit(console, rest)
        elif sub == "test":
            _sub_test(console, rest)
        elif sub == "models":
            _sub_models(console, rest)
        elif sub == "remove":
            _sub_remove(console, rest, state)
        elif sub == "export":
            _sub_export(console, rest)
        elif sub == "help":
            print_endpoint_help(console)
        else:
            print_error(
                console,
                f"Unknown /endpoint command: {sub}  "
                "[dim]— /endpoint help lists them[/dim]",
            )
    except ConfigError as exc:
        # Every configuration problem reaches the user as one line. The stack
        # behind it says nothing they can act on.
        print_error(console, str(exc))


def print_endpoint_help(console: Console) -> None:
    """Print the ``/endpoint`` sub-command table."""
    console.print()
    width = max(len(name) for name, _ in SUBCOMMANDS)
    for name, desc in SUBCOMMANDS:
        console.print(
            f"  [bold]/endpoint {name.ljust(width)}[/bold]  [dim]{desc}[/dim]"
        )
    console.print(
        f"\n  [dim]Presets: {', '.join(profile_ids())}[/dim]\n"
    )


def all_endpoints() -> tuple[EndpointConfig, ...]:
    """Return every configured endpoint: project config plus the user store."""
    saved, _diagnostic = load_user_endpoints()
    try:
        project = load_config(discover_config_path()).endpoints
    except ConfigError:
        # A broken project config is the project config's problem to report;
        # /endpoint must still be able to list what the user has saved.
        project = ()
    return merge_endpoints(project, saved)


def _require(console: Console, args: list[str], what: str) -> EndpointConfig | None:
    """Resolve ``args[0]`` to an endpoint, or report what exists and return None."""
    if not args:
        print_error(console, f"Which endpoint? Usage: /endpoint {what} <id>")
        return None
    endpoints = all_endpoints()
    wanted = args[0].strip().lower()
    for endpoint in endpoints:
        if endpoint.id == wanted:
            return endpoint
    known = ", ".join(e.id for e in endpoints) or "none configured"
    print_error(console, f"No endpoint '{wanted}'. Configured: {known}")
    return None


def _sub_list(console: Console) -> None:
    """Show every endpoint, with its model count and any store diagnostic."""
    saved, diagnostic = load_user_endpoints()
    if diagnostic is not None:
        print_warn(console, diagnostic.message())
    endpoints = all_endpoints()
    counts = {e.id: len(registry().for_endpoint(e.id)) for e in endpoints}
    print_list(console, endpoints, model_counts=counts)


def _sub_add(console: Console, state: CliState, profile: str) -> None:
    """Run the add wizard, commit its result, and apply the user's selection."""
    result = run_add(console, profile, existing=existing_ids())
    if result is None:
        return
    endpoint, draft = result
    if commit(console, endpoint, draft) is None:
        return
    if endpoint.id not in state.endpoint_ids:
        state.endpoint_ids.append(endpoint.id)
    if not draft.model:
        return
    spec = f"{endpoint.id}:{draft.model}"
    if draft.use_for_agents and spec not in state.selected_models:
        state.selected_models.append(spec)
        print_ok(console, f"Agents will use {spec}.")
    if draft.use_for_planner:
        state.planner_model = draft.model
        state.planner_endpoint_id = endpoint.id
        # The endpoint is authoritative now, so the legacy inference fields
        # must not linger and contradict it.
        state.planner_backend = ""
        state.planner_base_url = ""
        print_ok(console, f"Planner will use {spec}.")


def _sub_show(console: Console, args: list[str]) -> None:
    endpoint = _require(console, args, "show")
    if endpoint is not None:
        print_show(console, endpoint)


def _sub_edit(console: Console, args: list[str]) -> None:
    endpoint = _require(console, args, "edit")
    if endpoint is None:
        return
    saved, _ = load_user_endpoints()
    if not any(e.id == endpoint.id for e in saved):
        print_error(
            console,
            f"'{endpoint.id}' comes from your project config, not your saved "
            "endpoints — edit it in that file so the change stays with the "
            "project.",
        )
        return
    apply_edit(console, endpoint)


def _sub_test(console: Console, args: list[str]) -> None:
    """Probe one endpoint under its configured health policy."""
    endpoint = _require(console, args, "test")
    if endpoint is None:
        return
    model = args[1] if len(args) > 1 else ""
    resolved = resolve_endpoint(endpoint)

    if resolved.health_check is HealthPolicy.CHAT:
        if not model:
            print_error(
                console,
                "A chat probe needs a model: /endpoint test "
                f"{endpoint.id} <model>",
            )
            return
        answer = confirm(
            console,
            f"A chat probe sends a real request to {resolved.display_name} "
            "and may be billed to your account. Continue?",
        )
        if not answer:
            print_warn(console, "Cancelled — nothing was sent.")
            return
        resolved = resolve_endpoint(endpoint, chat_probe_allowed=True)

    from mak.agent_runner.adapters.openai_api_adapter import OpenAiCompatibleAdapter

    adapter = OpenAiCompatibleAdapter(
        model=model or "probe",
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        headers=resolved.headers,
        endpoint_id=resolved.id,
        endpoint_name=resolved.display_name,
        health_check_policy=resolved.health_check.value,
        chat_probe_ok=resolved.chat_probe_allowed,
        api_key_env=resolved.api_key_env,
    )
    if adapter.health_check():
        print_ok(console, f"{resolved.display_name}: {adapter.health_status()}")
    else:
        print_error(console, f"{resolved.display_name}: {adapter.health_detail()}")


def _sub_models(console: Console, args: list[str]) -> None:
    """Browse one endpoint's cached models, capped unless filtered."""
    endpoint = _require(console, args, "models")
    if endpoint is None:
        return
    needle = args[1].lower() if len(args) > 1 else ""
    entries = registry().for_endpoint(endpoint.id)
    if not entries:
        print_warn(
            console,
            f"No models cached for '{endpoint.id}' — run /refresh-models, or "
            "enter a model id by hand when you select one.",
        )
        return
    matches = [e for e in entries if not needle or needle in e.model_id.lower()]
    if not matches:
        print_warn(console, f"No model on '{endpoint.id}' matches '{needle}'.")
        return
    console.print()
    for entry in matches[:MODEL_LIST_CAP]:
        note = entry.planner_note()
        suffix = f"  [dim]({note})[/dim]" if note else ""
        console.print(f"  {entry.model_id}{suffix}")
    if len(matches) > MODEL_LIST_CAP:
        # Dumping several hundred rows destroys the session's scrollback and
        # helps nobody; a filter is one word away.
        console.print(
            f"\n  [dim]…and {len(matches) - MODEL_LIST_CAP} more. "
            f"Narrow it: /endpoint models {endpoint.id} <filter>[/dim]"
        )
    console.print()


def _sub_remove(console: Console, args: list[str], state: CliState) -> None:
    """Forget an endpoint, refusing while anything still references it."""
    endpoint = _require(console, args, "remove")
    if endpoint is None:
        return
    references = _references_to(endpoint.id, state)
    if references:
        print_error(
            console,
            f"'{endpoint.id}' is still in use by {', '.join(references)}. "
            "Point those elsewhere first.",
        )
        return
    saved, _ = load_user_endpoints()
    if not any(e.id == endpoint.id for e in saved):
        print_error(
            console,
            f"'{endpoint.id}' comes from your project config — remove it from "
            "that file.",
        )
        return
    answer = confirm(console, f"Forget endpoint '{endpoint.id}'?")
    if not answer:
        print_warn(console, "Cancelled — nothing was changed.")
        return
    save_user_endpoints(tuple(e for e in saved if e.id != endpoint.id))
    print_ok(console, f"Endpoint '{endpoint.id}' removed.")

    if endpoint.api_key_env:
        # Deliberately a separate question: the same variable may serve another
        # endpoint, and deleting a credential is not implied by forgetting a URL.
        drop = confirm(
            console, f"Also delete the stored {endpoint.api_key_env}?"
        )
        if drop:
            from cli.core.api_keys import save_keys

            save_keys({endpoint.api_key_env: ""})
            print_ok(console, f"{endpoint.api_key_env} deleted.")


def _references_to(endpoint_id: str, state: CliState) -> list[str]:
    """Return human descriptions of everything still pointing at an endpoint."""
    found: list[str] = []
    prefix = f"{endpoint_id}:"
    if any(spec.startswith(prefix) for spec in state.selected_models):
        found.append("the selected agent models")
    if state.planner_endpoint_id == endpoint_id:
        found.append("the planner")
    try:
        config = load_config(discover_config_path())
    except ConfigError:
        return found
    if any(a.endpoint == endpoint_id for a in config.agents):
        found.append("an agent in your config file")
    if config.planner.endpoint == endpoint_id:
        found.append("the planner in your config file")
    return found


def _sub_export(console: Console, args: list[str]) -> None:
    endpoint = _require(console, args, "export")
    if endpoint is None:
        return
    console.print()
    console.print(export_yaml(endpoint))
    console.print(
        "\n  [dim]Secret-free: it names the credential variable, never the "
        "key.[/dim]\n"
    )
