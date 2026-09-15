"""One constrained, auditable plan shared by both benchmark competitors."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from harness.agents import AgentSpec, RealBackend, Usage, _strip_fence
from harness.traditional import _extract_function
from harness.workload import Workload

DEFAULT_PLANNER = AgentSpec("planner", "anthropic", "claude-opus-5")
_MAX_PLAN_ATTEMPTS = 3
_PLANNER_MAX_TOKENS = 8192

_SYSTEM = """Plan implementation of a multi-tenant background-job service by the
numbered workers given below. Assign every complete feature module to exactly one
worker, use every worker, and balance complexity and shared-table contention.
Functions are independent edits; do not introduce new dependencies, change contracts,
or propose changes outside the listed functions and their wiring. Provide concise,
concrete implementation guidance per module (edge cases and integration risks),
at most 120 words per module. Escape line breaks inside JSON strings as \\n.
Return ONLY JSON with this exact shape:
{"modules": [{"module": "name", "worker": 0, "guidance": "..."}]}
Do not return implementations. Each module must appear exactly once."""


@dataclass(frozen=True)
class ModulePlan:
    """An ownership decision and guidance delivered to that module's worker."""

    module: str
    worker: int
    guidance: str


@dataclass(frozen=True)
class PlanAttempt:
    """A provider response, its cost, and any validation error for diagnosis."""

    response: str
    usage: Usage
    error: str = ""


@dataclass(frozen=True)
class BenchmarkPlan:
    """A validated plan and its measured cost, persisted with each sample."""

    model: str
    modules: tuple[ModulePlan, ...]
    usage: Usage
    wall_seconds: float
    attempts: tuple[PlanAttempt, ...] = ()


def parse_plan(text: str, workload: Workload, workers: int) -> tuple[ModulePlan, ...]:
    """Reject omissions, duplicate ownership, invalid workers, and empty guidance."""
    try:
        # Models sometimes emit literal line breaks in guidance. Permit those
        # without trying to complete truncated strings or invent missing fields.
        value = json.loads(_strip_fence(text), strict=False)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"planner returned invalid or incomplete JSON: {exc.msg} "
            f"at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"modules"}:
        raise ValueError("planner must return exactly a modules array")
    if not isinstance(value["modules"], list):
        raise ValueError("planner modules must be an array")
    plans = tuple(_module_plan(item, workers) for item in value["modules"])
    names = [plan.module for plan in plans]
    if len(names) != len(set(names)) or set(names) != set(workload.modules):
        raise ValueError("planner must assign every feature module exactly once")
    if {plan.worker for plan in plans} != set(range(workers)):
        raise ValueError("planner must assign work to every worker")
    return plans


def _module_plan(item: object, workers: int) -> ModulePlan:
    if not isinstance(item, dict) or set(item) != {"module", "worker", "guidance"}:
        raise ValueError("each planner module requires module, worker and guidance")
    module, worker, guidance = item["module"], item["worker"], item["guidance"]
    if not isinstance(module, str):
        raise ValueError("planner module name must be a string")
    if type(worker) is not int or not 0 <= worker < workers:
        raise ValueError(f"planner worker must be an integer in 0..{workers - 1}")
    if not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 4000:
        raise ValueError("planner guidance must contain 1..4000 characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in guidance):
        raise ValueError("planner guidance contains unsupported control characters")
    return ModulePlan(module, worker, guidance.strip())


def planner_prompt(workload: Workload, template: Path, workers: int) -> str:
    """Expose public contracts and wiring, excluding references and oracle tests."""
    parts = [f"Workers: {list(range(workers))}", workload.blurb]
    if workload.operations:
        parts.append(workload.operations[0].context)
    for operation in workload.operations:
        source = (template / workload.package / f"{operation.module}.py").read_text()
        contract = _extract_function(source, operation.func)
        tables = ", ".join(reg.module for reg in operation.registrations) or "none"
        parts.append(f"Module: {operation.module}; shared tables: {tables}\n{contract}")
    return "\n\n".join(parts)


def make_plan(
    workload: Workload,
    template: Path,
    workers: int,
    spec: AgentSpec,
    *,
    mock: bool,
    diagnostics_dir: Path | None = None,
) -> BenchmarkPlan:
    """Build one validated plan, retaining all response costs across retries."""
    if not 1 <= workers <= len(workload.modules):
        raise ValueError(
            "worker count must be between one and the feature module count"
        )
    if not mock:
        return _make_real_plan(workload, template, workers, spec, diagnostics_dir)
    start = time.monotonic()
    plans = tuple(
        ModulePlan(
            module,
            i % workers,
            "Follow the public contract; preserve immutable inputs.",
        )
        for i, module in enumerate(workload.modules)
    )
    return BenchmarkPlan(
        f"{spec.provider}:{spec.model}", plans, Usage(calls=1), time.monotonic() - start
    )


def _make_real_plan(
    workload: Workload,
    template: Path,
    workers: int,
    spec: AgentSpec,
    diagnostics_dir: Path | None,
) -> BenchmarkPlan:
    start = time.monotonic()
    backend = RealBackend(
        spec.name,
        spec.provider,
        spec.model,
        max_tokens=_PLANNER_MAX_TOKENS,
    )
    prompt = planner_prompt(workload, template, workers)
    usage = Usage()
    attempts: list[PlanAttempt] = []
    correction = ""
    for number in range(1, _MAX_PLAN_ATTEMPTS + 1):
        attempt = PlanAttempt(*backend.plan(_SYSTEM, prompt + correction))
        plans, attempt = _validate_attempt(attempt, workload, workers)
        usage = usage + attempt.usage
        attempts.append(attempt)
        _save_attempt(attempt, number, diagnostics_dir)
        if plans:
            return BenchmarkPlan(
                f"{spec.provider}:{spec.model}",
                plans,
                usage,
                time.monotonic() - start,
                tuple(attempts),
            )
        correction = _retry_prompt(attempt.error, number)
    location = f" Responses saved in {diagnostics_dir}." if diagnostics_dir else ""
    raise ValueError(
        f"planner failed validation after {_MAX_PLAN_ATTEMPTS} attempts: "
        f"{attempts[-1].error}.{location}"
    )


def _validate_attempt(
    attempt: PlanAttempt,
    workload: Workload,
    workers: int,
) -> tuple[tuple[ModulePlan, ...], PlanAttempt]:
    try:
        return parse_plan(attempt.response, workload, workers), attempt
    except ValueError as exc:
        return (), replace(attempt, error=str(exc))


def _retry_prompt(error: str, number: int) -> str:
    logging.getLogger(__name__).warning(
        "Planner attempt %s/%s rejected: %s",
        number,
        _MAX_PLAN_ATTEMPTS,
        error,
    )
    return (
        f"\n\nPrevious plan was rejected: {error}. Return a complete JSON object "
        "covering every module. Keep guidance concise; escape string line breaks."
    )


def _save_attempt(attempt: PlanAttempt, number: int, directory: Path | None) -> None:
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"attempt-{number}.json").write_text(
            json.dumps(asdict(attempt), indent=2)
        )


def apply_plan(workload: Workload, plan: BenchmarkPlan) -> tuple[Workload, list[int]]:
    """Give both runners identical ownership and public implementation guidance."""
    ownership = {entry.module: entry for entry in plan.modules}
    operations = [
        replace(
            operation,
            context=operation.context
            + "\n\nPlanner guidance:\n"
            + ownership[operation.module].guidance,
        )
        for operation in workload.operations
    ]
    return replace(workload, operations=operations), [
        ownership[operation.module].worker for operation in workload.operations
    ]
