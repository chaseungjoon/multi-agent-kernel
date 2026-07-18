"""Protocol-bridge wrappers that let MAK drive real coding CLIs.

The CLI adapters (``claude_code``, ``codex``, ``copilot``) speak MAK's
newline-delimited ``TaskBundle``/``TaskResult`` JSON protocol on their stdin and
stdout. Real CLIs (``claude``, ``codex``, ``gh copilot``) do not. Each wrapper in
this package is the bridge: it reads a ``TaskBundle`` line, turns it into a prompt
that asks the CLI for the rewritten source of each target node as a strict JSON
object, invokes the CLI non-interactively, parses that JSON back, and writes a
``TaskResult`` line. An adapter's ``default_command`` is
``python -m mak.agent_runner.wrappers.<name>``.
"""
