"""The shared protocol bridge between MAK's wire protocol and a real coding CLI.

A wrapper module (``claude_code``/``codex``/``copilot``) declares a :class:`CliSpec`
and calls :func:`main`. The bridge then, per task line on stdin:

1. decodes the ``TaskBundle``;
2. builds a prompt that hands the CLI each target node's current source plus
   read-only context and asks for a **strict JSON object** mapping each node id to
   its full rewritten source;
3. invokes the CLI non-interactively (prompt via argv or stdin), with a timeout;
4. extracts the JSON object from the CLI's (possibly noisy) stdout;
5. emits a ``TaskResult`` line — including a clean ``success=False`` result on any
   failure, so the caller never hangs waiting on a crashed bridge.

The exact CLI invocation is overridable per wrapper via the ``MAK_<AGENT>_CMD``
environment variable (shell-split), or per call via ``--cli <binary>``, so an
operator can correct a CLI's flags without editing code.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from mak.agent_runner.protocol import decode_task_bundle, encode_task_result
from mak.core.types import NodeId, TaskBundle, TaskResult

_DEFAULT_TIMEOUT_S = 240.0


@dataclass(frozen=True)
class CliSpec:
    """How to invoke one underlying CLI non-interactively."""

    agent_type: str
    cli_name: str  # binary that must exist on PATH (health check)
    base_argv: tuple[str, ...]  # e.g. ("claude", "-p"); prompt appended/piped
    prompt_via: str = "arg"  # "arg" (append) | "stdin" (pipe to the CLI)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def resolved_argv(self, cli_override: str | None) -> list[str]:
        """Return the base argv, honoring env and ``--cli`` overrides.

        ``MAK_<AGENT>_CMD`` (shell-split) replaces the whole base command;
        ``--cli`` replaces just the binary (argv[0]).
        """
        env = os.environ.get(f"MAK_{self.agent_type.upper()}_CMD")
        argv = shlex.split(env) if env else list(self.base_argv)
        if cli_override:
            argv = [cli_override, *argv[1:]] if argv else [cli_override]
        return argv


_PROMPT_TEMPLATE = """\
You are a coding agent editing Python source. Perform this task:

{description}

Rewrite ONLY these nodes. Each block below is a node id and its current source:
{targets}
{context}
Respond with ONLY a JSON object (no prose, no code fences) mapping each node id \
above to the COMPLETE rewritten Python source of that node (never a diff). Use \
exactly these keys:
{ids}
If you cannot complete the task, respond with the JSON object {{"error": "<why>"}}.
"""


def build_prompt(bundle: TaskBundle) -> str:
    """Compose the CLI prompt from a task bundle's targets and context."""
    targets = []
    for node_id in bundle.target_nodes:
        source = bundle.context.get(f"write_source:{node_id}", "")
        targets.append(f"### {node_id}\n{source}")
    reads = [
        f"### {key[len('read_source:'):]} (read-only)\n{value}"
        for key, value in bundle.context.items()
        if key.startswith("read_source:")
    ]
    context = ("\nRead-only context:\n" + "\n".join(reads) + "\n") if reads else ""
    ids = "\n".join(str(n) for n in bundle.target_nodes)
    return _PROMPT_TEMPLATE.format(
        description=bundle.description,
        targets="\n".join(targets) or "(none)",
        context=context,
        ids=ids or "(none)",
    )


def extract_json_object(text: str) -> dict[str, object] | None:
    """Return the first balanced top-level JSON object in ``text``, or None.

    Tolerates a code fence or surrounding prose by scanning for the first ``{``
    and tracking brace depth (ignoring braces inside JSON strings).
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # unbalanced-looking; try the next '{'
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    return None


def _invoke_cli(spec: CliSpec, argv: list[str], prompt: str) -> str:
    """Run the CLI with ``prompt`` and return its stdout (raises on failure)."""
    if spec.prompt_via == "stdin":
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
        )
    else:
        proc = subprocess.run(
            [*argv, prompt],
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{spec.cli_name} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )
    return proc.stdout


def run_task(spec: CliSpec, bundle: TaskBundle, cli_override: str | None) -> TaskResult:
    """Drive the CLI for one task and return a ``TaskResult`` (never raises)."""
    argv = spec.resolved_argv(cli_override)
    if not argv or shutil.which(argv[0]) is None:
        return TaskResult(
            task_id=bundle.task_id,
            success=False,
            error=f"CLI '{argv[0] if argv else spec.cli_name}' not found on PATH",
        )
    try:
        stdout = _invoke_cli(spec, argv, build_prompt(bundle))
    except subprocess.TimeoutExpired:
        return TaskResult(
            task_id=bundle.task_id,
            success=False,
            error=f"{spec.cli_name} timed out after {spec.timeout_s:.0f}s",
        )
    except (OSError, RuntimeError) as exc:
        return TaskResult(task_id=bundle.task_id, success=False, error=str(exc))

    obj = extract_json_object(stdout)
    if obj is None:
        return TaskResult(
            task_id=bundle.task_id,
            success=False,
            error=f"{spec.cli_name} produced no parseable JSON object",
        )
    if "error" in obj and len(obj) == 1:
        return TaskResult(
            task_id=bundle.task_id, success=False, error=str(obj["error"])
        )

    # Keep only authorized target nodes; the store ignores out-of-scope edits too,
    # but filtering here keeps the wire result honest.
    authorized = {str(n) for n in bundle.target_nodes}
    new_sources: dict[NodeId, str] = {
        NodeId(k): str(v)
        for k, v in obj.items()
        if k in authorized and isinstance(v, str)
    }
    return TaskResult(
        task_id=bundle.task_id,
        success=bool(new_sources),
        modified_nodes=list(new_sources),
        new_sources=new_sources,
        error=None if new_sources else "CLI returned no source for any target node",
    )


def _parse_argv(argv: Sequence[str]) -> tuple[bool, str | None]:
    """Return ``(health_check, cli_override)`` from the wrapper's own args."""
    health = False
    cli_override: str | None = None
    it = iter(argv)
    for arg in it:
        if arg == "--health-check":
            health = True
        elif arg == "--cli":
            cli_override = next(it, None)
    return health, cli_override


def main(spec: CliSpec, argv: Sequence[str] | None = None) -> int:
    """Run the wrapper: a health check, or bridge stdin tasks to the CLI."""
    health, cli_override = _parse_argv(sys.argv[1:] if argv is None else argv)
    if health:
        default = spec.base_argv[0] if spec.base_argv else spec.cli_name
        binary = cli_override or default
        return 0 if shutil.which(binary) is not None else 1

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            bundle = decode_task_bundle(line)
        except (ValueError, KeyError):
            continue  # not a task bundle; ignore noise on the pipe
        result = run_task(spec, bundle, cli_override)
        sys.stdout.write(encode_task_result(result))
        sys.stdout.flush()
    return 0
