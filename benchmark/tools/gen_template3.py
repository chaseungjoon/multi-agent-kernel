"""Generate the ``project_template_3`` benchmark target from ``template3_spec``.

Run from the repository root::

    python benchmark/tools/gen_template3.py

This writes the feature-module stubs (``app/*.py``), the four shared registry
tables (``routes.py``, ``events.py``, ``errors.py``, ``settings.py``), and the
test suite (``tests/test_operations.py``, ``tests/test_tables.py``) into
``benchmark/project_template_3/``. Everything is derived from
``harness/template3_spec.OPS``, so stubs, reference implementations, and tests
cannot drift. Re-run after editing the spec.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

from harness.template3_spec import (  # noqa: E402
    OPS,
    SHARED_TABLES,
    Op3Spec,
    expected_tests,
    modules,
    table_entries,
)

DEST = BENCH / "project_template_3"

_PKG_DOC = '''"""app (template 3) — a small storefront backend used as the real-world benchmark target.

{count} feature tasks across {nmod} feature modules ({mods}), each an unimplemented stub
modelled on real service code. Unlike the fully-contended toolkit templates, tasks here
register into ZERO, ONE, or TWO of four cross-cutting shared tables — ``routes``,
``events``, ``errors``, ``settings`` — the files real feature teams collide on. A
worktree-per-agent workflow conflicts on every shared table at merge time; MAK
serializes only same-table edits under node-level write locks and runs everything
else in parallel.
"""
'''

_TABLE_HEADER = '''"""{title}

``register``/``{reader}`` are already implemented. ``_register_all`` is a **shared,
contended** function: feature tasks across different modules each add one
``register(...)`` line to it. Under a worktree-per-agent workflow this file collides
at merge time; under MAK its edits serialize under one node-level write lock.
"""

from __future__ import annotations
'''

_ROUTES = _TABLE_HEADER.format(
    title="URL route table: maps route keys to feature handlers.",
    reader="dispatch",
) + '''
from collections.abc import Callable

from app import {imports}

ROUTES: dict[str, Callable[..., object]] = {{}}


def register(pattern: str, handler: Callable[..., object]) -> None:
    """Register ``handler`` for ``pattern`` (e.g. "GET /cart/total")."""
    ROUTES[pattern] = handler


def dispatch(pattern: str, *args: object) -> object:
    """Call the handler registered for ``pattern`` with ``args``."""
    if pattern not in ROUTES:
        raise KeyError(f"no route registered: {{pattern}}")
    return ROUTES[pattern](*args)


def _register_all() -> None:
    """Register every route. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
'''

_EVENTS = _TABLE_HEADER.format(
    title="Domain-event handler table: maps event names to feature handlers.",
    reader="emit",
) + '''
from collections.abc import Callable

from app import {imports}

HANDLERS: dict[str, Callable[..., object]] = {{}}


def register(event: str, handler: Callable[..., object]) -> None:
    """Register ``handler`` for ``event`` (e.g. "order.placed")."""
    HANDLERS[event] = handler


def emit(event: str, *args: object) -> object:
    """Invoke the handler registered for ``event`` with ``args``."""
    if event not in HANDLERS:
        raise KeyError(f"no handler registered: {{event}}")
    return HANDLERS[event](*args)


def _register_all() -> None:
    """Register every event handler. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
'''

_ERRORS = _TABLE_HEADER.format(
    title="Error-code catalog: maps stable error codes to user-facing messages.",
    reader="message_for",
) + '''
ERRORS: dict[str, str] = {}


def register(code: str, message: str) -> None:
    """Register the user-facing ``message`` for ``code``."""
    ERRORS[code] = message


def message_for(code: str) -> str:
    """Return the message registered for ``code``."""
    if code not in ERRORS:
        raise KeyError(f"no error registered: {code}")
    return ERRORS[code]


def _register_all() -> None:
    """Register every error code. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
'''

_SETTINGS = _TABLE_HEADER.format(
    title="Config defaults: maps setting keys to their default values.",
    reader="get",
) + '''
DEFAULTS: dict[str, object] = {}


def register(key: str, value: object) -> None:
    """Register the default ``value`` for ``key``."""
    DEFAULTS[key] = value


def get(key: str) -> object:
    """Return the default registered for ``key``."""
    if key not in DEFAULTS:
        raise KeyError(f"no default registered: {key}")
    return DEFAULTS[key]


def _register_all() -> None:
    """Register every default. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
'''

_CONFTEST = '''"""Project root on sys.path + a per-test timeout.

A real agent can implement one of the loop-based functions with an infinite loop.
Without a guard, that test would hang ``pytest`` forever and wedge the whole
benchmark. A small SIGALRM-based hook aborts any test that runs longer than the
limit, so the runaway test simply fails (and is correctly counted as not-passed)
instead of hanging. Applies equally to the MAK and worktree measurements, so it
does not favour either side.
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


def _stub(op: Op3Spec) -> str:
    return f'{op.header}\n    """{op.doc}"""\n    raise NotImplementedError\n'


def _module_file(module: str) -> str:
    ops = [op for op in OPS if op.module == module]
    body = "\n\n".join(_stub(op) for op in ops)
    return (
        f'"""{module} feature operations (unimplemented stubs)."""\n\n'
        f"from __future__ import annotations\n\n\n{body}"
    )


def _test_operations() -> str:
    mods = ", ".join(modules())
    lines = [
        '"""Per-operation specification — passes once each stub is implemented correctly."""',
        "",
        "import pytest",
        "",
        f"from app import {mods}",
        "",
    ]
    for op in OPS:
        lines.append("")
        lines.append(f"def test_{op.name}():")
        for args, expected in op.cases:
            lines.append(f"    assert {op.module}.{op.name}(*{args!r}) == {expected!r}")
        for args in op.raises:
            lines.append("    with pytest.raises(ValueError):")
            lines.append(f"        {op.module}.{op.name}(*{args!r})")
    return "\n".join(lines) + "\n"


def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _test_tables() -> str:
    """Two tests per shared-table entry: it is registered, and it works."""
    entries = table_entries()
    lines = [
        '"""Shared-table specification — every feature registration must land intact.',
        "",
        "This is the oracle for the contended files: a registration dropped in a bad",
        "merge (or a lost node-store update) fails here even when the feature's own",
        "function tests pass.",
        '"""',
        "",
        "from app import errors, events, routes, settings",
        "",
    ]
    for op, entry in entries["routes"]:
        args, expected = op.cases[0]
        s = _slug(entry.key)
        lines += [
            "",
            f"def test_route_registered_{s}():",
            f"    assert {entry.key!r} in routes.ROUTES",
            "",
            "",
            f"def test_route_dispatches_{s}():",
            f"    assert routes.dispatch({entry.key!r}, *{args!r}) == {expected!r}",
        ]
    for op, entry in entries["events"]:
        args, expected = op.cases[0]
        s = _slug(entry.key)
        lines += [
            "",
            f"def test_event_registered_{s}():",
            f"    assert {entry.key!r} in events.HANDLERS",
            "",
            "",
            f"def test_event_emits_{s}():",
            f"    assert events.emit({entry.key!r}, *{args!r}) == {expected!r}",
        ]
    for _op, entry in entries["errors"]:
        s = _slug(entry.key)
        message = entry.value  # repr(...) of the message string
        lines += [
            "",
            f"def test_error_registered_{s}():",
            f"    assert {entry.key!r} in errors.ERRORS",
            "",
            "",
            f"def test_error_message_{s}():",
            f"    assert errors.message_for({entry.key!r}) == {message}",
        ]
    for _op, entry in entries["settings"]:
        s = _slug(entry.key)
        lines += [
            "",
            f"def test_setting_registered_{s}():",
            f"    assert {entry.key!r} in settings.DEFAULTS",
            "",
            "",
            f"def test_setting_value_{s}():",
            f"    assert settings.get({entry.key!r}) == {entry.value}",
        ]
    # De-duplicate the doubled blank lines the loop above produces at the seams.
    return "\n".join(lines).replace("\n\n\n\n", "\n\n\n") + "\n"


def main() -> int:
    mods = modules()
    imports = ", ".join(sorted(mods))
    (DEST / "app").mkdir(parents=True, exist_ok=True)
    (DEST / "tests").mkdir(parents=True, exist_ok=True)

    (DEST / "conftest.py").write_text(_CONFTEST)
    (DEST / "app" / "__init__.py").write_text(
        _PKG_DOC.format(count=len(OPS), nmod=len(mods), mods=", ".join(mods))
    )
    (DEST / "app" / "routes.py").write_text(_ROUTES.format(imports=imports))
    (DEST / "app" / "events.py").write_text(_EVENTS.format(imports=imports))
    (DEST / "app" / "errors.py").write_text(_ERRORS)
    (DEST / "app" / "settings.py").write_text(_SETTINGS)
    for module in mods:
        (DEST / "app" / f"{module}.py").write_text(_module_file(module))

    (DEST / "tests" / "test_operations.py").write_text(_test_operations())
    (DEST / "tests" / "test_tables.py").write_text(_test_tables())

    entries = table_entries()
    per_table = ", ".join(f"{t}={len(entries[t])}" for t in SHARED_TABLES)
    print(
        f"[gen] wrote {DEST.relative_to(BENCH.parent)}: {len(OPS)} ops, "
        f"{len(mods)} feature modules, {per_table}, "
        f"expected_tests={expected_tests()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
