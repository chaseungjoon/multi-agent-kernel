"""Wave 20. 1: the seeded semantic-conflict corpus and its acceptance table."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

from semantic.evaluate import (  # noqa: E402
    ScenarioResult,
    evaluate,
    single_states,
    validate,
)
from semantic.scenarios import SCENARIOS, Scenario  # noqa: E402


@pytest.fixture(scope="module")
def results() -> dict[int, ScenarioResult]:
    return {s.shape: evaluate(s) for s in SCENARIOS}


def test_every_shape_has_a_scenario() -> None:
    # Shapes 1-9 of TASKS.md Wave 20; shape 10 (out-of-store artifacts) is not
    # representable as nodes and is reported as such in the table.
    assert sorted(s.shape for s in SCENARIOS) == list(range(1, 10))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: f"shape{s.shape}")
def test_scenario_is_a_valid_test_case(scenario: Scenario) -> None:
    # The corpus contract: the oracle passes on the base project, with A alone
    # and with B alone, and fails on the uncoordinated combination.
    a_only, b_only = single_states(scenario)
    assert validate(scenario, a_only, b_only) == []


@pytest.mark.parametrize("shape", [s.shape for s in SCENARIOS])
def test_mak_prevents_or_detects_every_shape(
    shape: int, results: dict[int, ScenarioResult]
) -> None:
    result = results[shape]
    outcome = result.mak.outcome
    if result.scenario.needs:
        # Shapes the static kernel cannot see (a behaviour change behind an
        # unchanged signature) are detected only with their gate enabled.
        assert outcome == "missed"
        assert result.mak_gated is not None
        assert result.mak_gated.outcome.startswith("detected")
    else:
        assert outcome in (
            "prevented", "detected (plan)", "detected (commit)", "detected (wave end)"
        ), result.mak.mechanisms


def test_no_false_positives_on_single_edit_runs(
    results: dict[int, ScenarioResult]
) -> None:
    # Running either edit on its own must produce no rejection, no stale-read
    # re-dispatch and no fix-up task: a check that fires on a lone correct edit
    # costs a whole task for nothing.
    assert {s: r.false_positives for s, r in results.items()} == dict.fromkeys(
        results, 0
    )


def test_commit_time_detection_resolves_within_the_wave(
    results: dict[int, ScenarioResult]
) -> None:
    # A conflict caught at commit is re-dispatched and fixed by the same wave;
    # one caught at wave end costs a fix-up wave. Both are reported, and the
    # difference is exactly what the extra-calls column measures.
    for result in results.values():
        if result.mak.outcome == "detected (commit)":
            assert result.mak.resolved, result.scenario.name
            assert result.extra_calls >= 1


def test_worktree_baseline_is_reported_next_to_mak(
    results: dict[int, ScenarioResult]
) -> None:
    outcomes = {s: r.worktree.outcome for s, r in results.items()}
    # The shapes that only move disjoint text merge cleanly and silently.
    assert outcomes[1] == outcomes[2] == outcomes[4] == "missed"
    # The two shared-table shapes collide textually — the one place a worktree
    # workflow does see the conflict.
    assert outcomes[6].startswith("detected") and outcomes[7].startswith("detected")


def test_extra_calls_stay_bounded(results: dict[int, ScenarioResult]) -> None:
    # Detection must not cost a re-dispatch storm: at most one extra agent call
    # per scenario across the corpus.
    assert all(0 <= r.extra_calls <= 1 for r in results.values())
