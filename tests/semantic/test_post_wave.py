"""Wave 20 post-wave analysis: deletions, graph cascade, new checks, R2."""

from __future__ import annotations

from pathlib import Path

from mak.core.logging import EventType
from mak.core.types import NodeId
from tests.semantic.helpers import ScriptRunner, events, make_session, task


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _run(tmp_path: Path, files: dict[str, str],
         script: dict[str, list[dict[str, object]]],
         plan: list[object], **kw: object) -> tuple[object, object]:
    _write(tmp_path, files)
    runner = ScriptRunner(script)  # type: ignore[arg-type]
    session, _, logger = make_session(tmp_path, runner, **kw)  # type: ignore[arg-type]
    session.initialize()
    session.install_plan(plan)  # type: ignore[arg-type]
    assert session.run().ok
    return session, logger


class TestDeletionTracking:
    def test_whole_file_commit_records_superseded_fragments(
        self, tmp_path: Path
    ) -> None:
        session, _ = _run(
            tmp_path,
            {"m.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n"},
            {"t": [{"m.py": "def a():\n    return 1\n"}]},
            [task("t", ["m.py"])],
        )
        entry = session.wave.committed[NodeId("m.py::function::b")]
        assert entry == ("def b():\n    return 2\n", None)


class TestGraphCascade:
    def test_same_file_caller_is_included(self, tmp_path: Path) -> None:
        session, _ = _run(
            tmp_path,
            {"m.py": "def f(x):\n    return x\n\n\ndef g():\n    return f(1)\n"},
            {"t": [{"m.py::function::f": "def f(x, y):\n    return x + y\n"}]},
            [task("t", ["m.py::function::f"])],
        )
        tasks = session.detect_cascade_tasks()  # type: ignore[attr-defined]
        assert [t.target_nodes for t in tasks] == [[NodeId("m.py::function::g")]]
        assert "signature changed from `def f(x)` to `def f(x, y)`" in (
            tasks[0].description
        )

    def test_compatible_callers_are_left_alone(self, tmp_path: Path) -> None:
        session, _ = _run(
            tmp_path,
            {"m.py": "def f(x):\n    return x\n\n\ndef g():\n    return f(1)\n"},
            {"t": [{"m.py::function::f": "def f(x, y=0):\n    return x + y\n"}]},
            [task("t", ["m.py::function::f"])],
        )
        assert session.detect_cascade_tasks() == []  # type: ignore[attr-defined]

    def test_deleted_symbol_callers_get_a_rename_hint(self, tmp_path: Path) -> None:
        session, _ = _run(
            tmp_path,
            {
                "helpers.py": "def slugify(t):\n    return t.lower()\n",
                "old.py":
                    "from helpers import slugify\n\n\n"
                    "def use(t):\n    return slugify(t)\n",
            },
            {"t": [{"helpers.py::function::slugify":
                    "def make_slug(t):\n    return t.lower()\n"}]},
            [task("t", ["helpers.py::function::slugify"])],
        )
        tasks = session.detect_cascade_tasks()  # type: ignore[attr-defined]
        described = " ".join(t.description for t in tasks)
        assert "was deleted this wave" in described
        assert "renamed to `make_slug`" in described
        # The broken from-import is also a cross-module defect: one task, not two.
        assert len([t for t in tasks if "old.py" in str(t.target_nodes)]) == 1

    def test_idempotent_and_logged_once(self, tmp_path: Path) -> None:
        session, logger = _run(
            tmp_path,
            {"a.py": "def f(x):\n    return x\n",
             "b.py": "from a import f\n\n\ndef g():\n    return f(1)\n"},
            {"t": [{"a.py::function::f": "def f(x, y):\n    return x\n"}]},
            [task("t", ["a.py::function::f"])],
        )
        first = session.detect_cascade_tasks()  # type: ignore[attr-defined]
        second = session.detect_cascade_tasks()  # type: ignore[attr-defined]
        assert first == second and first
        conflicts = events(logger, EventType.CONFLICT_DETECTED)  # type: ignore[arg-type]
        # b.py was not touched, so this is cascade work, logged exactly once.
        assert [e.payload["kind"] for e in conflicts] == ["cascade"]


class TestNewChecksInASession:
    def test_attribute_on_a_renamed_module_function(self, tmp_path: Path) -> None:
        session, _ = _run(
            tmp_path,
            {"helpers.py": "def slugify(t):\n    return t\n"},
            {
                "a": [{
                    "helpers.py::function::slugify":
                        "def make_slug(t):\n    return t\n",
                }],
                "b": [{"report.py": "import helpers\n\n\ndef build(t):\n"
                                    "    return helpers.slugify(t)\n"}],
            },
            [task("a", ["helpers.py::function::slugify"]), task("b", ["report.py"])],
        )
        kinds = [d.kind for d in session.detect_cross_module_defects()]  # type: ignore[attr-defined]
        assert "unresolved_attribute" in kinds

    def test_constructor_with_a_new_required_field(self, tmp_path: Path) -> None:
        order = (
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\nclass Order:\n    id: int\n    total: int\n"
        )
        session, _ = _run(
            tmp_path,
            {"orders.py": order},
            {
                "a": [{"orders.py": order + "    currency: str\n"}],
                "b": [{"shop.py": "from orders import Order\n\n\n"
                                  "def make():\n    return Order(1, 100)\n"}],
            },
            [task("a", ["orders.py"]), task("b", ["shop.py"])],
        )
        kinds = [d.kind for d in session.detect_cross_module_defects()]  # type: ignore[attr-defined]
        assert kinds == ["constructor_mismatch"]

    def test_new_import_cycle_and_duplicate_are_reported(self, tmp_path: Path) -> None:
        norm = "def _norm(e):\n    return e.strip().lower()\n"
        session, _ = _run(
            tmp_path,
            {"a.py": "X = 1\n", "b.py": "Y = 2\n"},
            {
                "a": [{"a.py": "from b import Y\n\nX = 1\n\n\n" + norm}],
                "b": [{"b.py": "from a import X\n\nY = 2\n\n\n" + norm}],
            },
            [task("a", ["a.py"]), task("b", ["b.py"])],
        )
        kinds = sorted(d.kind for d in session.detect_cross_module_defects())  # type: ignore[attr-defined]
        assert kinds == ["duplicate_implementation", "import_cycle"]

    def test_pre_existing_defects_are_not_this_waves(self, tmp_path: Path) -> None:
        session, _ = _run(
            tmp_path,
            {"a.py": "def f():\n    return 1\n",
             "b.py": "from a import missing\n\n\ndef g():\n    return 1\n"},
            {"t": [{"b.py::function::g": "def g():\n    return 2\n"}]},
            [task("t", ["b.py::function::g"])],
        )
        assert session.detect_cross_module_defects() == []  # type: ignore[attr-defined]


class TestPairContext:
    def test_fix_up_names_both_tasks_and_carries_both_diffs(
        self, tmp_path: Path
    ) -> None:
        session, _ = _run(
            tmp_path,
            {"alpha.py": "def f(a):\n    return a\n"},
            {
                "a": [{"alpha.py": "def f(a, b):\n    return a + b\n"}],
                "b": [{
                    "beta.py":
                        "from alpha import f\n\n\ndef g():\n    return f(1)\n",
                }],
            },
            [task("a", ["alpha.py"]), task("b", ["beta.py"])],
        )
        tasks = session.detect_cascade_tasks()  # type: ignore[attr-defined]
        fix = next(t for t in tasks if NodeId("beta.py") in t.target_nodes)
        assert "changed this wave by task(s) a" in fix.description
        assert "changed this wave by task(s) b" in fix.description
        assert "+def f(a, b):" in fix.description
