"""The benchmark workloads: operations, reference implementations, and assignment.

A :class:`Workload` bundles everything project-specific — the template directory,
the operation list, the modules, and the expected test count — so the runners and
the mock backend stay a single source of truth and the projects can never drift
apart. Four workloads ship:

- ``basic`` — the original 9-operation ``toolkit`` (3 modules).
- ``2`` — a larger, harder 90-operation ``toolkit`` (9 modules), several of them
  real algorithms.
- ``3`` — the *real-world contention* target: a storefront backend (``app``) with
  58 feature tasks across 8 feature modules and **four** cross-cutting shared
  tables (``routes``/``events``/``errors``/``settings``), each task registering
  into zero, one, or two of them.
- ``4`` — a planned multi-tenant job service with 24 function tasks, six feature
  modules, three stateless wiring tables, and 152 acceptance checks.

``basic`` and ``2`` share the *maximally contended* shape — one shared dispatch
function, ``registry._register_all``, that every operation adds one line to.
``3`` is *partially* contended: contention is spread over four shared
``_register_all`` functions, and much of the work touches none of them. Every
shared table follows the same in-file protocol (``register(...)`` lines inside a
``_register_all``), so one deterministic union-merge covers all workloads.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.template2_spec import OPS as _T2_SPEC
from harness.template2_spec import expected_tests as _t2_tests
from harness.template2_spec import modules as _t2_modules
from harness.template3_spec import OPS as _T3_SPEC
from harness.template3_spec import expected_tests as _t3_tests
from harness.template3_spec import modules as _t3_modules
from harness.template4_spec import CONTEXT as _T4_CONTEXT
from harness.template4_spec import SHARED_TABLES as _T4_TABLES
from harness.template4_spec import TASKS as _T4_TASKS
from harness.template4_spec import expected_tests as _t4_tests
from harness.template4_spec import modules as _t4_modules

REGISTRY_NODE = "toolkit/registry.py::function::_register_all"


@dataclass(frozen=True)
class Registration:
    """One edit to a shared table: add ``line`` to ``<module>._register_all``."""

    module: str  # the shared module (file) under the workload's package
    line: str  # the single line to add to that module's ``_register_all``


@dataclass(frozen=True)
class Operation:
    """One unit of work: implement a function and perform its shared registrations."""

    name: str  # the name it is registered under
    module: str  # the feature module the function lives in
    func: str  # the function name
    reference: str  # a correct implementation, used by the mock backend
    package: str = "toolkit"  # top-level package directory of the template
    registrations: tuple[Registration, ...] = ()  # shared-table edits (may be empty)
    context: str = ""  # public project context and optional planner guidance

    @property
    def func_node(self) -> str:
        """The MAK node id of the function this operation implements."""
        return f"{self.package}/{self.module}.py::function::{self.func}"

    def shared_node(self, reg: Registration) -> str:
        """The MAK node id of the shared ``_register_all`` this edit targets."""
        return f"{self.package}/{reg.module}.py::function::_register_all"


# -- basic workload (3 modules, 9 operations) -------------------------------
#
# Each reference keeps its stub's exact signature, annotations included: the
# mock backend stands in for a compliant agent ("keep the exact same function
# name and signature"), and MAK enforces the body-only declaration these tasks
# make — a reference that dropped the annotations would be an interface change.

_BASIC_OPERATIONS: list[Operation] = [
    Operation("upper", "strings", "upper", "def upper(s: str) -> str:\n    return s.upper()\n"),
    Operation("reverse", "strings", "reverse", "def reverse(s: str) -> str:\n    return s[::-1]\n"),
    Operation(
        "count_vowels",
        "strings",
        "count_vowels",
        'def count_vowels(s: str) -> int:\n    return sum(1 for c in s if c.lower() in "aeiou")\n',
    ),
    Operation("add", "numbers", "add", "def add(a: int, b: int) -> int:\n    return a + b\n"),
    Operation(
        "factorial",
        "numbers",
        "factorial",
        "def factorial(n: int) -> int:\n"
        "    if n < 0:\n"
        '        raise ValueError("n must be non-negative")\n'
        "    result = 1\n"
        "    for i in range(2, n + 1):\n"
        "        result *= i\n"
        "    return result\n",
    ),
    Operation(
        "is_prime",
        "numbers",
        "is_prime",
        "def is_prime(n: int) -> bool:\n"
        "    if n < 2:\n"
        "        return False\n"
        "    i = 2\n"
        "    while i * i <= n:\n"
        "        if n % i == 0:\n"
        "            return False\n"
        "        i += 1\n"
        "    return True\n",
    ),
    Operation(
        "unique",
        "sequences",
        "unique",
        "def unique(items: Sequence[T]) -> list[T]:\n"
        "    seen = set()\n"
        "    result = []\n"
        "    for item in items:\n"
        "        if item not in seen:\n"
        "            seen.add(item)\n"
        "            result.append(item)\n"
        "    return result\n",
    ),
    Operation(
        "maximum",
        "sequences",
        "maximum",
        "def maximum(items: Sequence[T]) -> T:\n"
        "    if not items:\n"
        '        raise ValueError("empty sequence")\n'
        "    return max(items)\n",
    ),
    Operation(
        "first",
        "sequences",
        "first",
        "def first(items: Sequence[T]) -> T:\n"
        "    if not items:\n"
        '        raise ValueError("empty sequence")\n'
        "    return items[0]\n",
    ),
]

# -- template 2 workload (9 modules, 90 operations) — built from template2_spec ---
#
# The operations, references, and tests are all generated from
# ``template2_spec`` (see ``tools/gen_template2.py``), so they cannot drift.

_TEMPLATE2_OPERATIONS: list[Operation] = [
    Operation(op.name, op.module, op.name, op.source) for op in _T2_SPEC
]


def _with_single_registry(ops: list[Operation]) -> list[Operation]:
    """Attach the classic single-registry registration to each toolkit operation."""
    from dataclasses import replace

    return [
        replace(
            op,
            registrations=(
                Registration(
                    "registry", f'    register("{op.name}", {op.module}.{op.func})'
                ),
            ),
        )
        for op in ops
    ]


# -- template 3 workload (8 feature modules + 4 shared tables) — from template3_spec

_TEMPLATE3_OPERATIONS: list[Operation] = [
    Operation(
        op.name,
        op.module,
        op.name,
        op.source,
        package="app",
        registrations=tuple(
            Registration(entry.table, entry.line) for entry in op.entries
        ),
    )
    for op in _T3_SPEC
]


@dataclass(frozen=True)
class Workload:
    """A complete benchmark target: its template, operations, and oracle size."""

    name: str  # "basic" | "2" | "3" | "4"
    template: str  # template directory name under benchmark/
    label: str  # human label for reports
    blurb: str  # one-line description for reports
    operations: list[Operation]
    modules: list[str]
    expected_tests: int
    package: str = "toolkit"  # top-level package directory inside the template
    shared_modules: tuple[str, ...] = ("registry",)  # contended `_register_all` files

    def shared_files(self) -> list[str]:
        """Repo-relative paths of the contended shared-table files."""
        return [f"{self.package}/{m}.py" for m in self.shared_modules]


WORKLOADS: dict[str, Workload] = {
    "4": Workload(
        name="4",
        template="project_template_4",
        label="Template 4",
        blurb="Multi-tenant background-job service: 24 tasks, 6 feature modules, "
        "3 shared tables; tenant isolation, idempotency, retries, leases and recovery.",
        operations=[
            Operation(
                task.name, task.module, task.name, task.reference,
                package="service",
                registrations=tuple(
                    Registration(table, f"    register({task.name!r}, {task.module}.{task.name})")
                    for table in task.tables
                ),
                context=_T4_CONTEXT,
            )
            for task in _T4_TASKS
        ],
        modules=list(_t4_modules()),
        expected_tests=_t4_tests(),
        package="service",
        shared_modules=_T4_TABLES,
    ),
    "basic": Workload(
        name="basic",
        template="project_template",
        label="Basic toolkit (9 ops)",
        blurb="9 operations across 3 modules (strings, numbers, sequences) + 1 shared "
        "registry function; 30 tests as the accuracy oracle.",
        operations=_with_single_registry(_BASIC_OPERATIONS),
        modules=["strings", "numbers", "sequences"],
        expected_tests=30,
    ),
    "2": Workload(
        name="2",
        template="project_template_2",
        label="Template 2 (90 ops)",
        blurb="90 operations across 9 modules (strkit, numkit, seqkit, dictkit, datekit, "
        "mathkit, parsekit, setkit, codekit) + 1 shared registry function — utility "
        "functions in the spirit of boltons/more-itertools (Levenshtein, Roman numerals, "
        "calendar math, sieves, parsers, set ops, ciphers); 270 tests as the oracle.",
        operations=_with_single_registry(_TEMPLATE2_OPERATIONS),
        modules=_t2_modules(),
        expected_tests=_t2_tests(),
    ),
    "3": Workload(
        name="3",
        template="project_template_3",
        label="Template 3 (real-world, 58 tasks)",
        blurb="58 feature tasks across 8 feature modules of a storefront backend "
        "(accounts, catalog, cart, orders, payments, shipping, reviews, search) + 4 "
        "cross-cutting shared tables (routes, events, errors, settings) that tasks "
        "register into — zero, one, or two tables each. Partial contention: the shape "
        "of real feature-team work; 148 tests as the oracle.",
        operations=_TEMPLATE3_OPERATIONS,
        modules=_t3_modules(),
        expected_tests=_t3_tests(),
        package="app",
        shared_modules=("routes", "events", "errors", "settings"),
    ),
}


def operation_by_func_node(operations: list[Operation], node_id: str) -> Operation:
    """Return the operation in ``operations`` whose function node is ``node_id``."""
    for op in operations:
        if op.func_node == node_id:
            return op
    raise KeyError(f"no operation for node {node_id}")


def assign(workload: Workload, num_agents: int) -> list[int]:
    """Assign each operation to an agent, grouping a whole module to one agent.

    Because every operation in a module goes to the *same* agent, the per-module
    source files are never edited by two agents at once — so the only places the
    worktree branches collide at merge time are the shared ``_register_all``
    functions (one registry in the toolkit templates; the four cross-cutting
    tables in template 3). That isolates the conflicts to exactly the contended
    nodes, which is the comparison we want to make.
    """
    module_agent = {module: i % num_agents for i, module in enumerate(workload.modules)}
    return [module_agent[op.module] for op in workload.operations]


def add_registration(current_source: str, register_line: str) -> str:
    """Return ``_register_all`` source with ``register_line`` added (idempotent).

    The line is inserted *in place* — after the last existing ``register(...)``
    line, in place of the ``raise NotImplementedError`` stub, or before the
    local table's ``return`` — and nothing else about the function changes.
    That is what an agent appending one registration does, and it is what makes
    the edit recognisably a pure append: rebuilding the whole function (the
    previous behaviour) also rewrote its docstring, so every append read as a
    rewrite of the table and MAK could not merge two of them. The edit is still
    deterministic, so it is the *same* registry edit for both runners — what
    differs is only *when* it is applied (under MAK's locks vs in parallel
    worktrees that must be merged).
    """
    lines = current_source.splitlines(keepends=True)
    existing = [line.strip() for line in lines if line.strip().startswith("register(")]
    if register_line.strip() in existing:
        return current_source
    new_line = f"    {register_line.strip()}\n"
    stub = [
        i for i, line in enumerate(lines)
        if line.strip() == "raise NotImplementedError"
    ]
    if stub:
        lines[stub[0]] = new_line
        return "".join(lines)
    registered = [
        i for i, line in enumerate(lines) if line.strip().startswith("register(")
    ]
    returns = [i for i, line in enumerate(lines) if line.strip().startswith("return ")]
    if registered:
        at = registered[-1] + 1
    elif returns:
        at = returns[-1]
    else:
        at = len(lines)
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.insert(at, new_line)
    return "".join(lines)


def registration_source(lines: list[str], *, local_table: bool = False) -> str:
    """Build shared wiring, preserving Template 4's local, stateless table."""
    if local_table:
        return (
            "def _register_all() -> dict[str, object]:\n"
            '    """Register feature handlers in a local table."""\n'
            "    entries: dict[str, object] = {}\n"
            "    register = entries.__setitem__\n"
            + "".join(f"    {line.strip()}\n" for line in lines)
            + "    return entries\n"
        )
    body = "\n".join(lines) if lines else "    pass"
    return f'def _register_all() -> None:\n    """Register every operation."""\n{body}\n'
