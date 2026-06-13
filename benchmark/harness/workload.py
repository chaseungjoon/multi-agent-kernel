"""The benchmark workloads: operations, reference implementations, and assignment.

A :class:`Workload` bundles everything project-specific — the template directory,
the operation list, the modules, and the expected test count — so the runners and
the mock backend stay a single source of truth and the two projects can never drift
apart. Two workloads ship:

- ``basic`` — the original 9-operation ``toolkit`` (3 modules).
- ``pro`` — a larger, harder 17-operation ``toolkit`` (4 modules: text processing,
  math, sequences, conversions), several of them real algorithms.

Both share the *same* contention shape: one shared dispatch function,
``registry._register_all``, that every operation must add one line to.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.template2_spec import OPS as _T2_SPEC
from harness.template2_spec import expected_tests as _t2_tests
from harness.template2_spec import modules as _t2_modules

REGISTRY_NODE = "toolkit/registry.py::function::_register_all"


@dataclass(frozen=True)
class Operation:
    """One unit of work: implement a function and register it in the dispatch table."""

    name: str  # the name it is registered under
    module: str  # the toolkit module the function lives in
    func: str  # the function name (== the registry key here)
    reference: str  # a correct implementation, used by the mock backend

    @property
    def func_node(self) -> str:
        """The MAK node id of the function this operation implements."""
        return f"toolkit/{self.module}.py::function::{self.func}"

    @property
    def register_line(self) -> str:
        """The single line this operation must add to ``_register_all``."""
        return f'    register("{self.name}", {self.module}.{self.func})'


# -- basic workload (3 modules, 9 operations) -------------------------------

_BASIC_OPERATIONS: list[Operation] = [
    Operation("upper", "strings", "upper", "def upper(s):\n    return s.upper()\n"),
    Operation("reverse", "strings", "reverse", "def reverse(s):\n    return s[::-1]\n"),
    Operation(
        "count_vowels",
        "strings",
        "count_vowels",
        'def count_vowels(s):\n    return sum(1 for c in s if c.lower() in "aeiou")\n',
    ),
    Operation("add", "numbers", "add", "def add(a, b):\n    return a + b\n"),
    Operation(
        "factorial",
        "numbers",
        "factorial",
        "def factorial(n):\n"
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
        "def is_prime(n):\n"
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
        "def unique(items):\n"
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
        "def maximum(items):\n"
        "    if not items:\n"
        '        raise ValueError("empty sequence")\n'
        "    return max(items)\n",
    ),
    Operation(
        "first",
        "sequences",
        "first",
        "def first(items):\n"
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


@dataclass(frozen=True)
class Workload:
    """A complete benchmark target: its template, operations, and oracle size."""

    name: str  # "basic" | "2"
    template: str  # template directory name under benchmark/
    label: str  # human label for reports
    blurb: str  # one-line description for reports
    operations: list[Operation]
    modules: list[str]
    expected_tests: int


WORKLOADS: dict[str, Workload] = {
    "basic": Workload(
        name="basic",
        template="project_template",
        label="Basic toolkit (9 ops)",
        blurb="9 operations across 3 modules (strings, numbers, sequences) + 1 shared "
        "registry function; 30 tests as the accuracy oracle.",
        operations=_BASIC_OPERATIONS,
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
        operations=_TEMPLATE2_OPERATIONS,
        modules=_t2_modules(),
        expected_tests=_t2_tests(),
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
    source files are never edited by two agents at once — so the only place the
    worktree branches collide at merge time is the shared ``_register_all``
    function. That isolates the conflict to exactly the contended node, which is
    the comparison we want to make.
    """
    module_agent = {module: i % num_agents for i, module in enumerate(workload.modules)}
    return [module_agent[op.module] for op in workload.operations]


def add_registration(current_source: str, register_line: str) -> str:
    """Return ``_register_all`` source with ``register_line`` added (idempotent).

    Rebuilds the function from the set of ``register(...)`` lines it already
    contains plus the new one, dropping the ``raise NotImplementedError`` stub. The
    rebuild is deterministic, so it is the *same* registry edit for both runners —
    what differs is only *when* it is applied (serialized under MAK vs in parallel
    worktrees that must be merged).
    """
    header = "def _register_all() -> None:"
    doc = '    """Register every operation."""'
    existing = [
        line for line in current_source.splitlines() if line.strip().startswith("register(")
    ]
    if register_line.strip() not in {line.strip() for line in existing}:
        existing.append(register_line)
    body = "\n".join(existing) if existing else "    pass"
    return f"{header}\n{doc}\n{body}\n"
