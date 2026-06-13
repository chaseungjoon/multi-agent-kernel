"""Generate the ``project_template_2`` benchmark target from ``template2_spec``.

Run from the repository root::

    python benchmark/tools/gen_template2.py

This writes the stub modules (`toolkit/*.py`), the shared `registry.py`, and the
test suite (`tests/test_operations.py`, `tests/test_registry.py`) into
``benchmark/project_template_2/``. Everything is derived from ``template2_spec.OPS``,
so stubs, reference implementations, and tests cannot drift. Re-run after editing the
spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

from harness.template2_spec import OPS, OpSpec, expected_tests, modules  # noqa: E402

DEST = BENCH / "project_template_2"

_PKG_DOC = '''"""toolkit (template 2) — a large operation library used as the heavy benchmark target.

{count} operations across {nmod} modules ({mods}), each an unimplemented stub modelled
on real open-source utility functions. Every operation must register itself in the
shared dispatch table ``registry._register_all`` — the single contention point that a
worktree-per-agent workflow collides on at merge time and MAK serializes under one
node-level write lock.
"""
'''

_REGISTRY = '''"""Central operation registry and dispatcher.

``register``/``run`` are already implemented. ``_register_all`` is the **shared,
contended** function: every one of the {count} operations must add one ``register(...)``
line to it. Because all the work funnels through this single function, a
worktree-per-agent workflow collides here at merge time, while MAK serializes the edits
under one node-level write lock so none are lost.
"""

from __future__ import annotations

from collections.abc import Callable

from toolkit import {imports}

OPERATIONS: dict[str, Callable[..., object]] = {{}}


def register(name: str, fn: Callable[..., object]) -> None:
    """Register operation ``fn`` under ``name``."""
    OPERATIONS[name] = fn


def run(name: str, *args: object) -> object:
    """Call the operation registered under ``name`` with ``args``."""
    if name not in OPERATIONS:
        raise KeyError(f"no operation registered: {{name}}")
    return OPERATIONS[name](*args)


def _register_all() -> None:
    """Register every operation. Each operation adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
'''

_CONFTEST = '''"""Project root on sys.path + a per-test timeout.

A real agent can implement one of the algorithmic functions (e.g. a while-based
``collatz_steps`` or ``nth_prime``) with an infinite loop. Without a guard, that test
would hang ``pytest`` forever and wedge the whole benchmark. A small SIGALRM-based hook
aborts any test that runs longer than the limit, so the runaway test simply fails (and
is correctly counted as not-passed) instead of hanging. Applies equally to the MAK and
worktree measurements, so it does not favour either side.
"""

import signal

import pytest

_PER_TEST_TIMEOUT_S = 5


def _on_timeout(signum, frame):
    raise TimeoutError(f"test exceeded {_PER_TEST_TIMEOUT_S}s (likely an infinite loop)")


signal.signal(signal.SIGALRM, _on_timeout)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    signal.setitimer(signal.ITIMER_REAL, _PER_TEST_TIMEOUT_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
'''


def _stub(op: OpSpec) -> str:
    return f'{op.header}\n    """{op.doc}"""\n    raise NotImplementedError\n'


def _module_file(module: str) -> str:
    ops = [op for op in OPS if op.module == module]
    body = "\n\n".join(_stub(op) for op in ops)
    return f'"""{module} operations (unimplemented stubs)."""\n\nfrom __future__ import annotations\n\n\n{body}'


def _test_operations() -> str:
    mods = ", ".join(modules())
    lines = ['"""Per-operation specification — passes once each stub is implemented correctly."""',
             "", "import pytest", "", f"from toolkit import {mods}", ""]
    for op in OPS:
        lines.append("")
        lines.append(f"def test_{op.name}():")
        for args, expected in op.cases:
            lines.append(f"    assert {op.module}.{op.name}(*{args!r}) == {expected!r}")
        for args in op.raises:
            lines.append("    with pytest.raises(ValueError):")
            lines.append(f"        {op.module}.{op.name}(*{args!r})")
    return "\n".join(lines) + "\n"


def _test_registry() -> str:
    lines = ['"""Dispatch specification — every operation must be registered in the shared table."""',
             "", "import pytest", "", "from toolkit import registry", "", "EXPECTED = {"]
    for op in OPS:
        args, expected = op.cases[0]
        lines.append(f"    {op.name!r}: ({args!r}, {expected!r}),")
    lines += [
        "}",
        "",
        "",
        '@pytest.mark.parametrize("name", sorted(EXPECTED))',
        "def test_operation_is_registered(name):",
        '    assert name in registry.OPERATIONS, f"{name!r} was never registered"',
        "",
        "",
        '@pytest.mark.parametrize("name", sorted(EXPECTED))',
        "def test_operation_dispatches_correctly(name):",
        "    args, expected = EXPECTED[name]",
        "    assert registry.run(name, *args) == expected",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    mods = modules()
    (DEST / "toolkit").mkdir(parents=True, exist_ok=True)
    (DEST / "tests").mkdir(parents=True, exist_ok=True)

    (DEST / "conftest.py").write_text(_CONFTEST)
    (DEST / "toolkit" / "__init__.py").write_text(
        _PKG_DOC.format(count=len(OPS), nmod=len(mods), mods=", ".join(mods))
    )
    (DEST / "toolkit" / "registry.py").write_text(
        _REGISTRY.format(count=len(OPS), imports=", ".join(sorted(mods)))
    )
    for module in mods:
        (DEST / "toolkit" / f"{module}.py").write_text(_module_file(module))

    (DEST / "tests" / "test_operations.py").write_text(_test_operations())
    (DEST / "tests" / "test_registry.py").write_text(_test_registry())

    print(f"[gen] wrote {DEST.relative_to(BENCH.parent)}: {len(OPS)} ops, "
          f"{len(mods)} modules, expected_tests={expected_tests()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
