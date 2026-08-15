"""Wave 17 acceptance: containment holds at every entry point, state is durable.

One test per acceptance criterion in TASKS.md § "Wave 17 acceptance". The
containment cases are deliberately checked on all three entry points separately
rather than once through the highest layer: they are independent trust
boundaries, and the whole point of the wave is that no single one of them is
load-bearing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mak.agent_runner.protocol import map_returned_sources
from mak.config import anchor_mak_dir, stale_mak_dir
from mak.core.exceptions import SessionError, UnsafeNodeIdError
from mak.core.types import NodeFragment, NodeId
from mak.node_store.store import NodeStore
from mak.planner.planner import parse_plan
from tests.test_session import _config, _session, _task

ESCAPING_TARGETS = ["/etc/cron.d/payload.py", "../../escaped.py", ".mak/x.py"]


def _plan_json(target: str) -> str:
    return (
        '[{"task_id": "t", "description": "d", "target_nodes": ["'
        + target
        + '"], "context_nodes": [], "depends_on": [], "agent_type": "a"}]'
    )


class TestGateOnePlanner:
    """parse_plan refuses an escaping target before a lock or a token is spent."""

    @pytest.mark.parametrize("target", ESCAPING_TARGETS)
    def test_escaping_target_is_rejected(self, target: str) -> None:
        with pytest.raises(ValueError, match="inside the working directory"):
            parse_plan(_plan_json(target))

    def test_rejection_is_a_valueerror_so_the_planner_retries(self) -> None:
        # Not UnsafeNodeIdError: _complete_with_retries catches ValueError and
        # feeds the reason back to the model, so a hallucinated path is re-asked
        # rather than taking the whole run down.
        with pytest.raises(ValueError):
            parse_plan(_plan_json("/etc/x.py"))

    def test_the_message_names_the_offender(self) -> None:
        with pytest.raises(ValueError, match="/etc/x.py"):
            parse_plan(_plan_json("/etc/x.py"))

    def test_ordinary_targets_still_parse(self) -> None:
        plan = parse_plan(_plan_json("pkg/mod.py"))
        assert plan[0].target_nodes == [NodeId("pkg/mod.py")]


class TestGateTwoNodeStore:
    """The store refuses to address a fragment outside its own root."""

    @pytest.mark.parametrize("node_id", ["../out.py", "/etc/x.py"])
    def test_escaping_put_is_refused(self, tmp_path: Path, node_id: str) -> None:
        store = NodeStore(tmp_path / "store")
        nid = NodeId(node_id)
        with pytest.raises(UnsafeNodeIdError):
            store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))

    def test_nothing_is_written_outside_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        store = NodeStore(root)
        nid = NodeId("../../ESCAPED.py")
        with pytest.raises(UnsafeNodeIdError):
            store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
        assert not (tmp_path.parent / "ESCAPED.py").exists()
        assert not (tmp_path / "ESCAPED.py").exists()

    def test_the_store_can_still_address_mak_dir_nodes(self, tmp_path: Path) -> None:
        # Containment only. The Wave 11 prune has to name the ".mak/…" nodes an
        # older MAK ingested in order to delete them; a store that refuses to
        # address them could never clean them up.
        store = NodeStore(tmp_path / "store")
        nid = NodeId(".mak/node_store/leaked.py::function::f")
        store.put_node(nid, NodeFragment(nid, "function", "def f():\n    pass\n", 1))
        store.commit_node(nid)
        assert store.remove_node(nid) is True


class TestGateThreeSession:
    """install_plan is the funnel the TUI and every cascade wave pass through."""

    @pytest.mark.parametrize("target", ESCAPING_TARGETS)
    def test_escaping_target_is_refused(self, tmp_path: Path, target: str) -> None:
        store = NodeStore(tmp_path / ".mak" / "node_store")
        session = _session(tmp_path, runner=_Never(), node_store=store)
        session.initialize()
        with pytest.raises(SessionError, match="outside the working directory"):
            session.install_plan([_task("t1", [target])])

    def test_a_legitimate_new_file_is_still_allowed(self, tmp_path: Path) -> None:
        # Greenfield targets are the normal shape and must not be caught by this.
        store = NodeStore(tmp_path / ".mak" / "node_store")
        session = _session(tmp_path, runner=_Never(), node_store=store)
        session.initialize()
        session.install_plan([_task("t1", ["pkg/brand_new.py"])])

    def test_the_error_names_every_offender(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / ".mak" / "node_store")
        session = _session(tmp_path, runner=_Never(), node_store=store)
        session.initialize()
        with pytest.raises(SessionError) as excinfo:
            session.install_plan(
                [_task("t1", ["/etc/a.py"]), _task("t2", ["../b.py"])]
            )
        assert "/etc/a.py" in str(excinfo.value)
        assert "../b.py" in str(excinfo.value)


class _Never:
    """An assigner that must never be reached."""

    def assign(self, adapter: object, task: object) -> object:  # pragma: no cover
        raise AssertionError("a refused plan must not dispatch")


class TestMakDirAnchoring:
    """A project's state lives with the project, not with the shell."""

    def test_relative_mak_dir_anchors_under_work_dir(self, tmp_path: Path) -> None:
        config = replace(
            _config(tmp_path),
            session=replace(
                _config(tmp_path).session, work_dir=str(tmp_path), mak_dir=".mak"
            ),
        )
        anchored = anchor_mak_dir(config)
        assert Path(anchored.session.mak_dir) == (tmp_path / ".mak").resolve()

    def test_absolute_mak_dir_is_left_alone(self, tmp_path: Path) -> None:
        explicit = str(tmp_path / "elsewhere")
        config = replace(
            _config(tmp_path),
            session=replace(
                _config(tmp_path).session, work_dir=str(tmp_path), mak_dir=explicit
            ),
        )
        assert anchor_mak_dir(config).session.mak_dir == explicit

    def test_two_projects_get_separate_stores(self, tmp_path: Path) -> None:
        # The corruption this prevents: node ids are work-dir-relative, so
        # "toolkit/registry.py" in project A and project B were the same id in
        # one shared store, and B inherited A's content.
        base = _config(tmp_path)
        dirs = []
        for name in ("projA", "projB"):
            work = tmp_path / name
            work.mkdir()
            config = anchor_mak_dir(
                replace(
                    base,
                    session=replace(
                        base.session, work_dir=str(work), mak_dir=".mak"
                    ),
                )
            )
            dirs.append(config.session.mak_dir)
        assert dirs[0] != dirs[1]

    def test_orphan_detection_reports_a_foreign_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shell = tmp_path / "shell"
        shell.mkdir()
        (shell / ".mak").mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(shell)

        base = _config(tmp_path)
        config = replace(
            base,
            session=replace(base.session, work_dir=str(project), mak_dir=".mak"),
        )
        assert stale_mak_dir(config) == (shell / ".mak").resolve()

    def test_no_orphan_when_running_from_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".mak").mkdir()
        monkeypatch.chdir(project)

        base = _config(tmp_path)
        config = replace(
            base,
            session=replace(base.session, work_dir=str(project), mak_dir=".mak"),
        )
        assert stale_mak_dir(config) is None


class TestFoldOrdering:
    """Folded fragments rebuild a file in source order, not emission order."""

    def test_module_header_leads_regardless_of_emission_order(self) -> None:
        grant = [NodeId("m.py")]
        returned = {
            NodeId("m.py::function::g"): "def g():\n    return 1\n",
            NodeId(
                "m.py::module_header::__header__"
            ): "from __future__ import annotations\n",
        }
        accepted, dropped = map_returned_sources(grant, returned)
        assert dropped == []
        source = accepted[NodeId("m.py")]
        # Not merely "somewhere earlier" — a __future__ import is only legal as
        # the very first statement, so anything else fails to compile.
        assert source.startswith("from __future__ import annotations")
        compile(source, "<test>", "exec")

    def test_store_order_decides_the_rest(self) -> None:
        grant = [NodeId("m.py")]
        returned = {
            NodeId("m.py::function::c"): "def c():\n    return 3\n",
            NodeId("m.py::function::a"): "def a():\n    return 1\n",
            NodeId("m.py::function::b"): "def b():\n    return 2\n",
        }
        order = {"m.py::function::a": 0, "m.py::function::b": 1, "m.py::function::c": 2}
        accepted, _ = map_returned_sources(
            grant, returned, order_key=lambda n: order.get(str(n))
        )
        source = accepted[NodeId("m.py")]
        assert source.index("def a") < source.index("def b") < source.index("def c")

    def test_emission_order_is_the_fallback(self) -> None:
        grant = [NodeId("m.py")]
        returned = {
            NodeId("m.py::function::a"): "def a():\n    return 1\n",
            NodeId("m.py::function::b"): "def b():\n    return 2\n",
        }
        accepted, _ = map_returned_sources(grant, returned)
        source = accepted[NodeId("m.py")]
        assert source.index("def a") < source.index("def b")

    def test_the_grant_boundary_is_unchanged(self) -> None:
        # Ordering must not have loosened what may be written.
        accepted, dropped = map_returned_sources(
            [NodeId("m.py")], {NodeId("other.py::function::x"): "def x(): pass\n"}
        )
        assert accepted == {}
        assert [str(n) for n, _reason in dropped] == ["other.py::function::x"]


class _ShutdownSpy:
    """An assigner that also exposes AgentRunner's pool-shutdown entry point."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def assign(self, adapter: object, task: object) -> object:
        return None

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class TestAgentLifecycle:
    """Pools and subprocesses do not outlive the session that created them."""

    def test_close_shuts_the_agent_runner_down(self, tmp_path: Path) -> None:
        # AgentRunner.shutdown() is documented as "call at session teardown" and
        # had no caller anywhere, so every pooled CLI agent process survived until
        # the interpreter exited — the whole session, in the long-lived TUI.
        store = NodeStore(tmp_path / ".mak" / "node_store")
        spy = _ShutdownSpy()
        session = _session(tmp_path, runner=spy, node_store=store)
        session.close()
        assert spy.shutdown_calls == 1

    def test_close_is_repeatable(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / ".mak" / "node_store")
        spy = _ShutdownSpy()
        session = _session(tmp_path, runner=spy, node_store=store)
        session.close()
        session.close()
        assert spy.shutdown_calls == 2

    def test_a_runner_without_shutdown_is_fine(self, tmp_path: Path) -> None:
        # The injected _Assigner protocol does not declare shutdown; a fake that
        # lacks it must not break teardown.
        store = NodeStore(tmp_path / ".mak" / "node_store")
        session = _session(tmp_path, runner=_Never(), node_store=store)
        session.close()

    def test_a_wedged_run_does_not_block_on_teardown(self, tmp_path: Path) -> None:
        # The hang this closes: the collect timeout stops waiting for a wedged
        # agent, then close() blocked joining that very call. The run must finish
        # and report rather than hang.
        import time

        # Deliberately a short sleep, not a long one. A pool worker is
        # non-daemon, so Python joins it at interpreter exit — a 30s stand-in
        # here would add 30s to every full test run even though the assertion
        # passes in milliseconds.
        worker_seconds = 2.0

        class _Wedged:
            def assign(self, adapter: object, task: object) -> object:
                time.sleep(worker_seconds)
                return None

        store = NodeStore(tmp_path / ".mak" / "node_store")
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        session = _session(tmp_path, runner=_Wedged(), node_store=store)
        session.initialize()
        session.install_plan([_task("t1", ["a.py::function::f"])])
        session._collect_timeout = 0.2

        started = time.monotonic()
        result = session.run()
        elapsed = time.monotonic() - started

        # Returned before the worker did: teardown declined to join it. Before
        # this, close() blocked on exactly the call the collect timeout had just
        # given up on, so the timeout bought nothing.
        assert elapsed < worker_seconds
        assert not result.ok
        assert "t1" in (result.blocked + result.failed)


class TestApiTimeoutsAreConfigured:
    """Every provider client gets a bounded request timeout."""

    def test_adapters_accept_and_store_a_timeout(self) -> None:
        from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
        from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
        from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter

        for cls in (AnthropicApiAdapter, OpenAiApiAdapter, GeminiApiAdapter):
            assert cls(timeout=42.0).timeout == 42.0  # type: ignore[call-arg]

    def test_bootstrap_threads_the_configured_timeout_into_the_client(self) -> None:
        # The bug: AgentConfig.timeout was consumed only by the *subprocess* read
        # loop, so an API agent had no bound at all.
        from mak.bootstrap import build_registry
        from mak.config import AgentConfig, MakConfig

        config = MakConfig(
            agents=(AgentConfig(type="anthropic_api", timeout=123),)
        )
        adapter = build_registry(config).get("anthropic_api")
        assert adapter.timeout == 123.0  # type: ignore[attr-defined]

    def test_gemini_converts_seconds_to_milliseconds(self) -> None:
        # google-genai measures HttpOptions.timeout in milliseconds while the
        # other two SDKs use seconds; passing seconds through would make every
        # real call time out immediately.
        from google import genai

        from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter

        adapter = GeminiApiAdapter(api_key="k", timeout=30.0)
        client = adapter._get_client()
        assert isinstance(client, genai.Client)
        assert client._api_client._http_options.timeout == 30_000


class TestCascadeIdsDoNotCollide:
    """Two paths that sanitize alike must not become one task id."""

    def test_sanitization_collisions_are_broken_by_a_digest(
        self, tmp_path: Path
    ) -> None:
        from mak.session import _fixup_task_id

        # Both of these sanitize to "api_fix_a_b_py" under the old scheme, and
        # DAG rejects a duplicate id — losing the entire cascade wave.
        first = _fixup_task_id("api_fix", "a/b.py")
        second = _fixup_task_id("api_fix", "a-b.py")
        assert first != second
        assert first.startswith("api_fix_a_b_py_")
        assert second.startswith("api_fix_a_b_py_")

    def test_ids_are_stable_for_the_same_subject(self) -> None:
        from mak.session import _fixup_task_id

        first = _fixup_task_id("cascade", "x/y.py")
        assert first == _fixup_task_id("cascade", "x/y.py")

    def test_ids_stay_dag_safe(self) -> None:
        from mak.session import _fixup_task_id

        generated = _fixup_task_id("cascade", "pkg/mod.py::function::f")
        assert generated.replace("_", "").isalnum()
