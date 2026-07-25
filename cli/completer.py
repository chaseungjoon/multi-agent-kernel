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
from cli.core.state import CliState

COMMANDS: list[tuple[str, str]] = [
    ("/models",     "Select agent models"),
    ("/planner",    "Switch the planner model"),
    ("/refresh-models", "Re-fetch the model list from each provider"),
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

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}


class MakCompleter(Completer):
    """Tab / inline completions for slash commands."""

    def __init__(self, state: CliState) -> None:
        self._state    = state
        self._path_cpl = PathCompleter(only_directories=True, expanduser=True)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> list[Completion]:
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
        return results

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
        return results

    # ── Directory path completions ─────────────────────────────────────────────

    def _complete_path(
        self, arg: str, complete_event: CompleteEvent
    ) -> list[Completion]:
        sub_doc = Document(arg, cursor_position=len(arg))
        return list(self._path_cpl.get_completions(sub_doc, complete_event))
