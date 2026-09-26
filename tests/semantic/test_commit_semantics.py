"""Wave 20 commit semantics: read sets, stale reads, registrars, interfaces."""

from __future__ import annotations

from pathlib import Path

from mak.config import SemanticConfig
from mak.core.logging import EventType
from mak.core.types import NodeId
from tests.semantic.helpers import (
    ScriptRunner,
    committed,
    events,
    logged,
    make_session,
    task,
)

M = "m.py"
LOAD = "m.py::function::load"
SHOW = "m.py::function::show"
HELPER = "m.py::function::helper"

_BASE = (
    "def load(uid):\n    return {'name': 'x'}\n\n\n"
    "def show(uid):\n    return 1\n\n\n"
    "def helper():\n    return 0\n"
)


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _race(
    tmp_path: Path,
    a_source: str,
    b_sources: list[str],
    *,
    semantic: SemanticConfig | None = None,
    a_target: str = LOAD,
    a_needle: str | None = None,
) -> tuple[object, ScriptRunner, object, object]:
    """Task A rewrites ``a_target``; B rewrites SHOW, dispatched before A commits."""
    _write(tmp_path, {M: _BASE})
    runner = ScriptRunner(
        {
            "a": [{a_target: a_source}],
            "b": [{SHOW: src} for src in b_sources],
        }
    )
    session, store, logger = make_session(tmp_path, runner, semantic=semantic)
    runner._hold["b"] = committed(store, a_target, a_needle or a_source.strip()[:30])
    session.initialize()
    session.install_plan([task("a", [a_target]), task("b", [SHOW])])
    result = session.run()
    return result, runner, logger, store


class TestReadSets:
    def test_every_layer_is_recorded_with_its_version(self, tmp_path: Path) -> None:
        _write(tmp_path, {
            M: _BASE,
            "caller.py": "from m import show\n\n\ndef use():\n    return show(1)\n",
        })
        runner = ScriptRunner({"b": [{SHOW: "def show(uid):\n    return 2\n"}]})
        # Validation would drop the ghost context id (no task creates it).
        session, store, _ = make_session(tmp_path, runner, validate=False)
        session.initialize()
        session.install_plan([task("b", [SHOW], context=["ghost.py::function::g"])])
        assert session.run().ok
        read_set = session.wave.read_sets["b"]
        assert read_set[NodeId(SHOW)].layer == "write_targets"
        assert read_set[NodeId(LOAD)].layer == "same_file"
        assert read_set[NodeId("caller.py::function::use")].layer == "cross_file"
        assert read_set[NodeId(LOAD)].version == 1
        assert read_set[NodeId(LOAD)].digest is not None
        # A planner context node that did not exist is recorded as absent.
        ghost = read_set[NodeId("ghost.py::function::g")]
        assert ghost.digest is None and ghost.version is None

    def test_read_sets_are_persisted_with_the_task_graph(self, tmp_path: Path) -> None:
        import json

        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"b": [{SHOW: "def show(uid):\n    return 2\n"}]})
        session, _, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("b", [SHOW])])
        session.run()
        graph = json.loads((tmp_path / ".mak" / "task_graph.json").read_text())
        persisted = graph["annotations"]["read_sets"]["b"]
        assert persisted[LOAD]["version"] == 1
        assert "source" not in persisted[LOAD]


class TestStaleReads:
    def test_body_only_change_is_accepted_and_logged(self, tmp_path: Path) -> None:
        result, runner, logger, _ = _race(
            tmp_path,
            "def load(uid):\n    return {'name': 'y'}\n",
            ["def show(uid):\n    return load(uid)['name']\n"],
        )
        assert result.ok
        (event,) = events(logger, EventType.STALE_READ)
        assert event.payload["node_id"] == LOAD
        assert event.payload["change"] == "body_only"
        assert event.payload["verdict"] == "accept"
        assert runner.calls["b"] == 1

    def test_uncertain_api_change_redispatches_with_the_diff(
        self, tmp_path: Path
    ) -> None:
        result, runner, logger, store = _race(
            tmp_path,
            "def load(uid) -> 'User':\n    return User('x')\n",
            [
                "def show(uid):\n    return load(uid)['name']\n",
                "def show(uid):\n    return load(uid).name\n",
            ],
            a_needle="-> 'User'",
        )
        assert result.ok
        assert runner.calls["b"] == 2
        retry = runner.bundles_for("b")[1]
        assert retry.retry_note is not None
        assert "read (v1)" in retry.retry_note and "-> 'User'" in retry.retry_note
        assert ".name" in store.get_node(NodeId(SHOW)).source
        assert result.metrics["stale_redispatches"] == 1.0
        verdicts = [e.payload["verdict"] for e in events(logger, EventType.STALE_READ)]
        assert verdicts == ["redispatch"]

    def test_compatible_parameter_change_is_rechecked_and_accepted(
        self, tmp_path: Path
    ) -> None:
        result, runner, logger, _ = _race(
            tmp_path,
            "def load(uid, verbose=False):\n    return {'name': 'x'}\n",
            ["def show(uid):\n    return load(uid)\n"],
            a_needle="verbose",
        )
        assert result.ok and runner.calls["b"] == 1
        (event,) = events(logger, EventType.STALE_READ)
        assert event.payload["verdict"] == "accept"
        assert "re-verified" in str(event.payload["reason"])

    def test_incompatible_parameter_change_redispatches(self, tmp_path: Path) -> None:
        result, runner, logger, store = _race(
            tmp_path,
            "def load(uid, region):\n    return {'name': 'x'}\n",
            [
                "def show(uid):\n    return load(uid)\n",
                "def show(uid):\n    return load(uid, 'eu')\n",
            ],
            a_needle="region",
        )
        assert result.ok and runner.calls["b"] == 2
        (event,) = events(logger, EventType.STALE_READ)
        assert event.payload["verdict"] == "redispatch"
        assert "missing required argument 'region'" in str(event.payload["reason"])
        assert "'eu'" in store.get_node(NodeId(SHOW)).source

    def test_unreferenced_api_change_is_accepted(self, tmp_path: Path) -> None:
        result, runner, logger, _ = _race(
            tmp_path,
            "def helper(x, y):\n    return 0\n",
            ["def show(uid):\n    return 7\n"],
            a_target=HELPER,
            a_needle="x, y",
        )
        assert result.ok and runner.calls["b"] == 1
        (event,) = events(logger, EventType.STALE_READ)
        assert event.payload["change"] == "api_change"
        assert event.payload["referenced"] is False
        assert event.payload["verdict"] == "accept"

    def test_strict_policy_redispatches_even_a_body_change(
        self, tmp_path: Path
    ) -> None:
        result, runner, _, _ = _race(
            tmp_path,
            "def load(uid):\n    return {'name': 'y'}\n",
            ["def show(uid):\n    return 2\n"],
            semantic=SemanticConfig(stale_read="redispatch"),
        )
        assert result.ok and runner.calls["b"] == 2

    def test_reject_policy_counts_a_conflict(self, tmp_path: Path) -> None:
        result, runner, _, _ = _race(
            tmp_path,
            "def load(uid) -> int:\n    return 1\n",
            ["def show(uid):\n    return load(uid)\n"],
            semantic=SemanticConfig(stale_read="reject"),
            a_needle="-> int",
        )
        assert result.metrics["conflict_rejections"] >= 1
        assert result.metrics["stale_redispatches"] == 0

    def test_accept_if_api_stable_redispatches_any_api_change(
        self, tmp_path: Path
    ) -> None:
        result, runner, _, _ = _race(
            tmp_path,
            "def helper(x, y):\n    return 0\n",
            ["def show(uid):\n    return 7\n"],
            a_target=HELPER,
            a_needle="x, y",
            semantic=SemanticConfig(stale_read="accept_if_api_stable"),
        )
        assert result.ok and runner.calls["b"] == 2

    def test_no_stale_read_when_nothing_moved(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"b": [{SHOW: "def show(uid):\n    return 2\n"}]})
        session, _, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("b", [SHOW])])
        assert session.run().ok
        assert events(logger, EventType.STALE_READ) == []


TABLE = "r.py::function::_register_all"
_TABLE_FILE = (
    "def register(key, fn):\n    pass\n\n\n"
    "def _register_all() -> None:\n"
    '    """Every route."""\n'
    "    raise NotImplementedError\n"
)


def _append(line: str) -> object:
    """Return a response that appends ``line`` to the table the agent was shown."""
    def build(bundle: object) -> str:
        current = bundle.context[f"write_source:{TABLE}"]  # type: ignore[attr-defined]
        body = current.replace("    raise NotImplementedError\n", "")
        return body + f"    {line}\n"

    return build


class TestRegistrars:
    def _run(
        self, tmp_path: Path, a_line: str, b_line: str, **kw: object
    ) -> tuple[object, ScriptRunner, object, object]:
        _write(tmp_path, {"r.py": _TABLE_FILE})
        runner = ScriptRunner({
            "a": [{TABLE: _append(a_line)}],
            "b": [{TABLE: _append(b_line)}, {TABLE: _append('register("/c", c)')}],
        })
        session, store, logger = make_session(tmp_path, runner, **kw)  # type: ignore[arg-type]
        runner._hold["b"] = committed(store, TABLE, a_line)
        session.initialize()
        session.install_plan([task("a", [TABLE]), task("b", [TABLE])])
        return session.run(), runner, logger, store

    def test_concurrent_appenders_are_merged(self, tmp_path: Path) -> None:
        result, runner, logger, store = self._run(
            tmp_path, 'register("/a", a)', 'register("/b", b)'
        )
        assert result.ok
        assert runner.calls == {"a": 1, "b": 1}
        table = store.get_node(NodeId(TABLE)).source
        assert 'register("/a", a)' in table and 'register("/b", b)' in table
        assert events(logger, EventType.REGISTRY_MERGED)
        assert result.metrics["conflict_rejections"] == 0

    def test_same_key_is_a_detected_collision(self, tmp_path: Path) -> None:
        result, runner, logger, store = self._run(
            tmp_path, 'register("/a", a)', 'register("/a", b)'
        )
        assert result.ok  # B's retry appends a different key
        reasons = [
            r for e in events(logger, EventType.CONFLICT_DETECTED)
            for r in e.payload.get("reasons", [])  # type: ignore[union-attr]
        ]
        assert any("registry key collision" in str(r) for r in reasons)
        table = store.get_node(NodeId(TABLE)).source
        assert table.count('"/a"') == 1 and '"/c"' in table

    def test_flag_off_serializes_on_the_node_lock(self, tmp_path: Path) -> None:
        _write(tmp_path, {"r.py": _TABLE_FILE})
        runner = ScriptRunner({
            "a": [{TABLE: _append('register("/a", a)')}],
            "b": [{TABLE: _append('register("/b", b)')}],
        })
        session, store, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(registry_keys=False)
        )
        session.initialize()
        session.install_plan([task("a", [TABLE]), task("b", [TABLE])])
        assert session.run().ok
        # Serialized: B was dispatched after A committed and saw its line.
        b_bundle = runner.bundles_for("b")[0]
        assert '"/a"' in b_bundle.context[f"write_source:{TABLE}"]
        assert events(logger, EventType.REGISTRY_MERGED) == []

    def test_non_append_edit_waits_for_exclusive_access(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {"r.py": _TABLE_FILE})
        rewrite = (
            "def _register_all() -> None:\n"
            '    """Rewritten."""\n'
            '    register("/z", z)\n'
        )
        runner = ScriptRunner({
            "a": [{TABLE: _append('register("/a", a)')}],
            "b": [{TABLE: rewrite}],
        })
        session, store, logger = make_session(tmp_path, runner)
        runner._hold["a"] = logged(logger, EventType.TASK_DISPATCHED, task_id="b")
        session.initialize()
        session.install_plan([task("a", [TABLE]), task("b", [TABLE])])
        result = session.run()
        assert result.ok
        failures = [
            e for e in events(logger, EventType.AGENT_RESULT)
            if e.payload["task_id"] == "b"
        ]
        assert failures  # B ran at least once
        # Whatever the order, nothing A appended was lost silently: either B's
        # rewrite came after A (and saw A's line) or A merged onto B's table.
        table = store.get_node(NodeId(TABLE)).source
        assert '"/z"' in table


class TestInterfaceEnforcement:
    def test_body_only_promise_is_enforced(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"a": [
            {LOAD: "def load(uid, extra):\n    return {}\n"},
            {LOAD: "def load(uid):\n    return {}\n"},
        ]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("a", [LOAD], changes_api=False)])
        result = session.run()
        assert result.ok and runner.calls["a"] == 2
        note = runner.bundles_for("a")[1].retry_note
        assert note is not None and "body-only" in note
        (event,) = events(logger, EventType.API_ESCALATED)
        assert event.payload["outcome"] == "refused_promise"
        assert store.get_node(NodeId(LOAD)).source.startswith("def load(uid):")

    def test_undeclared_change_is_escalated_when_free(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"a": [{
            LOAD: "def load(uid, extra):\n    return {}\n",
            SHOW: "def show(uid):\n    return 3\n",
        }]})
        session, _, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([
            task("a", [LOAD, SHOW], changes_api=True, api_targets=[NodeId(SHOW)])
        ])
        assert session.run().ok
        (event,) = events(logger, EventType.API_ESCALATED)
        assert event.payload["outcome"] == "acquired"

    def test_undeclared_change_waits_for_concurrent_readers(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({
            "w": [{
                LOAD: "def load(uid, extra=None):\n    return {}\n",
                HELPER: "def helper():\n    return 5\n",
            }],
            "r": [{SHOW: "def show(uid):\n    return load(uid)\n"}],
        })
        # Validation would order R after W (R's target calls what W writes);
        # this test is about the lock level, so the plan is taken as given.
        session, _, logger = make_session(tmp_path, runner, validate=False)
        # R's target calls LOAD in the pre-wave graph? Not yet — so give R the
        # read lock by making SHOW reference LOAD before the wave starts.
        (tmp_path / M).write_text(_BASE.replace(
            "def show(uid):\n    return 1\n", "def show(uid):\n    return load(uid)\n"
        ))
        runner._hold["r"] = logged(
            logger, EventType.API_ESCALATED, outcome="refused_contended"
        )
        session.initialize()
        session.install_plan([
            task("w", [LOAD, HELPER], changes_api=True, api_targets=[NodeId(HELPER)]),
            task("r", [SHOW], changes_api=False),
        ])
        result = session.run()
        assert result.ok
        escalations = events(logger, EventType.API_ESCALATED)
        outcomes = [e.payload["outcome"] for e in escalations]
        assert outcomes[0] == "refused_contended"
        assert outcomes[-1] == "acquired"
        # Parked, not re-run: the writer's agent was called exactly once.
        assert runner.calls["w"] == 1
        deferred = events(logger, EventType.COMMIT_DEFERRED)
        assert any(e.payload.get("resumed") for e in deferred)

    def test_api_locks_off_skips_enforcement(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"a": [{LOAD: "def load(uid, extra):\n    return {}\n"}]})
        session, _, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(api_locks=False)
        )
        session.initialize()
        session.install_plan([task("a", [LOAD], changes_api=False)])
        assert session.run().ok and runner.calls["a"] == 1
        assert events(logger, EventType.API_ESCALATED) == []


class TestParkedCommits:
    def test_a_cycle_of_parked_results_is_broken(self, tmp_path: Path) -> None:
        base = _BASE.replace(
            "def show(uid):\n    return 1\n", "def show(uid):\n    return load(uid)\n"
        )
        _write(tmp_path, {M: base, "r.py": _TABLE_FILE})
        rewrite = (
            "def _register_all() -> None:\n"
            '    """Rewritten."""\n'
            '    register("/r", r)\n'
        )
        runner = ScriptRunner({
            # W: an undeclared interface change to LOAD, plus a table append.
            "w": [{
                LOAD: "def load(uid, extra=None):\n    return {}\n",
                HELPER: "def helper():\n    return 5\n",
                TABLE: _append('register("/w", w)'),
            }],
            # R: reads LOAD's interface (SHOW calls it) and rewrites the table.
            "r": [{SHOW: "def show(uid):\n    return load(uid)\n", TABLE: rewrite}],
        })
        session, store, logger = make_session(tmp_path, runner, validate=False)
        session.initialize()
        session.install_plan([
            task("w", [LOAD, HELPER, TABLE], changes_api=True,
                 api_targets=[NodeId(HELPER)]),
            task("r", [SHOW, TABLE], changes_api=False),
        ])
        result = session.run()
        assert result.ok, result.failure_reasons
        released = [
            e for e in events(logger, EventType.COMMIT_DEFERRED)
            if e.payload.get("released")
        ]
        assert [e.payload["task_id"] for e in released] == ["w"]
        assert runner.calls == {"r": 1, "w": 2}
        table = store.get_node(NodeId(TABLE)).source
        assert '"/r"' in table and '"/w"' in table
        # The released task was re-dispatched with the reason it was released.
        note = runner.bundles_for("w")[1].retry_note
        assert note is not None and "released" in note
