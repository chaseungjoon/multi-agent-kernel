"""Smoke test: the offline planner-input benchmark runs (Wave 7, 7.11)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_TOOL = Path(__file__).resolve().parents[1] / "benchmark" / "tools" / "planner_input.py"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("planner_input", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["planner_input"] = module
    spec.loader.exec_module(module)
    return module


def test_synthetic_10_measures_both_strategies() -> None:
    tool = _tool()
    rows = tool.run({"synthetic-10"})
    assert [(r.requested, r.chosen) for r in rows] == [
        ("oneshot", "oneshot"), ("auto", "full"),
    ]
    for row in rows:
        assert row.files == 10
        assert row.first_prompt_tokens > row.first_inventory_tokens > 0
        assert row.max_inventory_tokens <= 12_000
    table = tool.markdown(rows)
    assert "| synthetic-10 | 10 |" in table
    assert "auto → full" in table
