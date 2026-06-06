"""AgentRunner: dispatch a task to an adapter, API-first with a subprocess pool.

``assign(adapter, task)`` is the single entry point. It routes by adapter type
(PLANS.md §6.4):

- **API adapters** (the primary path) implement a ``send`` method — the runner
  calls ``format_task → send → parse_result`` and returns the structured
  ``TaskResult``. The adapter owns the SDK call; the runner owns failure policy.
- **Subprocess adapters** (the secondary/CLI path) are driven over stdin/stdout.
  The runner keeps an idle-process pool per agent type, writes the task as a JSON
  line, and reads the result line back under a timeout. On timeout the process is
  killed and the task is reported failed; on any failure the (possibly broken)
  process is discarded rather than returned to the pool.

Every path returns a ``TaskResult``: backend failures become ``success=False``
results (so the scheduler can re-queue), while a genuine misconfiguration — an
adapter that is neither API- nor subprocess-shaped — raises ``AgentError``.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Protocol, runtime_checkable

from mak.agent_runner.adapters.base_adapter import (
    AgentAdapter,
    SubprocessAgentAdapter,
)
from mak.core.exceptions import AgentError
from mak.core.types import TaskBundle, TaskResult

_DEFAULT_TIMEOUT_S = 300.0


@runtime_checkable
class ApiAdapter(Protocol):
    """An adapter that performs its own backend call via ``send`` (no subprocess)."""

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Translate a task bundle into the backend request payload."""
        ...

    def send(self, prompt: str) -> str:
        """Perform the backend call and return raw result text for parsing."""
        ...

    def parse_result(self, raw_output: str) -> TaskResult:
        """Parse the backend's raw response into a ``TaskResult``."""
        ...


class AgentRunner:
    """Routes tasks to API or subprocess adapters and enforces failure policy."""

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        discard_on_failure: bool = True,
    ) -> None:
        self._timeout_s = timeout_s
        self._discard_on_failure = discard_on_failure
        self._pool: dict[str, list[subprocess.Popen[str]]] = {}
        self._lock = threading.Lock()

    def assign(
        self,
        adapter: AgentAdapter,
        task: TaskBundle,
        working_dir: str = ".",
    ) -> TaskResult:
        """Dispatch ``task`` to ``adapter`` and return its structured result."""
        # Subprocess check first: a SubprocessAgentAdapter also satisfies the API
        # protocol's method names but must be driven over pipes, not ``send``.
        if isinstance(adapter, SubprocessAgentAdapter):
            return self._assign_subprocess(adapter, task, working_dir)
        if isinstance(adapter, ApiAdapter):
            return self._assign_api(adapter, task)
        raise AgentError(
            f"adapter '{type(adapter).__name__}' is neither an API adapter "
            "(no 'send' method) nor a subprocess adapter"
        )

    # -- API path ----------------------------------------------------------

    def _assign_api(self, adapter: ApiAdapter, task: TaskBundle) -> TaskResult:
        prompt = adapter.format_task(task)
        try:
            raw = adapter.send(prompt)
            return adapter.parse_result(raw)
        except Exception as exc:
            # A backend or parse failure is a failed task, not a runner crash —
            # return a result so the scheduler can release locks and re-queue.
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"api call failed: {exc}",
            )

    # -- subprocess path ---------------------------------------------------

    def _assign_subprocess(
        self,
        adapter: SubprocessAgentAdapter,
        task: TaskBundle,
        working_dir: str,
    ) -> TaskResult:
        proc = self._acquire_process(adapter, working_dir)
        # Hard failures below (io error, timeout, closed stream, unparseable
        # output) mean the process is in an unknown state and is always dropped.
        try:
            self._write_task(proc, adapter.format_task(task))
            line = self._read_result(proc, self._timeout_s)
        except Exception as exc:
            self._drop(adapter.agent_type, proc)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"subprocess io error: {exc}",
            )

        if line is None:
            self._drop(adapter.agent_type, proc)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"agent timed out after {self._timeout_s}s",
            )
        if not line.strip():
            self._drop(adapter.agent_type, proc)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="agent produced no result (closed stream)",
            )

        try:
            result = adapter.parse_result(line)
        except Exception as exc:
            self._drop(adapter.agent_type, proc)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"could not parse agent result: {exc}",
            )

        # A clean (well-formed) failed result leaves the process healthy: whether
        # to keep it for reuse is governed by ``discard_on_failure``.
        if result.success or not self._discard_on_failure:
            self._release_process(adapter.agent_type, proc)
        else:
            self._drop(adapter.agent_type, proc)
        return result

    def _acquire_process(
        self, adapter: SubprocessAgentAdapter, working_dir: str
    ) -> subprocess.Popen[str]:
        with self._lock:
            pool = self._pool.setdefault(adapter.agent_type, [])
            while pool:
                proc = pool.pop()
                if proc.poll() is None:  # still alive → reuse
                    return proc
        return adapter.spawn(working_dir)

    def _release_process(
        self, agent_type: str, proc: subprocess.Popen[str]
    ) -> None:
        if proc.poll() is not None:
            return
        with self._lock:
            self._pool.setdefault(agent_type, []).append(proc)

    def _drop(self, agent_type: str, proc: subprocess.Popen[str]) -> None:
        """Remove a process from the pool and terminate it (hard discard)."""
        with self._lock:
            pool = self._pool.get(agent_type, [])
            if proc in pool:
                pool.remove(proc)
        self._terminate(proc)

    @staticmethod
    def _write_task(proc: subprocess.Popen[str], payload: str) -> None:
        if proc.stdin is None:
            raise AgentError("subprocess has no stdin pipe")
        if not payload.endswith("\n"):
            payload += "\n"
        proc.stdin.write(payload)
        proc.stdin.flush()

    @staticmethod
    def _read_result(proc: subprocess.Popen[str], timeout: float) -> str | None:
        """Read stdout until a complete JSON object is found (RA-6).

        Tolerates noisy CLI agents: non-JSON preamble lines (logs, progress) are
        skipped, and a result printed across multiple lines is accumulated until
        the buffer parses as a JSON object. Returns the JSON text, ``None`` on
        timeout (process unresponsive), or ``""`` on EOF without a result.
        """
        if proc.stdout is None:
            raise AgentError("subprocess has no stdout pipe")
        box: list[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            buffer = ""
            for line in proc.stdout:
                if not line.strip():
                    continue
                single = _as_json_object(line)
                if single is not None:
                    box.append(single)
                    return
                buffer += line
                accumulated = _as_json_object(buffer)
                if accumulated is not None:
                    box.append(accumulated)
                    return
            box.append("")  # EOF without a parseable result

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None
        return box[0] if box else ""

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def shutdown(self) -> None:
        """Terminate every pooled subprocess. Call at session teardown."""
        with self._lock:
            pools = list(self._pool.values())
            self._pool.clear()
        for pool in pools:
            for proc in pool:
                self._terminate(proc)


def _as_json_object(text: str) -> str | None:
    """Return ``text`` stripped if it parses as a JSON object, else ``None``.

    Used to pick the result line out of noisy agent stdout: a debug log or a JSON
    array/number is not a ``TaskResult`` and is rejected; only a JSON object
    (``{...}``) is accepted.
    """
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate if isinstance(parsed, dict) else None
