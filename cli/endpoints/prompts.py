"""The input seam for the ``/endpoint`` wizard, plus the shared askers.

Every question the wizard asks goes through :func:`ask`, so a test drives the
whole flow by replacing one function — the same discipline ``cli/local.py``
uses for its own wizard.

**Cancellation is a value, not an exception.** Ctrl-C or EOF at any prompt
returns :data:`CANCELLED`, which the wizard checks after each step and turns
into "nothing changed". Raising would work too, but a sentinel makes the
"gather everything, then commit" shape explicit at every call site: there is no
path where half the answers have been applied.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console

from cli.ui import ACCENT

# Returned by every asker when the user cancels. Distinct from an empty string,
# which is a legitimate answer meaning "accept the default" or "leave unset".
CANCELLED = "\x00cancelled"


def ask(console: Console, prompt: str, default: str = "") -> str:
    """Ask one question, returning :data:`CANCELLED` on Ctrl-C / EOF.

    The single input seam of the wizard.
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    style = Style.from_dict({"": "#c9d1d9", "prompt": f"{ACCENT} bold"})
    try:
        answer = pt_prompt(f"  {prompt}", default=default, style=style)
    except (KeyboardInterrupt, EOFError):
        console.print()
        return CANCELLED
    return answer.strip()


def ask_secret(console: Console, prompt: str) -> str:
    """Ask for a credential without echoing it, returning :data:`CANCELLED`.

    Masked rather than hidden entirely: the user needs to see that keystrokes
    are landing. The value is never echoed back, never logged, and never stored
    anywhere but the mode-0600 ``.env``.
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    style = Style.from_dict({"": "#c9d1d9", "prompt": f"{ACCENT} bold"})
    try:
        answer = pt_prompt(f"  {prompt}", is_password=True, style=style)
    except (KeyboardInterrupt, EOFError):
        console.print()
        return CANCELLED
    return answer.strip()


def choose(
    console: Console,
    options: Sequence[str],
    prompt: str,
    default_index: int = 0,
) -> int:
    """Print a numbered menu and return the chosen index.

    Returns ``-1`` when the user cancels, so a caller can distinguish "took the
    default" from "backed out" — the wizard must not commit on the latter.
    """
    for index, option in enumerate(options, 1):
        console.print(f"    [dim]{index:>2})[/dim]  {option}")
    console.print()
    raw = ask(console, f"{prompt} (1–{len(options)}) [{default_index + 1}]: ")
    if raw == CANCELLED:
        return -1
    if not raw:
        return default_index
    try:
        index = int(raw) - 1
    except ValueError:
        return default_index
    return index if 0 <= index < len(options) else default_index


def confirm(console: Console, question: str, *, default: bool = False) -> bool | None:
    """Ask a yes/no question. Returns None when the user cancels.

    Defaults to **no** unless told otherwise: every question this wizard asks
    with a yes-default is one where yes is the safe answer, and the ones that
    spend money or weaken transport security are not among them.
    """
    suffix = "[Y/n]" if default else "[y/N]"
    raw = ask(console, f"{question} {suffix}: ")
    if raw == CANCELLED:
        return None
    if not raw:
        return default
    return raw.lower() in ("y", "yes")
