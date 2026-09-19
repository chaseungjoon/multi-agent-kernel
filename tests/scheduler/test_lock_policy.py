"""Wave 20 lock policy: interface, intention and registry-key resources."""

from __future__ import annotations

from mak.core.types import LockMode, NodeId, SubTask
from mak.lock_manager.lock_table import LockTable
from mak.scheduler.lock_policy import (
    LEGACY_POLICY,
    LockPolicy,
    api_write_targets,
    intention_parents,
    lock_requests,
)

F = NodeId("m.py::function::f")
G = NodeId("m.py::function::g")
H = NodeId("n.py::function::h")
TABLE = NodeId("r.py::function::_register_all")


def _task(
    task_id: str,
    targets: list[NodeId],
    context: list[NodeId] | None = None,
    **kw: object,
) -> SubTask:
    return SubTask(task_id, "d", list(targets), list(context or []), **kw)  # type: ignore[arg-type]


def _callees(mapping: dict[NodeId, set[NodeId]]) -> LockPolicy:
    return LockPolicy(
        api_locks=True,
        callees=lambda n: frozenset(mapping.get(n, set())),
    )


class TestLegacy:
    def test_every_flag_off_is_the_pre_wave_20_set(self) -> None:
        task = _task("t", [F, F], [G, F, G])
        assert lock_requests(task, LEGACY_POLICY) == [
            (F, LockMode.WRITE),
            (G, LockMode.READ),
        ]


class TestApiLocks:
    def test_undeclared_task_write_locks_every_interface(self) -> None:
        requests = dict(lock_requests(_task("t", [F, G]), _callees({})))
        assert requests[NodeId(f"{F}#api")] is LockMode.WRITE
        assert requests[NodeId(f"{G}#api")] is LockMode.WRITE

    def test_body_only_task_takes_no_interface_write(self) -> None:
        requests = dict(
            lock_requests(_task("t", [F], changes_api=False), _callees({}))
        )
        assert NodeId(f"{F}#api") not in requests
        assert requests[F] is LockMode.WRITE

    def test_declared_change_narrows_to_api_targets_and_contracts(self) -> None:
        task = _task(
            "t", [F, G, H], changes_api=True, api_targets=[G],
            contract={H: "def h()"},
        )
        assert api_write_targets(task) == [G, H]
        assert api_write_targets(_task("t", [F], changes_api=True)) == [F]

    def test_callers_read_lock_callee_interfaces(self) -> None:
        policy = _callees({F: {H, G}})
        requests = dict(lock_requests(_task("t", [F, G], changes_api=False), policy))
        assert requests[NodeId(f"{H}#api")] is LockMode.READ
        # A callee the task itself writes is not read-locked from outside.
        assert NodeId(f"{G}#api") not in requests

    def test_body_writer_runs_beside_caller_api_change_serializes(self) -> None:
        table = LockTable()
        policy = _callees({F: {H}})
        caller = _task("caller", [F], changes_api=False)
        body_writer = _task("body", [H], changes_api=False)
        api_writer = _task("api", [H], changes_api=True)
        assert table.try_acquire_all(lock_requests(caller, policy), "caller")
        assert table.try_acquire_all(lock_requests(body_writer, policy), "body")
        table.release_all("body")
        assert not table.try_acquire_all(lock_requests(api_writer, policy), "api")
        table.release_all("caller")
        assert table.try_acquire_all(lock_requests(api_writer, policy), "api")

    def test_whole_file_interface_write_covers_its_fragments(self) -> None:
        whole = NodeId("m.py")
        policy = LockPolicy(api_locks=True, fragments_of=lambda n: [F, G])
        requests = dict(lock_requests(_task("t", [whole]), policy))
        for node in (whole, F, G):
            assert requests[NodeId(f"{node}#api")] is LockMode.WRITE


class TestIntentionLocks:
    def test_parents(self) -> None:
        assert intention_parents(NodeId("a/m.py::method::C.get#2")) == [
            NodeId("a/m.py"), NodeId("a/m.py::class::C"),
        ]
        assert intention_parents(NodeId("m.py::class_body::C#1")) == [
            NodeId("m.py"), NodeId("m.py::class::C"),
        ]
        assert intention_parents(F) == [NodeId("m.py")]
        assert intention_parents(NodeId("m.py")) == []

    def test_whole_file_writer_excludes_fragment_writers(self) -> None:
        table = LockTable()
        policy = LockPolicy(intention_locks=True)
        a = _task("a", [F])
        b = _task("b", [G])
        whole = _task("w", [NodeId("m.py")])
        assert table.try_acquire_all(lock_requests(a, policy), "a")
        assert table.try_acquire_all(lock_requests(b, policy), "b")
        assert not table.try_acquire_all(lock_requests(whole, policy), "w")
        table.release_all("a")
        table.release_all("b")
        assert table.try_acquire_all(lock_requests(whole, policy), "w")

    def test_whole_class_writer_excludes_method_writers(self) -> None:
        table = LockTable()
        policy = LockPolicy(intention_locks=True)
        method = _task("m", [NodeId("m.py::method::C.get")])
        klass = _task("k", [NodeId("m.py::class::C")])
        assert table.try_acquire_all(lock_requests(method, policy), "m")
        assert not table.try_acquire_all(lock_requests(klass, policy), "k")

    def test_strongest_mode_wins_on_overlap(self) -> None:
        policy = LockPolicy(intention_locks=True)
        task = _task("t", [NodeId("m.py::class::C"), NodeId("m.py::method::C.get")])
        requests = dict(lock_requests(task, policy))
        assert requests[NodeId("m.py::class::C")] is LockMode.WRITE


class TestRegistryKeys:
    def _policy(self) -> LockPolicy:
        return LockPolicy(registry_keys=True, commutative=lambda t, n: n == TABLE)

    def test_appenders_of_different_keys_co_hold_the_table(self) -> None:
        table = LockTable()
        a = _task("a", [F, TABLE], registry_keys={TABLE: ["/a"]})
        b = _task("b", [G, TABLE], registry_keys={TABLE: ["/b"]})
        assert table.try_acquire_all(lock_requests(a, self._policy()), "a")
        assert table.try_acquire_all(lock_requests(b, self._policy()), "b")

    def test_same_key_serializes(self) -> None:
        table = LockTable()
        a = _task("a", [TABLE], registry_keys={TABLE: ["/users"]})
        b = _task("b", [TABLE], registry_keys={TABLE: ["/users"]})
        assert table.try_acquire_all(lock_requests(a, self._policy()), "a")
        assert not table.try_acquire_all(lock_requests(b, self._policy()), "b")

    def test_non_commutative_targets_keep_the_node_lock(self) -> None:
        requests = dict(lock_requests(_task("t", [F]), self._policy()))
        assert requests[F] is LockMode.WRITE

    def test_flag_off_ignores_commutativity(self) -> None:
        policy = LockPolicy(commutative=lambda t, n: True)
        assert dict(lock_requests(_task("t", [TABLE]), policy))[TABLE] is (
            LockMode.WRITE
        )
