"""Build the teardown test runner from configuration.

``Session.teardown`` accepts a ``test_runner: () -> (passed, output)`` and uses it
to gate an ``auto_push`` and to report a pass/fail after a run. This module turns
the ``session.test_command`` config value into such a callable: it runs the
command in the work dir and reports the outcome. When no command is configured
the factory returns ``None`` (teardown then skips the test step entirely, rather
than pretending a suite passed).
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

TestRunner = Callable[[], tuple[bool, str]]

# A generous ceiling so a real suite is never cut short, while a hung command
# still cannot wedge teardown forever.
_TEST_TIMEOUT_S = 1800.0


def build_test_runner(
    test_command: str | None, work_dir: Path
) -> TestRunner | None:
    """Return a callable that runs ``test_command`` in ``work_dir``, or None.

    ``None`` (no command configured) means teardown skips testing. Otherwise the
    returned callable runs the command and yields ``(passed, combined_output)``;
    a non-zero exit, a timeout, or a missing binary all report ``passed=False``
    with a diagnostic instead of raising, so teardown degrades gracefully.
    """
    if not test_command or not test_command.strip():
        return None
    argv = shlex.split(test_command)

    def run() -> tuple[bool, str]:
        try:
            result = subprocess.run(
                argv,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=_TEST_TIMEOUT_S,
            )
        except FileNotFoundError:
            return False, f"test command not found: {argv[0]!r}"
        except subprocess.TimeoutExpired:
            return False, f"test command timed out after {_TEST_TIMEOUT_S:.0f}s"
        except OSError as exc:
            return False, f"could not run test command: {exc}"
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output

    return run
