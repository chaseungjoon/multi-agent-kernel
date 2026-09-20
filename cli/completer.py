"""prompt_toolkit completers for MAK slash commands and model specs.

Typing "/" pops the command menu immediately (complete_while_typing is on in
app.py); each entry carries a one-line description in the meta column.
"""
from __future__ import annotations

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document

from cli.core.models import PROVIDER_DISPLAY, PROVIDER_ORDER, all_models
from cli.core.state import MODES, CliState, mode_summary

COMMANDS: list[tuple[str, str]] = [
    ("/models",     "Select agent models"),
    ("/planner",    "Switch the planner model"),
    ("/refresh-models", "Re-fetch cloud and local model lists"),
    ("/endpoint",   "Add and manage OpenAI-compatible endpoints"),
    ("/local",      "Show local runtimes and remote hosts (Ollama, vLLM, LM Studio)"),
    ("/mode",       "Switch between cloud, local, and hybrid"),
    ("/max-agents", "Set how many agents run in parallel"),
    ("/work-dir",   "Set the working directory MAK edits"),
    ("/apikey",     "Add or update provider API keys"),
    ("/config",     "Load a config YAML (no arg: auto-discover)"),
    ("/no-review",  "Toggle plan approval before running"),
    ("/status",     "Show current session settings"),
    ("/help",       "Show commands and shortcuts"),
    ("/clear",      "Clear the screen"),
    ("/exit",       "Quit MAK"),
]

# ``/local``'s sub-commands, for argument completion. Mirrors cli.local's own
# table; kept here so the completer does not import the wizard module (which
# would make every keystroke pay for prompt_toolkit's styles and rich's
# progress).
_LOCAL_SUBCOMMANDS: list[tuple[str, str]] = [
    ("status",  "endpoint, version, models installed and loaded"),
    ("models",  "list what the runtime offers, live"),
    ("use",     "set the agent model(s)"),
    ("planner", "set the planner to a local model"),
    ("pull",    "download a model with a progress bar"),
    ("url",     "connect to a (remote) endpoint and remember it"),
    ("forget",  "remove a remembered host"),
    ("off",     "drop back to cloud mode"),
    ("help",    "list the /local sub-commands"),
]

# ``/endpoint``'s sub-commands. Mirrors ``cli.endpoints.commands.SUBCOMMANDS``,
# kept here for the same reason ``_LOCAL_SUBCOMMANDS`` is: the completer must not
# import the wizard module, or every keystroke pays for prompt_toolkit's styles
# and the endpoint store's file read.
_ENDPOINT_SUBCOMMANDS: list[tuple[str, str]] = [
    ("list",   "show every configured endpoint"),
    ("add",    "set up a new endpoint (preset or custom)"),
    ("show",   "all non-secret settings of one endpoint"),
    ("edit",   "change an endpoint's URL, location or credential"),
    ("test",   "probe one endpoint using its health policy"),
    ("models", "browse or refresh one endpoint's model list"),
    ("remove", "forget an endpoint"),
    ("export", "print a pasteable, secret-free YAML entry"),
    ("help",   "list the /endpoint sub-commands"),
]

# Sub-commands whose next argument is an endpoint id.
_ENDPOINT_ID_ARGS = frozenset({"show", "edit", "test", "models", "remove", "export"})

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}


def _profile_ids() -> tuple[str, ...]:
    """Return the built-in profile ids (imported lazily, per keystroke)."""
    from mak.endpoints.profiles import profile_ids

    return profile_ids()


class MakCompleter(Completer):
    """Tab / inline completions for slash commands."""

    def __init__(self, state: CliState) -> None:
        self._state    = state
        self._path_cpl = PathCompleter(only_directories=True, expanduser=True)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> list[Completion]:
        """Yield completions for the current input (slash commands and their args)."""
        text = document.text_before_cursor

        # Non-slash input never triggers completions.
        if not text.startswith("/"):
            return []

        # Split at most once so paths with spaces are handled as a unit.
        parts = text.split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""

        # ── Still typing the command name (no space yet) ──────────────────────
        if len(parts) == 1 and not text.endswith(" "):
            return [
                Completion(
                    name,
                    start_position=-len(cmd),
                    display=name,
                    display_meta=desc,
                )
                for name, desc in COMMANDS
                if name.startswith(cmd)
            ]

        # ── Per-command argument completions ──────────────────────────────────
        if cmd == "/models":
            return self._complete_models(arg)

        if cmd == "/planner":
            return self._complete_planner(arg)

        if cmd == "/max-agents":
            if not arg.strip():
                return [
                    Completion(
                        "",
                        start_position=0,
                        display="<number>",
                        display_meta="positive integer, e.g. 3",
                    )
                ]
            return []

        if cmd == "/work-dir":
            return self._complete_path(arg, complete_event)

        if cmd == "/config":
            if not arg.strip():
                return [
                    Completion(
                        "",
                        start_position=0,
                        display="<path>",
                        display_meta="config YAML file (empty: reset to default)",
                    ),
                ]
            return self._complete_path(arg, complete_event)

        if cmd == "/endpoint":
            return self._complete_endpoint(arg)

        if cmd == "/mode":
            partial = arg.strip()
            return [
                Completion(
                    mode,
                    start_position=-len(partial),
                    display=mode,
                    display_meta=mode_summary(mode),
                )
                for mode in MODES
                if mode.startswith(partial)
            ]

        if cmd == "/local":
            partial = arg.strip()
            return [
                Completion(
                    name,
                    start_position=-len(partial),
                    display=name,
                    display_meta=desc,
                )
                for name, desc in _LOCAL_SUBCOMMANDS
                if name.startswith(partial)
            ]

        if cmd == "/no-review":
            partial = arg.strip()
            opts = [
                ("false", "require approval before running (default)"),
                ("true",  "skip approval — run plans immediately"),
            ]
            return [
                Completion(
                    val,
                    start_position=-len(partial),
                    display=val,
                    display_meta=meta,
                )
                for val, meta in opts
                if val.startswith(partial)
            ]

        return []

    # ── Model completions ──────────────────────────────────────────────────────

    def _group_header(self, label: str, meta: str = "") -> Completion:
        """Return a non-inserting row that titles a group in the completion menu."""
        return Completion(
            "",
            start_position=0,
            display=f"── {label} ──",
            display_meta=meta,
        )

    def _complete_local(self, partial: str) -> list[Completion]:
        """Complete the models every known local host reported.

        Offered in every mode: a host connected with ``/local url`` (now or in
        an earlier session) is usable from ``/models`` and ``/planner`` whether
        or not the session is in local or hybrid mode. Models on the active host
        insert ``provider:model`` (the endpoint is attached when the command
        runs); models on another host insert the full ``provider:model@url``.
        """
        results: list[Completion] = []
        for host in self._state.all_local_hosts():
            is_current = host.url == self._state.local_base_url
            meta       = f"Local · {host.host_display()}"
            for name in host.models:
                spec = f"{host.provider()}:{name}"
                if not (spec.startswith(partial) or name.startswith(partial)):
                    continue
                text = spec if is_current else f"{spec}@{host.url}"
                results.append(
                    Completion(
                        text,
                        start_position=-len(partial),
                        display=spec,
                        display_meta=meta,
                    )
                )
        return results

    def _grouped(
        self, local: list[Completion], cloud: list[Completion]
    ) -> list[Completion]:
        """Order completions as Local then Cloud, titled when local ones exist."""
        if not local:
            return cloud
        results = [self._group_header("Local"), *local]
        if cloud:
            results += [self._group_header("Cloud"), *cloud]
        return results

    def _complete_models(self, arg: str) -> list[Completion]:
        # The user may have typed multiple specs; complete the last token.
        tokens  = arg.split()
        partial = "" if arg.endswith(" ") else (tokens[-1] if tokens else "")

        results: list[Completion] = []
        for provider in PROVIDER_ORDER:
            has_key = bool(self._state.api_keys.get(_KEY_ENV[provider], "").strip())
            for m in all_models():
                if m.provider != provider:
                    continue
                spec = f"{provider}:{m.model_id}"
                if not spec.startswith(partial):
                    continue
                rec_marker = "  ★" if m.recommended else ""
                key_note   = "" if has_key else " — no API key"
                results.append(
                    Completion(
                        spec,
                        start_position=-len(partial),
                        display=f"{spec}{rec_marker}",
                        display_meta=PROVIDER_DISPLAY[provider] + key_note,
                    )
                )
        return self._grouped(self._complete_local(partial), results)

    # ── Planner model completions ─────────────────────────────────────────────

    def _complete_planner(self, arg: str) -> list[Completion]:
        partial = arg.strip()
        results: list[Completion] = []
        for provider in PROVIDER_ORDER:
            has_key = bool(self._state.api_keys.get(_KEY_ENV[provider], "").strip())
            for m in all_models():
                if m.provider != provider:
                    continue
                if not m.model_id.startswith(partial):
                    continue
                warn     = " — ⚠ not recommended" if not m.planner_ok else ""
                key_note = "" if has_key else " — no API key"
                results.append(
                    Completion(
                        m.model_id,
                        start_position=-len(partial),
                        display=m.model_id,
                        display_meta=PROVIDER_DISPLAY[provider] + warn + key_note,
                    )
                )
        return self._grouped(self._complete_local(partial), results)

    # ── Directory path completions ─────────────────────────────────────────────

    def _complete_endpoint(self, arg: str) -> list[Completion]:
        """Complete ``/endpoint`` sub-commands, then profiles or endpoint ids."""
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        typing_sub = len(parts) <= 1 and not arg.endswith(" ")
        if typing_sub:
            return [
                Completion(
                    name,
                    start_position=-len(sub),
                    display=name,
                    display_meta=desc,
                )
                for name, desc in _ENDPOINT_SUBCOMMANDS
                if name.startswith(sub)
            ]
        partial = (parts[1] if len(parts) > 1 else "").strip().lower()
        if sub == "add":
            return [
                Completion(
                    profile,
                    start_position=-len(partial),
                    display=profile,
                    display_meta="preset",
                )
                for profile in _profile_ids()
                if profile.startswith(partial)
            ]
        if sub in _ENDPOINT_ID_ARGS:
            return [
                Completion(
                    endpoint_id,
                    start_position=-len(partial),
                    display=endpoint_id,
                    display_meta="endpoint",
                )
                for endpoint_id in self._state.endpoint_ids
                if endpoint_id.startswith(partial)
            ]
        return []

    def _complete_path(
        self, arg: str, complete_event: CompleteEvent
    ) -> list[Completion]:
        sub_doc = Document(arg, cursor_position=len(arg))
        return list(self._path_cpl.get_completions(sub_doc, complete_event))
