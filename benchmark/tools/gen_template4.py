"""Generate Template 4 stubs and acceptance tests from versioned contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent, indent

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

from harness.template4_spec import (  # noqa: E402
    MODELS,
    SHARED_TABLES,
    TASKS,
    Case,
    expected_tests,
    modules,
)
from harness.template4_workflows import WORKFLOWS  # noqa: E402

_IMPORTS = """from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission
"""


def _tests(cases: tuple[Case, ...]) -> str:
    imports = ", ".join((*modules(), *SHARED_TABLES))
    header = (
        '"""Acceptance oracle with independently specified expected outcomes."""\n\n'
        "from dataclasses import replace\n\nimport pytest\n\n"
        f"from service import ({imports})\n"
        "from service.models import Job, Submission\n"
    )
    return header + "".join(
        f"\n\ndef test_{case.name}() -> None:\n"
        f'    """Verify {case.name.replace("_", " ")}."""\n'
        + indent(dedent(case.body).strip(), "    ")
        + "\n"
        for case in cases
    )


def _table(module: str) -> str:
    imports = ", ".join(modules())
    return f'''"""Shared {module} wiring; build fresh state for every lookup."""

from service import {imports}


def _register_all() -> dict[str, object]:
    """Register feature handlers in a local table."""
    entries: dict[str, object] = {{}}
    register = entries.__setitem__
    return entries


def lookup(key: str) -> object:
    """Return a registered handler or raise KeyError."""
    return _register_all()[key]
'''


def generate(destination: Path) -> None:
    """Write a reproducible project to the supplied directory."""
    package = destination / "service"
    tests = destination / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        '"""Multi-tenant background-job service benchmark."""\n'
    )
    (package / "models.py").write_text(MODELS)
    for module in modules():
        body = "\n\n".join(task.stub for task in TASKS if task.module == module)
        (package / f"{module}.py").write_text(
            f'"""{module.title()} service operations awaiting implementation."""\n\n'
            + _IMPORTS
            + "\n\n"
            + body
        )
    for module in SHARED_TABLES:
        (package / f"{module}.py").write_text(_table(module))
    _write_oracle(destination)


def _write_oracle(destination: Path) -> None:
    checks = tuple(case for task in TASKS for case in task.cases)
    wiring = tuple(
        Case(
            f"{table}_{task.name}",
            f'assert {table}.lookup("{task.name}") is {task.module}.{task.name}',
        )
        for task in TASKS
        for table in task.tables
    )
    for name, cases in (
        ("contracts", checks),
        ("wiring", wiring),
        ("workflows", WORKFLOWS),
    ):
        (destination / "tests" / f"test_{name}.py").write_text(_tests(cases))
    (destination / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\ntestpaths = tests\n"
    )
    (
        destination / "conftest.py"
    ).write_text('''"""Bound acceptance tests, including unbounded agent loops."""

from collections.abc import Iterator
import signal
from types import FrameType

import pytest


def _timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("benchmark acceptance test exceeded five seconds")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Install and restore a per-test alarm on POSIX hosts."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
''')


def main() -> int:
    """Regenerate the checked-in fixture."""
    generate(BENCH / "project_template_4")
    print(
        f"Template 4: {len(TASKS)} tasks, {len(modules())} modules, "
        f"{expected_tests()} tests"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
