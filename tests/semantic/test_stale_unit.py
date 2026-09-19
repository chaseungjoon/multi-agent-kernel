"""Unit tests for stale-read classification, policy and the retry note."""

from __future__ import annotations

import pytest

from mak.core.types import NodeFragment, NodeId
from mak.node_store.store import source_digest
from mak.semantic.read_set import (
    ReadMark,
    build_read_set,
    read_set_from_json,
    read_set_to_json,
)
from mak.semantic.stale import ChangeKind, Verdict, classify, decide, retry_note

X = NodeId("m.py::function::load")
OLD = "def load(uid):\n    return {'name': 'x'}\n"


def _mark(source: str | None = OLD, *, api_only: bool = False) -> ReadMark:
    return ReadMark(
        X,
        1 if source is not None else None,
        source_digest(source) if source is not None else None,
        "same_file",
        api_only,
        source,
    )


def _staged(code: str = "def show(uid):\n    return load(uid)\n") -> dict[NodeId, str]:
    return {NodeId("m.py::function::show"): code}


class TestClassify:
    def test_body_change(self) -> None:
        stale = classify(_mark(), 2, "def load(uid):\n    return {}\n", _staged())
        assert stale.kind is ChangeKind.BODY_ONLY

    def test_api_change_shape_only(self) -> None:
        stale = classify(_mark(), 2, "def load(uid, x=1):\n    return {}\n", _staged())
        assert stale.kind is ChangeKind.API_CHANGE and stale.shape_only

    def test_return_annotation_is_not_shape_only(self) -> None:
        stale = classify(_mark(), 2, "def load(uid) -> int:\n    return 1\n", _staged())
        assert stale.kind is ChangeKind.API_CHANGE and not stale.shape_only

    def test_method_parameter_change_is_not_shape_only(self) -> None:
        old = "class C:\n    def m(self, a):\n        pass\n"
        new = "class C:\n    def m(self, a, b):\n        pass\n"
        mark = ReadMark(X, 1, source_digest(old), "x", False, old)
        assert not classify(mark, 2, new, {X: "C().m(1)"}).shape_only

    def test_deleted_and_created(self) -> None:
        assert classify(_mark(), None, None, _staged()).kind is ChangeKind.DELETED
        created = classify(_mark(None), 1, OLD, _staged())
        assert created.kind is ChangeKind.CREATED

    def test_unreferenced(self) -> None:
        stale = classify(
            _mark(), 2, "def load(uid) -> int:\n    return 1\n",
            _staged("def show(uid):\n    return 1\n"),
        )
        assert not stale.referenced

    def test_substring_is_not_a_reference(self) -> None:
        stale = classify(
            _mark(), 2, "def load(uid) -> int:\n    return 1\n",
            _staged("def show(uid):\n    return download(uid)\n"),
        )
        assert not stale.referenced

    def test_an_added_import_is_not_a_reference(self) -> None:
        header = NodeId("m.py::module_header::__header__")
        old = "import os\n"
        mark = ReadMark(header, 1, source_digest(old), "same_file", False, old)
        stale = classify(mark, 2, "import os\nimport sys\n", _staged("os.getcwd()"))
        # Only an *existing* binding changing is an interface change.
        assert stale.kind is ChangeKind.BODY_ONLY and not stale.referenced

    def test_a_removed_import_is(self) -> None:
        header = NodeId("m.py::module_header::__header__")
        old = "import os\nimport sys\n"
        mark = ReadMark(header, 1, source_digest(old), "same_file", False, old)
        assert classify(mark, 2, "import os\n", _staged("sys.exit()")).referenced

    def test_unknown_old_source_counts_every_name_as_changed(self) -> None:
        # A mark restored from disk has no source: nothing proves the change
        # body-only, and every name the node binds counts as re-bound.
        mark = ReadMark(X, 1, "digest", "x", False, None)
        stale = classify(mark, 2, OLD, _staged("y = load(1)"))
        assert stale.kind is ChangeKind.API_CHANGE and stale.referenced
        assert not classify(mark, 2, OLD, _staged("x = 1")).referenced


def _decide(policy: str, new: str | None, staged: str, defects: list[str]) -> Verdict:
    stale = classify(_mark(), 2 if new else None, new, _staged(staged))
    return decide(policy, [stale], recheck=lambda s: defects).verdict


class TestPolicies:
    BODY = "def load(uid):\n    return {}\n"
    SHAPE = "def load(uid, x=1):\n    return {}\n"
    RET = "def load(uid) -> int:\n    return 1\n"
    CALL = "def show(uid):\n    return load(uid)\n"

    @pytest.mark.parametrize(
        ("policy", "new", "defects", "expected"),
        [
            ("revalidate", BODY, [], Verdict.ACCEPT),
            ("revalidate", SHAPE, [], Verdict.ACCEPT),
            ("revalidate", SHAPE, ["arity"], Verdict.REDISPATCH),
            ("revalidate", RET, [], Verdict.REDISPATCH),
            ("revalidate", None, [], Verdict.REDISPATCH),
            ("accept_if_api_stable", BODY, [], Verdict.ACCEPT),
            ("accept_if_api_stable", SHAPE, [], Verdict.REDISPATCH),
            ("redispatch", BODY, [], Verdict.REDISPATCH),
            ("reject", BODY, [], Verdict.ACCEPT),
            ("reject", RET, [], Verdict.REJECT),
        ],
    )
    def test_matrix(
        self, policy: str, new: str | None, defects: list[str], expected: Verdict
    ) -> None:
        assert _decide(policy, new, self.CALL, defects) is expected

    def test_api_only_reader_ignores_body_changes_even_strictly(self) -> None:
        stale = classify(_mark(api_only=True), 2, self.BODY, _staged())
        assert decide("redispatch", [stale], recheck=lambda s: []).verdict is (
            Verdict.ACCEPT
        )

    def test_adjudicator_can_only_accept_an_uncertain_case(self) -> None:
        stale = classify(_mark(), 2, self.RET, _staged(self.CALL))
        yes = decide(
            "revalidate", [stale], recheck=lambda s: [], adjudicate=lambda s: True
        )
        unsure = decide(
            "revalidate", [stale], recheck=lambda s: [], adjudicate=lambda s: None
        )
        assert yes.verdict is Verdict.ACCEPT
        assert unsure.verdict is Verdict.REDISPATCH

    def test_adjudicator_never_overrides_a_found_defect(self) -> None:
        stale = classify(_mark(), 2, self.SHAPE, _staged(self.CALL))
        decision = decide(
            "revalidate", [stale], recheck=lambda s: ["arity"],
            adjudicate=lambda s: True,
        )
        assert decision.verdict is Verdict.REDISPATCH

    def test_recheck_runs_once_per_commit(self) -> None:
        calls: list[int] = []
        stales = [
            classify(_mark(), 2, self.SHAPE, _staged(self.CALL)) for _ in range(3)
        ]
        decide("revalidate", stales, recheck=lambda s: calls.append(1) or [])
        assert calls == [1]

    def test_most_severe_verdict_wins(self) -> None:
        body = classify(_mark(), 2, self.BODY, _staged(self.CALL))
        ret = classify(_mark(), 2, self.RET, _staged(self.CALL))
        assert decide("reject", [body, ret], recheck=lambda s: []).verdict is (
            Verdict.REJECT
        )


class TestRetryNote:
    def test_note_carries_the_diff_and_is_bounded(self) -> None:
        stale = classify(_mark(), 2, "def load(uid) -> int:\n    return 1\n", _staged())
        decision = decide("revalidate", [stale], recheck=lambda s: [])
        note = retry_note(decision)
        assert "read (v1)" in note and "+def load(uid) -> int:" in note
        big = classify(_mark(), 2, "def load(uid) -> int:\n" + "    x = 1\n" * 5000,
                       _staged())
        long_note = retry_note(decide("revalidate", [big], recheck=lambda s: []),
                               limit=500)
        assert len(long_note) < 1000 and "diff omitted" in long_note


class TestReadSetCodec:
    def test_build_marks_each_key_and_expands_whole_files(self) -> None:
        a = NodeFragment(NodeId("a.py::function::f"), "function", "def f(): pass\n", 3)
        store = {a.node_id: a}
        read_set = build_read_set(
            {"read_source:a.py": "…", "read_api:b.py::function::g": "…"},
            {"dependency_output": ["read_source:a.py"]},
            absent=[NodeId("new.py")],
            fetch=store.get,
            expand=lambda n: [a.node_id] if str(n) == "a.py" else [],
        )
        assert read_set[a.node_id].version == 3
        assert read_set[a.node_id].layer == "dependency_output"
        assert read_set[NodeId("new.py")].digest is None
        assert NodeId("b.py::function::g") not in read_set  # no committed node

    def test_json_round_trip_drops_sources_and_tolerates_junk(self) -> None:
        marks = {X: _mark()}
        restored = read_set_from_json(read_set_to_json(marks))
        assert restored[X].digest == marks[X].digest
        assert restored[X].source is None
        assert read_set_from_json("junk") == {}
        assert read_set_from_json({"a": 3}) == {}
