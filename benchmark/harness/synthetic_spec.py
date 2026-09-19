"""Single source of truth for generated scaling workloads and their oracles."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from pathlib import Path

from harness.workload import Operation, Registration, Workload, registration_source


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    """Parameters controlling one deterministic synthetic project."""

    modules: int
    tasks: int
    shared_tables: int
    registration_probabilities: tuple[float, float, float] = (0.2, 0.6, 0.2)
    popularity: str = "uniform"
    zipf_exponent: float = 1.2
    contention_kinds: tuple[str, ...] = ("registry_append",)
    assignment_policy: str = "round_robin"
    seed: int = 1
    profile_path: Path | None = None

    def validate(self) -> None:
        """Reject parameter sets that cannot form a meaningful workload."""
        if min(self.modules, self.tasks, self.shared_tables) < 1:
            raise ValueError("modules, tasks, and shared_tables must all be positive")
        if len(self.registration_probabilities) != 3:
            raise ValueError("registration_probabilities must contain p0, p1, p2")
        if abs(sum(self.registration_probabilities) - 1.0) > 1e-6:
            raise ValueError("registration probabilities must sum to 1")
        if self.popularity not in {"uniform", "zipf"}:
            raise ValueError(f"unknown table popularity: {self.popularity}")
        known = {
            "registry_append",
            "same_node",
            "same_file",
            "dependency_pair",
        }
        unknown = set(self.contention_kinds) - known
        if unknown:
            raise ValueError(f"unknown contention kinds: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class GeneratedWorkload:
    """Generated workload plus its requested assignment policy."""

    workload: Workload
    assignment_policy: str
    seed: int


def build_workload(spec: SyntheticSpec) -> GeneratedWorkload:
    """Create deterministic operations, references, registrations, and dependencies."""
    spec.validate()
    rng = random.Random(spec.seed)
    table_weights = _table_weights(spec)
    operations: list[Operation] = []
    for index in range(spec.tasks):
        name = f"task_{index:04d}"
        module = f"module_{index % spec.modules:03d}"
        count = rng.choices((0, 1, 2), weights=spec.registration_probabilities, k=1)[0]
        count = min(count, spec.shared_tables)
        table_indexes = _weighted_sample_without_replacement(rng, table_weights, count)
        registrations = tuple(
            Registration(
                module=f"table_{table_index:03d}",
                line=f'    register("{name}", {module}.{name})',
            )
            for table_index in table_indexes
        )
        dependency = (
            (f"task_{index - 1:04d}",)
            if "dependency_pair" in spec.contention_kinds and index % 2 == 1
            else ()
        )
        operations.append(
            Operation(
                name=name,
                module=module,
                func=name,
                reference=(
                    f"def {name}(value: int) -> int:\n    return value + {index + 1}\n"
                ),
                package="synthetic",
                registrations=registrations,
                depends_on=dependency,
                commutative_registrations="same_node" not in spec.contention_kinds,
            )
        )
    workload = Workload(
        name=f"synthetic-{spec.seed}-{spec.tasks}",
        template="generated",
        label="Simulated agent scaling 1",
        blurb="Generated zero-LLM scaling workload",
        operations=operations,
        modules=[f"module_{index:03d}" for index in range(spec.modules)],
        expected_tests=spec.tasks + 1,
        package="synthetic",
        shared_modules=tuple(
            f"table_{index:03d}" for index in range(spec.shared_tables)
        ),
    )
    return GeneratedWorkload(workload, spec.assignment_policy, spec.seed)


def write_project(destination: Path, generated: GeneratedWorkload) -> None:
    """Materialize stubs and behavior/registration oracle tests from one workload."""
    workload = generated.workload
    package = destination / workload.package
    tests = destination / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""Generated scaling target."""\n')
    by_module = {
        module: [op for op in workload.operations if op.module == module]
        for module in workload.modules
    }
    for module, operations in by_module.items():
        source = ['"""Generated task stubs."""', ""]
        for operation in operations:
            source.extend(
                [
                    f"def {operation.func}(value: int) -> int:",
                    '    """Return value plus this task\'s stable offset."""',
                    "    raise NotImplementedError",
                    "",
                    "",
                ]
            )
        (package / f"{module}.py").write_text("\n".join(source).rstrip() + "\n")
    imports = "\n".join(f"from . import {module}" for module in workload.modules)
    for table in workload.shared_modules:
        (package / f"{table}.py").write_text(
            '"""Generated shared registration table."""\n\n'
            + imports
            + "\n\n"
            + registration_source([], local_table=True)
        )
    (tests / "test_generated.py").write_text(_render_tests(workload))


def assign_operations(
    generated: GeneratedWorkload, num_agents: int, *, policy: str | None = None
) -> list[int]:
    """Assign operations under each baseline policy."""
    if num_agents < 1:
        raise ValueError("num_agents must be positive")
    workload = generated.workload
    selected = policy or generated.assignment_policy
    rng = random.Random(generated.seed)
    if selected == "module_owned":
        module_owners = {
            module: index % num_agents for index, module in enumerate(workload.modules)
        }
        return [module_owners[operation.module] for operation in workload.operations]
    if selected == "round_robin":
        return [index % num_agents for index in range(len(workload.operations))]
    if selected == "random":
        return [rng.randrange(num_agents) for _operation in workload.operations]
    if selected == "conflict_avoiding":
        parent = {table: table for table in workload.shared_modules}

        def _find(table: str) -> str:
            while parent[table] != table:
                parent[table] = parent[parent[table]]
                table = parent[table]
            return table

        def _union(left: str, right: str) -> None:
            left_root = _find(left)
            right_root = _find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for operation in workload.operations:
            tables = [registration.module for registration in operation.registrations]
            for table in tables[1:]:
                _union(tables[0], table)
        component_owner: dict[str, int] = {}
        assignments: list[int] = []
        for index, operation in enumerate(workload.operations):
            tables = [registration.module for registration in operation.registrations]
            if not tables:
                assignments.append(index % num_agents)
                continue
            component = _find(tables[0])
            owner = component_owner.setdefault(
                component, len(component_owner) % num_agents
            )
            assignments.append(owner)
        return assignments
    if selected == "from_profile":
        if generated.seed is None:
            raise ValueError("from_profile requires a stable seed")
        return _profile_assignment(generated, num_agents)
    raise ValueError(f"unknown assignment policy: {selected}")


def from_profile(spec: SyntheticSpec, profile_path: Path) -> SyntheticSpec:
    """Apply Wave 22 distributions when a mined repository profile is available."""
    data = json.loads(profile_path.read_text())
    raw_probabilities = data.get(
        "registrations_per_task", spec.registration_probabilities
    )
    probabilities = tuple(float(value) for value in raw_probabilities)
    if len(probabilities) != 3:
        raise ValueError("profile registrations_per_task must contain p0, p1, p2")
    return replace(
        spec,
        popularity="zipf",
        zipf_exponent=float(data.get("zipf_exponent", spec.zipf_exponent)),
        registration_probabilities=(
            probabilities[0],
            probabilities[1],
            probabilities[2],
        ),
        assignment_policy="from_profile",
        profile_path=profile_path,
    )


def _table_weights(spec: SyntheticSpec) -> list[float]:
    if spec.popularity == "uniform":
        return [1.0] * spec.shared_tables
    return [
        1.0 / ((index + 1) ** spec.zipf_exponent) for index in range(spec.shared_tables)
    ]


def _weighted_sample_without_replacement(
    rng: random.Random, weights: list[float], count: int
) -> list[int]:
    remaining = list(range(len(weights)))
    chosen: list[int] = []
    for _ in range(count):
        selected = rng.choices(remaining, weights=[weights[i] for i in remaining], k=1)[
            0
        ]
        chosen.append(selected)
        remaining.remove(selected)
    return chosen


def _render_tests(workload: Workload) -> str:
    lines = ['"""Generated behavior and registration oracle."""', ""]
    for operation in workload.operations:
        lines.extend(
            [
                f"from synthetic import {operation.module}",
            ]
        )
    for table in workload.shared_modules:
        lines.append(f"from synthetic import {table}")
    lines.append("")
    for index, operation in enumerate(workload.operations):
        lines.extend(
            [
                f"def test_{operation.name}() -> None:",
                f"    assert {operation.module}.{operation.func}(10) == {index + 11}",
                "",
            ]
        )
    lines.extend(
        [
            "def test_all_registrations_survive() -> None:",
            "    actual: set[str] = set()",
        ]
    )
    for table in workload.shared_modules:
        lines.append(f"    actual.update({table}._register_all())")
    expected = {
        operation.name for operation in workload.operations if operation.registrations
    }
    lines.append(f"    assert actual == {expected!r}")
    return "\n".join(lines) + "\n"


def _profile_assignment(generated: GeneratedWorkload, num_agents: int) -> list[int]:
    path = generated.workload.template
    # The operation order already reflects the profile's sampled locality. A
    # stable random assignment preserves its empirical touch distribution.
    rng = random.Random(f"{generated.seed}:{path}")
    return [rng.randrange(num_agents) for _ in generated.workload.operations]
