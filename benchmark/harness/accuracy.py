"""Accuracy oracle: run a project copy's test suite and count what passes.

Accuracy is ``passed / workload.expected_tests`` against a *fixed* expected count,
so that tests which never even get collected — e.g. because ``registry.py`` is left
with git conflict markers and fails to import — are correctly counted as failures
rather than silently dropped from the denominator.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors)")


def measure(project_dir: Path) -> int:
    """Run ``pytest`` on ``project_dir`` and return the number of tests that passed."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(project_dir),
            "-q", "--no-header", "-p", "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    passed = 0
    for count, kind in _SUMMARY.findall(proc.stdout + proc.stderr):
        if kind == "passed":
            passed = int(count)
    return passed
