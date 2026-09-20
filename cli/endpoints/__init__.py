"""The ``/endpoint`` command: manage OpenAI-compatible endpoints interactively.

Split into three modules rather than one file, because this surface has three
genuinely separate jobs and ``cli/local.py`` (916 lines) is the shape to avoid:

* ``prompts`` — the input seam and the small shared askers;
* ``render`` — turning endpoints into the lines a terminal shows;
* ``wizard`` — the add/edit flow, which gathers everything before it commits;
* ``commands`` — subcommand dispatch and the non-wizard subcommands.
"""

from cli.endpoints.commands import cmd_endpoint, print_endpoint_help

__all__ = ["cmd_endpoint", "print_endpoint_help"]
