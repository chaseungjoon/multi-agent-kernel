"""The golden event logs reproduce: the kernel's decisions have not moved.

Runs every scenario of :mod:`tests.golden.golden` except the benchmark
templates, whose accuracy measurement runs each project's own suite and is
checked by ``python -m tests.golden.golden compare`` instead. After an
intended behaviour change, re-record with ``python -m tests.golden.golden
record`` and review the data diff like any other change.
"""

from __future__ import annotations

import pytest

from tests.golden.golden import Scenario, compare, scenarios

_IN_SUITE = [s for s in scenarios() if not s.name.startswith("benchmark/")]


@pytest.mark.parametrize("scenario", _IN_SUITE, ids=lambda s: s.name)
def test_scenario_reproduces_its_golden(scenario: Scenario) -> None:
    problems = compare([scenario])
    assert problems == [], problems[0] if problems else ""
