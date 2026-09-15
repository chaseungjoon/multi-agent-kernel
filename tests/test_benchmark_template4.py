"""Template 4 contracts, planner isolation, fairness, and report integration."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from shutil import copytree

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

import run_benchmark as cli  # noqa: E402
from harness.agents import AgentSpec, RealBackend, Usage, make_backends  # noqa: E402
from harness.mak_runner import run_mak  # noqa: E402
from harness.metrics import RunResult  # noqa: E402
from harness.planner import (  # noqa: E402
    DEFAULT_PLANNER,
    BenchmarkPlan,
    apply_plan,
    make_plan,
    parse_plan,
    planner_prompt,
)
from harness.report import RunMeta, render_stats  # noqa: E402
from harness.template4_spec import TASKS, expected_tests  # noqa: E402
from harness.traditional import run_traditional  # noqa: E402
from harness.workload import WORKLOADS, Workload, assign  # noqa: E402
from tools.gen_template4 import generate  # noqa: E402


def _response() -> str:
    return json.dumps(
        {
            "modules": [
                {
                    "module": module,
                    "worker": i % 3,
                    "guidance": f"Check {module} edge cases.",
                }
                for i, module in enumerate(WORKLOADS["4"].modules)
            ]
        }
    )


def test_generator_matches_checked_in_fixture(tmp_path: Path) -> None:
    generate(tmp_path)
    fixture = BENCH / "project_template_4"
    generated = sorted(
        path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()
    )
    checked_in = sorted(
        path.relative_to(fixture)
        for path in fixture.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert generated == checked_in
    for path in generated:
        assert (tmp_path / path).read_text() == (fixture / path).read_text(), path
    for task in TASKS:
        compile(task.reference, f"{task.module}.{task.name}", "exec")


def test_baseline_collects_every_check_and_passes_none(tmp_path: Path) -> None:
    generate(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{expected_tests()} failed" in result.stdout
    assert "passed" not in result.stdout
    assert "error during collection" not in result.stdout


@pytest.mark.parametrize("runner", [run_mak, run_traditional])
@pytest.mark.parametrize("project", ["basic", "2", "3", "4"])
def test_mock_runners_satisfy_oracle(
    tmp_path: Path,
    project: str,
    runner: object,
) -> None:
    workload = WORKLOADS[project]
    project_dir = tmp_path / "project"
    copytree(BENCH / workload.template, project_dir)
    specs = cli._parse_specs(None, 3, project)
    assignment = assign(workload, 3)
    if project == "4":
        plan = make_plan(workload, project_dir, 3, DEFAULT_PLANNER, mock=True)
        workload, assignment = apply_plan(workload, plan)
    result = runner(
        project_dir,
        tmp_path / "state",
        make_backends(specs, mock=True),
        assignment,
        workload,
    )
    assert result.passed == result.total == workload.expected_tests, result.notes
    assert not result.notes
    assert result.usage.calls >= len(workload.operations)
    if project == "4":
        assert result.conflicts == (6 if runner is run_traditional else 0)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "unknown",
        "worker",
        "boolean",
        "idle",
        "guidance",
        "extra",
        "json",
    ],
)
def test_planner_rejects_invalid_plans(kind: str) -> None:
    value = json.loads(_response())
    modules = value["modules"]
    if kind == "missing":
        modules.pop()
    elif kind == "duplicate":
        modules.append(modules[0])
    elif kind == "unknown":
        modules[0]["module"] = "made_up"
    elif kind == "worker":
        modules[0]["worker"] = 3
    elif kind == "boolean":
        modules[0]["worker"] = True
    elif kind == "idle":
        for module in modules:
            module["worker"] = 0
    elif kind == "guidance":
        modules[0]["guidance"] = " "
    elif kind == "extra":
        modules[0]["implementation"] = "not allowed"
    text = "not JSON" if kind == "json" else json.dumps(value)
    with pytest.raises(ValueError, match="planner"):
        parse_plan(text, WORKLOADS["4"], 3)


def test_planner_only_sees_contracts_and_applies_complete_ownership() -> None:
    workload = WORKLOADS["4"]
    prompt = planner_prompt(workload, BENCH / workload.template, 3)
    assert "class Job:" in prompt
    assert "raise NotImplementedError" in prompt
    assert "assert " not in prompt
    assert "test_" not in prompt
    assert "hashlib.sha256" not in prompt
    plan = BenchmarkPlan(
        "anthropic:claude-opus-5",
        parse_plan(_response(), workload, 3),
        Usage(10, 20, 1),
        2.5,
    )
    planned, assignment = apply_plan(workload, plan)
    assert set(assignment) == {0, 1, 2}
    for module in workload.modules:
        assert (
            len(
                {
                    assignment[i]
                    for i, op in enumerate(planned.operations)
                    if op.module == module
                }
            )
            == 1
        )
    for before, after in zip(workload.operations, planned.operations, strict=True):
        assert "Planner guidance:" not in before.context
        assert f"Check {after.module} edge cases." in after.context
        assert before.reference == after.reference


def test_real_planner_makes_one_call_and_records_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_plan(self: RealBackend, system: str, prompt: str) -> tuple[str, Usage]:
        assert self.model == "claude-opus-5"
        assert "Return ONLY JSON" in system
        calls.append(prompt)
        return _response(), Usage(123, 45, 1)

    monkeypatch.setattr(RealBackend, "plan", fake_plan)
    workload = WORKLOADS["4"]
    plan = make_plan(
        workload, BENCH / workload.template, 3, DEFAULT_PLANNER, mock=False
    )
    assert len(calls) == 1
    assert plan.usage == Usage(123, 45, 1)
    assert plan.wall_seconds >= 0
    assert plan.model == "anthropic:claude-opus-5"


def test_both_runners_receive_same_plan_and_equal_planning_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workloads: list[Workload] = []
    assignments: list[list[int]] = []

    def fake_runner(
        project_dir: Path,
        state: Path,
        backends: list[object],
        assignment: list[int],
        workload: Workload,
    ) -> RunResult:
        workloads.append(workload)
        assignments.append(assignment)
        return RunResult("runner", 5.0, Usage(100, 50, 24), 152, 152, 0, 0)

    monkeypatch.setattr(cli, "run_mak", fake_runner)
    monkeypatch.setattr(cli, "run_traditional", fake_runner)
    workload = WORKLOADS["4"]
    plan = BenchmarkPlan(
        "anthropic:claude-opus-5",
        parse_plan(_response(), workload, 3),
        Usage(10, 20, 1),
        2.5,
    )
    results = cli._one_pass(
        workload, cli._parse_specs(None, 3, "4"), "mock", tmp_path, plan
    )
    assert workloads[0] is workloads[1]
    assert assignments[0] == assignments[1]
    for result in results:
        assert result.usage == Usage(110, 70, 25)
        assert result.planning_usage == plan.usage
        assert result.wall_seconds == 7.5
        assert result.planning_seconds == 2.5
    assert cli._aggregate(list(results)).planning_usage == plan.usage


def test_plan_persistence_and_template4_report_preserve_previous_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "BENCH", tmp_path)
    monkeypatch.setattr(cli, "_LEGACY_RUN", tmp_path / "absent.json")
    for project in ("basic", "2", "3", "4"):
        meta = RunMeta(
            "mock",
            3,
            ["claude-opus-5"] * 3,
            "test",
            24,
            152,
            project=project,
            label=f"Template {project}",
            modules=6,
            planner_model=DEFAULT_PLANNER.model if project == "4" else "",
        )
        result = RunResult("runner", 5.0, Usage(100, 50, 25), 152, 152, 0, 0)
        plan = BenchmarkPlan(
            "anthropic:claude-opus-5",
            parse_plan(_response(), WORKLOADS["4"], 3),
            Usage(10, 20, 1),
            2.5,
        )
        if project == "4":
            result.planning_usage = plan.usage
            result.planning_seconds = plan.wall_seconds
        cli._save_run(
            project, result, result, meta, plans=[plan] if project == "4" else None
        )
    runs = cli._collect_runs()
    assert [run.meta.project for run in runs] == ["basic", "2", "3", "4"]
    assert runs[-1].mak.planning_usage == Usage(10, 20, 1)
    saved = json.loads((tmp_path / ".last_run.4.json").read_text())
    assert saved["plans"][0] == json.loads(json.dumps(dataclasses.asdict(plan)))
    report = render_stats(runs)
    assert "\n## Template 4\n" in report
    assert "Planner tokens (included above)" in report
    assert "## Template 3" in report


def test_template4_defaults_and_explicit_overrides() -> None:
    defaults = cli._parse_specs(None, 3, "4")
    assert len(defaults) == 3
    assert len({spec.name for spec in defaults}) == 3
    assert all(
        spec.provider == "anthropic" and spec.model == "claude-opus-5"
        for spec in defaults
    )
    assert DEFAULT_PLANNER == AgentSpec("planner", "anthropic", "claude-opus-5")
    assert cli._parse_specs(["openai:custom"], 3, "4")[0].model == "custom"


def test_cli_repeat_persists_plans_and_updates_both_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copytree(BENCH / "project_template_4", tmp_path / "project_template_4")
    (tmp_path / "README.md").write_text(
        "# Benchmark\n\n<!-- RESULTS:START -->\n<!-- RESULTS:END -->\n"
    )
    monkeypatch.setattr(cli, "BENCH", tmp_path)
    monkeypatch.setattr(cli, "_LEGACY_RUN", tmp_path / "absent.json")
    assert (
        cli.main(["--mode", "mock", "--project", "4", "--repeat", "2", "--keep"]) == 0
    )
    saved = json.loads((tmp_path / ".last_run.4.json").read_text())
    assert saved["meta"]["models"] == ["claude-opus-5"] * 3
    assert saved["meta"]["planner_model"] == "anthropic:claude-opus-5"
    assert len(saved["plans"]) == len(saved["samples"]) == 2
    assert saved["mak"]["usage"]["calls"] == 25
    assert saved["trad"]["usage"]["calls"] == 31
    assert saved["mak"]["passed"] == saved["trad"]["passed"] == 152
    assert "## Template 4\n" in (tmp_path / "STATS.md").read_text()
    assert "Per-run breakdown (2 runs)" in (tmp_path / "STATS.md").read_text()
    assert "### Template 4" in (tmp_path / "README.md").read_text()
    assert (tmp_path / ".runs" / "4" / "plan-2.json").exists()


def test_planner_accepts_literal_line_breaks_inside_guidance() -> None:
    response = _response().replace(
        "Check tenancy edge cases.",
        "Check tenant boundaries.\nPreserve inputs.\tUse roles.",
    )
    plans = parse_plan("```json\n" + response + "\n```", WORKLOADS["4"], 3)
    assert plans[0].guidance == "Check tenant boundaries.\nPreserve inputs.\tUse roles."


@pytest.mark.parametrize(
    "response",
    [
        _response()[:50],
        _response().replace("Check tenancy edge cases.", "bad\x00guidance"),
        _response() + " trailing prose",
    ],
)
def test_planner_does_not_repair_incomplete_or_invalid_plans(response: str) -> None:
    with pytest.raises(ValueError, match="planner"):
        parse_plan(response, WORKLOADS["4"], 3)


def test_planner_retries_and_preserves_all_costs_and_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [_response()[:50], '{"modules": []}', _response()]
    prompts: list[str] = []

    def fake_plan(self: RealBackend, system: str, prompt: str) -> tuple[str, Usage]:
        assert self._max_tokens == 8192
        prompts.append(prompt)
        return responses[len(prompts) - 1], Usage(100, 20, 1)

    monkeypatch.setattr(RealBackend, "plan", fake_plan)
    workload = WORKLOADS["4"]
    plan = make_plan(
        workload,
        BENCH / workload.template,
        3,
        DEFAULT_PLANNER,
        mock=False,
        diagnostics_dir=tmp_path,
    )
    assert plan.usage == Usage(300, 60, 3)
    assert [attempt.response for attempt in plan.attempts] == responses
    assert len(prompts) == 3
    assert "Previous plan was rejected" in prompts[1]
    assert "exactly once" in prompts[2]
    assert "attempt 1/3 rejected" in caplog.text
    for i, attempt in enumerate(plan.attempts, 1):
        saved = json.loads((tmp_path / f"attempt-{i}.json").read_text())
        assert saved == json.loads(json.dumps(dataclasses.asdict(attempt)))
    assert plan.attempts[0].error
    assert not plan.attempts[-1].error


def test_planner_stops_after_three_rejections_and_saves_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_plan(self: RealBackend, system: str, prompt: str) -> tuple[str, Usage]:
        calls.append(prompt)
        return '{"modules": [{"guidance": "cut off', Usage(10, 20, 1)

    monkeypatch.setattr(RealBackend, "plan", fake_plan)
    workload = WORKLOADS["4"]
    with pytest.raises(ValueError, match="after 3 attempts.*Responses saved"):
        make_plan(
            workload,
            BENCH / workload.template,
            3,
            DEFAULT_PLANNER,
            mock=False,
            diagnostics_dir=tmp_path,
        )
    assert len(calls) == 3
    assert len(list(tmp_path.glob("attempt-*.json"))) == 3
    assert json.loads((tmp_path / "attempt-3.json").read_text())["usage"]["calls"] == 1


def test_anthropic_planner_has_larger_output_budget_than_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    limits: list[object] = []

    def create(**kwargs: object) -> SimpleNamespace:
        limits.append(kwargs["max_tokens"])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_response())],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(RealBackend, "_anthropic", lambda self: client)
    workload = WORKLOADS["4"]
    plan = make_plan(
        workload, BENCH / workload.template, 3, DEFAULT_PLANNER, mock=False
    )
    worker = RealBackend("worker", "anthropic", "claude-opus-5")
    worker.implement(workload.operations[0], "def stub(): pass")
    assert limits == [8192, 2048]
    assert plan.usage == Usage(100, 50, 1)
