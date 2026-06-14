"""Tests for mak.conflict_detector.detector orchestration."""

from __future__ import annotations

from mak.conflict_detector.detector import (
    ConflictDetector,
    ConflictReport,
    EditRound,
)


class TestConflictReport:
    def test_empty_report_is_ok(self) -> None:
        report = ConflictReport()
        assert report.ok
        assert report.reasons == []


class TestDetect:
    def test_clean_round_passes(self) -> None:
        edits = EditRound(
            definitions={"m.py::function::f": "def f(a, b): pass"},
            callers={"m.py::function::g": "def g():\n    return f(1, 2)\n"},
            header_edits={"agent_a": "import os"},
            symbol_edits={"agent_a": "def f(a, b): pass"},
        )
        report = ConflictDetector().detect(edits)
        assert report.ok, report.reasons

    def test_signature_conflict_detected(self) -> None:
        edits = EditRound(
            definitions={"m.py::function::f": "def f(a, b, c): pass"},
            callers={"m.py::function::g": "def g():\n    return f(1)\n"},
        )
        report = ConflictDetector().detect(edits)
        assert not report.ok
        assert report.by_check("signature")

    def test_import_conflict_detected(self) -> None:
        edits = EditRound(
            header_edits={
                "agent_a": "from a import config",
                "agent_b": "from b import config",
            }
        )
        report = ConflictDetector().detect(edits)
        assert not report.ok
        assert report.by_check("import")

    def test_name_collision_detected(self) -> None:
        edits = EditRound(
            symbol_edits={
                "agent_a": "def handler(): pass",
                "agent_b": "def handler(): pass",
            }
        )
        report = ConflictDetector().detect(edits)
        assert not report.ok
        assert report.by_check("name_collision")

    def test_syntax_error_gates_other_checks(self) -> None:
        # A parse failure short-circuits the structural checks (they would be
        # meaningless on unparseable source) and is reported on its own.
        edits = EditRound(
            definitions={"m.py::function::f": "def f(a, b: pass"},
            callers={"m.py::function::g": "def g():\n    return f(1)\n"},
        )
        report = ConflictDetector().detect(edits)
        assert not report.ok
        assert report.by_check("syntax")
        assert not report.by_check("signature")

    def test_non_python_target_gets_actionable_message(self) -> None:
        # A node whose file is not .py can never be valid Python; say so plainly
        # instead of surfacing a bare tokenizer "invalid character".
        edits = EditRound(definitions={"docs/design.md": "# Title\n├── tree\n"})
        report = ConflictDetector().detect(edits)
        assert not report.ok
        (reason,) = report.by_check("syntax")
        assert "not a Python (.py) file" in reason.message
        assert "docs/design.md" in reason.message

    def test_python_node_with_prose_blames_the_agent(self) -> None:
        # A .py node that still won't parse usually means the agent returned prose.
        edits = EditRound(definitions={"app/main.py::function::f": "Here is the code:"})
        report = ConflictDetector().detect(edits)
        assert not report.ok
        (reason,) = report.by_check("syntax")
        assert "not valid Python" in reason.message
        assert "prose or" in reason.message

    def test_multiple_conflicts_aggregated(self) -> None:
        edits = EditRound(
            definitions={"m.py::function::f": "def f(a, b): pass"},
            callers={"m.py::function::g": "def g():\n    return f()\n"},
            header_edits={
                "agent_a": "from a import x",
                "agent_b": "from b import x",
            },
            symbol_edits={
                "agent_a": "def dup(): pass",
                "agent_b": "def dup(): pass",
            },
        )
        report = ConflictDetector().detect(edits)
        kinds = {c.check for c in report.conflicts}
        assert {"signature", "import", "name_collision"} <= kinds

    def test_empty_round_is_ok(self) -> None:
        assert ConflictDetector().detect(EditRound()).ok

    def test_caller_against_merged_definitions(self) -> None:
        # A caller can reference a function defined in a different node; the
        # detector merges all definition sources before checking.
        edits = EditRound(
            definitions={
                "m.py::function::f": "def f(a): pass",
                "m.py::function::h": "def h(a, b): pass",
            },
            callers={"m.py::function::g": "def g():\n    return h(1)\n"},
        )
        report = ConflictDetector().detect(edits)
        assert report.by_check("signature")
