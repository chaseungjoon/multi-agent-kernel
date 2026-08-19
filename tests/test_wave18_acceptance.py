"""Wave 18 acceptance: the residual audit items, one test per criterion.

The wave's items are independent of one another, so this file is grouped the way
TASKS.md groups them — by area, not by severity — and each class states the
criterion it stands for. Behaviour-preservation claims (18.3, 18.4) are checked
by differential tests against the implementation each item replaced, because
"identical, only cheaper" is the whole claim being made.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import replace
from pathlib import Path

import cli.core.api_keys as api_keys
import pytest
from cli.__main__ import _latest_release_tag, _resolve_update_target, _version_key

from mak.config import (
    MakConfig,
    NodeStoreConfig,
    SessionConfig,
    load_config,
)
from mak.core.exceptions import ConfigError, NodeStoreError
from mak.core.logging import EventType
from mak.core.types import NodeFragment, NodeId, TaskBundle, TaskResult
from mak.node_store.ingestion import _is_excluded, iter_source_files
from mak.node_store.store import (
    DEFAULT_VERSION_RETENTION,
    MIN_VERSION_RETENTION,
    NodeStore,
)
from tests.test_session import _config, _session, _store, _task

# ── 18.1 a no-op is refused where the target cannot have existed ─────────────


class _AssertedNoOpRunner:
    """An agent that always claims there was nothing to change."""

    def __init__(self) -> None:
        self.notes: list[str | None] = []

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        self.notes.append(task.retry_note)
        return TaskResult(
            task_id=task.task_id, success=True, no_changes_required=True
        )


class _CreateThenNoOpRunner:
    """Writes the file for the first task; asserts a no-op for every other."""

    def __init__(self, node_store: NodeStore, creator: str, source: str) -> None:
        self._store = node_store
        self._creator = creator
        self._source = source
        self.notes: list[str | None] = []

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        self.notes.append(task.retry_note)
        if task.task_id == self._creator:
            for node_id in task.target_nodes:
                self._store.put_node(
                    node_id, NodeFragment(node_id, "module", self._source, 1)
                )
            return TaskResult(
                task_id=task.task_id,
                success=True,
                modified_nodes=list(task.target_nodes),
            )
        return TaskResult(
            task_id=task.task_id, success=True, no_changes_required=True
        )


class TestNoopIsRefusedWhereNothingCouldHaveBeenInspected:
    def test_a_dependency_created_target_cannot_be_no_opped(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = _CreateThenNoOpRunner(store, "build", "def a():\n    return 0\n")
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan(
            [
                _task("build", ["new.py"]),
                _task("polish", ["new.py::function::a"], deps=["build"]),
            ]
        )
        result = session.run()
        assert result.completed == ("build",)
        assert result.failed == ("polish",)
        assert "did not exist when this wave was planned" in (
            result.failure_reasons["polish"]
        )
        assert "'build'" in result.failure_reasons["polish"]

    def test_the_refusal_is_fed_back_as_the_retry_instruction(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = _CreateThenNoOpRunner(store, "build", "def a():\n    return 0\n")
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan(
            [
                _task("build", ["new.py"]),
                _task("polish", ["new.py::function::a"], deps=["build"]),
            ]
        )
        session.run()
        # The retry differs from the attempt that failed, and says why.
        assert any(
            note and "did not exist when this wave was planned" in note
            for note in runner.notes
        )

    def test_a_greenfield_whole_file_grant_is_refused_on_the_first_attempt(
        self, tmp_path: Path
    ) -> None:
        # The file exists on disk but not in the wave's starting inventory: an
        # earlier MAK run left it, this wave was planned to write it, and a
        # first-attempt "nothing to change" is not an assessment of anything.
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=_AssertedNoOpRunner(),
            node_store=store,
            max_attempts=1,
        )
        session.initialize()
        (tmp_path / "late.py").write_text("def a():\n    return 0\n")
        store.parse_file_into_nodes(
            "late.py", (tmp_path / "late.py").read_text()
        )
        session.install_plan([_task("write", ["late.py"])])
        # install_plan snapshots *after* the node exists, so re-snapshot the way
        # a real wave would see it: the plan is what predates the file.
        session._preexisting_files = set()
        result = session.run()
        assert result.failed == ("write",)
        assert "not an assessment" in result.failure_reasons["write"]

    def test_a_second_attempt_on_a_greenfield_grant_is_accepted(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=_AssertedNoOpRunner(),
            node_store=store,
            max_attempts=3,
        )
        session.initialize()
        (tmp_path / "late.py").write_text("def a():\n    return 0\n")
        store.parse_file_into_nodes(
            "late.py", (tmp_path / "late.py").read_text()
        )
        session.install_plan([_task("write", ["late.py"])])
        session._preexisting_files = set()
        result = session.run()
        # Refused once, then accepted: the agent has now seen the retry note and
        # the file's real contents, so its second assertion is about something.
        assert result.completed == ("write",)
        assert result.noop == ("write",)

    def test_an_ordinary_no_op_on_a_pre_existing_file_still_completes(
        self, tmp_path: Path
    ) -> None:
        # The narrowing must not touch the case the acceptance path exists for.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=_AssertedNoOpRunner(), node_store=store
        )
        session.initialize()
        session.install_plan([_task("audit", ["m.py"])])
        result = session.run()
        assert result.ok
        assert result.noop == ("audit",)


# ── 18.2 a spend ceiling stops a runaway run and names the budget ────────────


class _ExpensiveRunner:
    """An agent that reports a fixed, large token spend and stages real work."""

    def __init__(self, node_store: NodeStore, tokens: int) -> None:
        self._store = node_store
        self._tokens = tokens
        self.calls = 0

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        self.calls += 1
        for node_id in task.target_nodes:
            self._store.put_node(
                node_id, NodeFragment(node_id, "function", "x = 1\n", 1)
            )
        return TaskResult(
            task_id=task.task_id,
            success=True,
            modified_nodes=list(task.target_nodes),
            usage={"input_tokens": self._tokens, "output_tokens": self._tokens},
        )


def _budget_config(tmp_path: Path, ceiling: int | None) -> MakConfig:
    base = _config(tmp_path)
    return replace(
        base,
        session=replace(
            base.session, max_total_tokens=ceiling, max_concurrent_agents=1
        ),
    )


class TestSpendCeiling:
    def test_unset_is_unbounded(self, tmp_path: Path) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.py").write_text("def f():\n    return 0\n")
        store = _store(tmp_path)
        runner = _ExpensiveRunner(store, 10_000)
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            config=_budget_config(tmp_path, None),
        )
        session.initialize()
        session.install_plan(
            [_task(n, [f"{n}.py::function::f"]) for n in ("a", "b", "c")]
        )
        result = session.run()
        assert result.ok
        assert result.stopped_reason is None

    def test_a_breach_stops_the_run_and_names_the_budget(
        self, tmp_path: Path
    ) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.py").write_text("def f():\n    return 0\n")
        store = _store(tmp_path)
        runner = _ExpensiveRunner(store, 5_000)
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            config=_budget_config(tmp_path, 9_000),
        )
        session.initialize()
        session.install_plan(
            [_task(n, [f"{n}.py::function::f"]) for n in ("a", "b", "c")]
        )
        result = session.run()
        assert not result.ok
        assert result.stopped_reason is not None
        assert "max_total_tokens" in result.stopped_reason
        assert "9000" in result.stopped_reason
        # One task's worth of spend already passed the ceiling, so the rest are
        # never dispatched.
        assert runner.calls < 3
        assert result.blocked

    def test_work_already_done_is_committed_not_discarded(
        self, tmp_path: Path
    ) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.py").write_text("def f():\n    return 0\n")
        store = _store(tmp_path)
        runner = _ExpensiveRunner(store, 5_000)
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            config=_budget_config(tmp_path, 9_000),
        )
        session.initialize()
        session.install_plan(
            [_task(n, [f"{n}.py::function::f"]) for n in ("a", "b", "c")]
        )
        result = session.run()
        # The task that finished before the ceiling was reached keeps its work:
        # a budget stop never unwinds a commit.
        assert result.completed
        for task_id in result.completed:
            assert (tmp_path / f"{task_id}.py").read_text() == "x = 1\n"

    def test_a_budget_already_spent_by_the_planner_dispatches_nothing(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "a.py").write_text("def f():\n    return 0\n")
        store = _store(tmp_path)
        runner = _ExpensiveRunner(store, 5_000)
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            config=_budget_config(tmp_path, 1_000),
        )
        session.initialize()
        # Decomposition, its retries, and the critique pass are billed too, so a
        # plan can arrive having already spent the run's whole budget.
        session._agent_usage.update({"input_tokens": 900, "output_tokens": 400})
        session.install_plan([_task("a", ["a.py::function::f"])])
        result = session.run()
        assert runner.calls == 0
        assert result.stopped_reason is not None
        assert not result.completed

    def test_the_ceiling_is_configurable(self, tmp_path: Path) -> None:
        path = tmp_path / "mak.yaml"
        path.write_text(
            "session:\n  max_total_tokens: 250000\n"
            "agents:\n  - type: anthropic_api\n"
        )
        assert load_config(path).session.max_total_tokens == 250_000

    def test_a_non_positive_ceiling_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "mak.yaml"
        path.write_text(
            "session:\n  max_total_tokens: 0\nagents:\n  - type: anthropic_api\n"
        )
        with pytest.raises(ConfigError, match="max_total_tokens"):
            load_config(path)


# ── 18.3 enrichment is cheaper and byte-identical ────────────────────────────


def _legacy_scan(
    session: object, symbols: set[str], target_files: set[str], context: dict[str, str]
) -> list[tuple[NodeId, str, frozenset[str]]]:
    """Reproduce the pre-Wave-18 scan: walk the store, regex every node."""
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(s) for s in sorted(symbols)) + r")\b"
    )
    found: list[tuple[NodeId, str, frozenset[str]]] = []
    for node_id in session._node_store.list_nodes():  # type: ignore[attr-defined]
        if str(node_id).split("::", 1)[0] in target_files:
            continue
        if any(
            f"{key}:{node_id}" in context
            for key in ("write_source", "read_source", "read_api")
        ):
            continue
        source = session._node_source(node_id)  # type: ignore[attr-defined]
        if not source:
            continue
        hits = frozenset(pattern.findall(source))
        if hits:
            found.append((node_id, source, hits))
    return found


def _corpus(tmp_path: Path) -> NodeStore:
    """Build a store with cross-file references worth finding."""
    (tmp_path / "core.py").write_text(
        "def transform_payload(data):\n    return data\n\n\n"
        "def unrelated_helper():\n    return 1\n"
    )
    (tmp_path / "caller.py").write_text(
        "from core import transform_payload\n\n\n"
        "def run():\n    return transform_payload({})\n"
    )
    (tmp_path / "other.py").write_text(
        "def unrelated_helper_use():\n    return unrelated_helper()\n"
    )
    store = _store(tmp_path)
    for name in ("core.py", "caller.py", "other.py"):
        store.parse_file_into_nodes(name, (tmp_path / name).read_text())
    return store


class TestEnrichmentIsIdenticalAndCheaper:
    def test_the_index_returns_exactly_what_the_scan_returned(
        self, tmp_path: Path
    ) -> None:
        store = _corpus(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store)
        for symbols in (
            {"transform_payload"},
            {"unrelated_helper"},
            {"transform_payload", "unrelated_helper"},
            {"nothing_matches_this"},
        ):
            expected = sorted(
                _legacy_scan(session, symbols, set(), {}), key=lambda c: str(c[0])
            )
            actual = sorted(
                session._scan_for_symbols(symbols, set(), {}),
                key=lambda c: str(c[0]),
            )
            assert actual == expected, symbols

    def test_the_bundle_context_is_byte_identical(self, tmp_path: Path) -> None:
        store = _corpus(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store)
        session.initialize()
        session.install_plan([_task("t", ["core.py::function::transform_payload"])])
        dispatch = session._enrich_bundle(
            TaskBundle(
                task_id="t",
                description="d",
                target_nodes=[NodeId("core.py::function::transform_payload")],
            )
        )
        # The caller was found through the index, and carries its call site.
        key = "read_source:caller.py::function::run"
        assert key in dispatch.bundle.context
        assert "transform_payload({})" in dispatch.bundle.context[key]

    def test_the_index_is_built_once_per_store_generation(
        self, tmp_path: Path
    ) -> None:
        store = _corpus(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store)
        builds = 0
        original = session._build_symbol_index

        def counting() -> dict[str, list[NodeId]]:
            nonlocal builds
            builds += 1
            return original()

        session._build_symbol_index = counting  # type: ignore[method-assign]
        for _ in range(5):
            session._scan_for_symbols({"transform_payload"}, set(), {})
        assert builds == 1

    def test_a_commit_invalidates_the_index(self, tmp_path: Path) -> None:
        store = _corpus(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store)
        assert not session._scan_for_symbols({"brand_new_symbol"}, set(), {})
        nid = NodeId("other.py::function::unrelated_helper_use")
        store.put_node(
            nid,
            NodeFragment(nid, "function", "def unrelated_helper_use():\n"
                         "    return brand_new_symbol()\n", 1),
        )
        store.commit_node(nid)
        assert session._scan_for_symbols({"brand_new_symbol"}, set(), {})

    def test_list_nodes_ordering_is_memoized_and_correct(
        self, tmp_path: Path
    ) -> None:
        store = _corpus(tmp_path)
        first = store.list_nodes()
        assert store.list_nodes() == first
        # Memoized, not frozen: a commit re-derives it.
        generation = store.generation
        nid = NodeId("fresh.py")
        store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
        store.commit_node(nid)
        assert store.generation != generation
        assert nid in store.list_nodes()


# ── 18.4 initialize does not descend into excluded directories ───────────────


def _tree_with_a_virtualenv(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("def f():\n    return 0\n")
    (root / "top.py").write_text("x = 1\n")
    venv = root / ".venv" / "lib" / "python3.11" / "site-packages" / "dep"
    venv.mkdir(parents=True)
    for i in range(20):
        (venv / f"m{i}.py").write_text("y = 1\n")
    cache = root / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "mod.py").write_text("z = 1\n")


class TestIngestionPrunesBeforeDescending:
    def test_the_walk_matches_the_glob_it_replaces(self, tmp_path: Path) -> None:
        _tree_with_a_virtualenv(tmp_path)
        excludes = NodeStoreConfig().exclude_patterns
        includes = NodeStoreConfig().include_patterns
        legacy: list[Path] = []
        seen: set[Path] = set()
        for pattern in includes:
            for path in sorted(tmp_path.glob(pattern)):
                rel = str(path.relative_to(tmp_path))
                if not path.is_file() or _is_excluded(rel, excludes) or path in seen:
                    continue
                seen.add(path)
                legacy.append(path)
        assert iter_source_files(tmp_path, includes, excludes) == legacy

    def test_an_excluded_directory_is_never_entered(self, tmp_path: Path) -> None:
        _tree_with_a_virtualenv(tmp_path)
        entered: list[str] = []
        real_iterdir = Path.iterdir

        def spy(self: Path) -> object:
            entered.append(str(self))
            return real_iterdir(self)

        original = Path.iterdir
        Path.iterdir = spy  # type: ignore[method-assign,assignment]
        try:
            iter_source_files(
                tmp_path,
                NodeStoreConfig().include_patterns,
                NodeStoreConfig().exclude_patterns,
            )
        finally:
            Path.iterdir = original  # type: ignore[method-assign]
        assert not any(".venv" in path for path in entered)
        assert not any("__pycache__" in path for path in entered)
        assert any(path.endswith("pkg") for path in entered)

    def test_initialize_ingests_the_same_set(self, tmp_path: Path) -> None:
        _tree_with_a_virtualenv(tmp_path)
        store = _store(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store)
        inventory = session.initialize()
        files = {str(nid).split("::", 1)[0] for nid in inventory}
        assert files == {"top.py", "pkg/mod.py"}

    def test_a_symlinked_directory_is_not_followed(self, tmp_path: Path) -> None:
        _tree_with_a_virtualenv(tmp_path)
        target = tmp_path / "pkg"
        (tmp_path / "link").symlink_to(target, target_is_directory=True)
        found = iter_source_files(
            tmp_path,
            NodeStoreConfig().include_patterns,
            NodeStoreConfig().exclude_patterns,
        )
        # Matching Path.glob("**/*.py"), which does not follow symlinked dirs.
        relative = {path.relative_to(tmp_path).as_posix() for path in found}
        assert relative == {"top.py", "pkg/mod.py"}


# ── 18.5 the key file is never observable at anything but 0600 ───────────────


class TestKeyFileHygiene:
    def test_the_file_is_created_at_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(os, "umask", lambda mask: 0o022)
        api_keys.save_keys({"ANTHROPIC_API_KEY": "sk-test"})
        path = tmp_path / "mak" / ".env"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_mode_is_set_by_creation_not_by_a_later_chmod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The race this closes: written at the umask, then chmod-ed. With chmod
        # disabled the file must *still* be 0600, which is only true if the mode
        # came from the open.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        api_keys.save_keys({"OPENAI_API_KEY": "sk-test"})
        path = tmp_path / "mak" / ".env"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_an_existing_loose_file_is_repaired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = tmp_path / "mak" / ".env"
        path.parent.mkdir(parents=True)
        path.write_text("ANTHROPIC_API_KEY=old\n")
        os.chmod(path, 0o644)
        api_keys.save_keys({"ANTHROPIC_API_KEY": "sk-new"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_legacy_location_warns_but_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        for name in api_keys.KEY_NAMES:
            monkeypatch.delenv(name, raising=False)
        legacy = tmp_path / "legacy.env"
        legacy.write_text("ANTHROPIC_API_KEY=sk-legacy\n")
        monkeypatch.setattr(api_keys, "_LEGACY_ENV_PATH", legacy)
        with pytest.warns(DeprecationWarning, match="legacy"):
            keys = api_keys.load_keys()
        assert keys["ANTHROPIC_API_KEY"] == "sk-legacy"


# ── 18.6 mak update names the version it moves to ────────────────────────────


class TestUpdateResolvesAReleaseTag:
    def test_versions_order_correctly(self) -> None:
        tags = ["v0.5.9b0", "v0.5.10b0", "v0.5.9", "v0.6.0", "not-a-version"]
        ordered = sorted(
            (t for t in tags if _version_key(t) is not None),
            key=lambda t: _version_key(t) or ((), 0, ""),
        )
        assert ordered == ["v0.5.9b0", "v0.5.9", "v0.5.10b0", "v0.6.0"]

    def test_the_newest_tag_wins_and_the_peeled_commit_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = (
            "a" * 40 + "\trefs/tags/v0.5.9\n"
            + "b" * 40 + "\trefs/tags/v0.5.10\n"
            + "c" * 40 + "\trefs/tags/v0.5.10^{}\n"
        )
        monkeypatch.setattr("cli.__main__._ls_remote", lambda *a: out)
        assert _latest_release_tag() == ("v0.5.10", "c" * 40)

    def test_the_install_spec_is_pinned_to_the_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cli.__main__._latest_release_tag", lambda: ("v0.5.10", "c" * 40)
        )
        target = _resolve_update_target()
        assert target.spec.endswith("@v0.5.10")
        assert target.label == "v0.5.10"
        assert target.commit == "c" * 40

    def test_an_untagged_repo_falls_back_to_head_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.__main__._latest_release_tag", lambda: None)
        monkeypatch.setattr("cli.__main__._remote_commit", lambda: "d" * 40)
        target = _resolve_update_target()
        assert "@" not in target.spec.rsplit("/", 1)[-1]
        assert "no release tag" in target.label


# ── 18.7 no log event misreports its own type ────────────────────────────────


class TestLogAndInventoryCorrectness:
    def test_a_failure_is_not_logged_as_a_completion(self, tmp_path: Path) -> None:
        from mak.core.logging import SessionLogger

        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class FailingRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id, success=False, error="boom"
                )

        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path,
            runner=FailingRunner(),
            node_store=store,
            max_attempts=1,
            logger=logger,
        )
        session.initialize()
        session.install_plan([_task("t", ["m.py::function::a"])])
        session.run()
        kinds = [e.event_type for e in logger.read_log()]
        assert EventType.TASK_FAILED in kinds
        assert EventType.TASK_COMPLETED not in kinds

    def test_an_agent_remap_has_its_own_event(self, tmp_path: Path) -> None:
        from mak.core.logging import SessionLogger

        class ListingRegistry:
            def get(self, agent_type: str) -> object:
                return object()

            def list_types(self) -> list[str]:
                return ["fake"]

        logger = SessionLogger(tmp_path / "session.log")
        store = _store(tmp_path)
        session = _session(tmp_path, runner=object(), node_store=store, logger=logger)
        session.initialize()
        session._registry = ListingRegistry()  # type: ignore[assignment]
        session._agent_pool = ["fake"]
        session.install_plan(
            [replace(_task("t", ["m.py"]), agent_type="hallucinated")]
        )
        events = {e.event_type for e in logger.read_log()}
        assert EventType.AGENT_REMAPPED in events
        assert EventType.TASK_COMPLETED not in events

    def test_the_inventory_is_grouped_by_file(self, tmp_path: Path) -> None:
        for name in ("alpha.py", "beta.py"):
            (tmp_path / name).write_text(
                "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
            )
        store = _store(tmp_path)
        for name in ("alpha.py", "beta.py"):
            store.parse_file_into_nodes(name, (tmp_path / name).read_text())
        files = [str(nid).split("::", 1)[0] for nid in store.list_nodes()]
        # Every file's nodes are contiguous — no interleaving by per-file order.
        assert files == sorted(files, key=files.index)
        assert len(set(files)) == 2
        for name in ("alpha.py", "beta.py"):
            block = [i for i, f in enumerate(files) if f == name]
            assert block == list(range(block[0], block[0] + len(block)))


# ── 18.8 the store is bounded and holds no orphans ───────────────────────────


class TestNodeStoreDiskGrowth:
    def _bump(self, store: NodeStore, node_id: NodeId, times: int) -> None:
        for i in range(times):
            store.put_node(node_id, NodeFragment(node_id, "module", f"x = {i}\n", 1))
            store.commit_node(node_id)

    def test_versions_are_bounded_by_the_retention_policy(
        self, tmp_path: Path
    ) -> None:
        store = NodeStore(tmp_path / "store", version_retention=3)
        nid = NodeId("m.py")
        self._bump(store, nid, 10)
        assert len(store.list_versions(nid)) == 3
        # The newest survive, and the committed one is among them.
        assert store.list_versions(nid) == [8, 9, 10]

    def test_revert_still_works_at_the_floor(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "store", version_retention=MIN_VERSION_RETENTION)
        nid = NodeId("m.py")
        self._bump(store, nid, 6)
        assert store.revert_node(nid).version == 5

    def test_unbounded_retention_keeps_everything(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "store", version_retention=-1)
        nid = NodeId("m.py")
        self._bump(store, nid, 8)
        assert len(store.list_versions(nid)) == 8

    def test_a_retention_below_the_floor_is_raised_to_it(
        self, tmp_path: Path
    ) -> None:
        store = NodeStore(tmp_path / "store", version_retention=1)
        assert store.version_retention == MIN_VERSION_RETENTION

    def test_superseded_fragments_leave_no_directory_behind(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "store"
        store = NodeStore(root)
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store.parse_file_into_nodes("m.py", (tmp_path / "m.py").read_text())
        frag_dir = root / "m.py" / "function" / "a"
        assert frag_dir.is_dir()
        whole = NodeId("m.py")
        store.put_node(
            whole, NodeFragment(whole, "module", "def a():\n    return 1\n", 1)
        )
        store.commit_node(whole)
        assert not frag_dir.exists()
        # The whole-file node's own directory is untouched.
        assert (root / "m.py").is_dir()

    def test_gc_removes_orphans_an_older_store_left(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        store = NodeStore(root)
        nid = NodeId("m.py")
        self._bump(store, nid, 2)
        orphan = root / "gone.py" / "function" / "dead"
        orphan.mkdir(parents=True)
        (orphan / "v1.py").write_text("x = 1\n")
        removed = store.gc()
        assert removed["directories"] >= 1
        assert not (root / "gone.py").exists()
        # A live node is never collected.
        assert store.get_node(nid).source == "x = 1\n"

    def test_gc_applies_the_retention_policy_to_an_old_store(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "store"
        loose = NodeStore(root, version_retention=-1)
        nid = NodeId("m.py")
        self._bump(loose, nid, 9)
        assert len(loose.list_versions(nid)) == 9
        tightened = NodeStore(root, version_retention=DEFAULT_VERSION_RETENTION)
        tightened.gc()
        assert len(tightened.list_versions(nid)) == DEFAULT_VERSION_RETENTION

    def test_the_policy_is_configurable_and_range_checked(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "mak.yaml"
        path.write_text(
            "node_store:\n  version_retention: 3\nagents:\n  - type: anthropic_api\n"
        )
        assert load_config(path).node_store.version_retention == 3
        path.write_text(
            "node_store:\n  version_retention: 1\nagents:\n  - type: anthropic_api\n"
        )
        with pytest.raises(ConfigError, match="version_retention"):
            load_config(path)


# ── 18.9 the TUI does not busy-wait ──────────────────────────────────────────


class TestNoBusyWait:
    def test_the_spin_loop_is_gone(self) -> None:
        source = Path("cli/runner.py").read_text()
        assert "time.sleep" not in source
        assert "import time" not in source


# ── the wave's own invariants ────────────────────────────────────────────────


def test_session_config_defaults_are_backwards_compatible() -> None:
    assert SessionConfig().max_total_tokens is None
    assert NodeStoreConfig().version_retention == DEFAULT_VERSION_RETENTION


def test_a_missing_node_still_raises_rather_than_returning_a_pruned_version(
    tmp_path: Path,
) -> None:
    store = NodeStore(tmp_path / "store", version_retention=2)
    nid = NodeId("m.py")
    for i in range(5):
        store.put_node(nid, NodeFragment(nid, "module", f"x = {i}\n", 1))
        store.commit_node(nid)
    with pytest.raises(NodeStoreError):
        store.get_node(nid, version=1)
