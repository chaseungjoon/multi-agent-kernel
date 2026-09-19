"""Wave 20 foundations: config, API fingerprint, node stamps, lock resources."""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.config import SemanticConfig, load_config
from mak.core.exceptions import ConfigError
from mak.core.types import NodeFragment, NodeId, SubTask
from mak.lock_manager.resources import (
    api_resource,
    base_node,
    is_api_resource,
    is_derived,
    is_key_resource,
    key_resource,
)
from mak.node_store.api_digest import api_fingerprint
from mak.node_store.store import NodeStore, source_digest


def _config(tmp_path: Path, semantic: str) -> Path:
    path = tmp_path / "mak.yaml"
    path.write_text(
        "agents:\n  - type: anthropic_api\n" + semantic, encoding="utf-8"
    )
    return path


class TestSemanticConfig:
    def test_defaults_when_the_section_is_absent(self, tmp_path: Path) -> None:
        config = load_config(_config(tmp_path, ""))
        assert config.semantic == SemanticConfig()
        assert config.semantic.stale_read == "revalidate"
        assert config.semantic.api_locks and config.semantic.registry_keys
        assert not config.semantic.contract_dispatch
        assert config.semantic.type_check == "off"
        assert not config.semantic.impact_tests
        assert config.semantic.adjudicator is None

    def test_every_setting_parses(self, tmp_path: Path) -> None:
        config = load_config(_config(tmp_path, (
            "semantic:\n"
            "  stale_read: redispatch\n"
            "  api_locks: false\n"
            "  intention_locks: false\n"
            "  registry_keys: false\n"
            "  contract_dispatch: true\n"
            "  type_check: mypy\n"
            "  impact_tests: on\n"
            "  import_smoke: 'on'\n"
            "  adjudicator: 'anthropic:claude-haiku-4-5-20251001'\n"
            "  adjudicator_max_calls: 2\n"
            "  gate_timeout_s: 30\n"
            "  impact_max_overlays: 4\n"
        )))
        sem = config.semantic
        assert sem.stale_read == "redispatch"
        assert not sem.api_locks and not sem.intention_locks
        assert not sem.registry_keys and sem.contract_dispatch
        assert sem.type_check == "mypy"
        assert sem.impact_tests and sem.import_smoke
        assert sem.adjudicator == "anthropic:claude-haiku-4-5-20251001"
        assert (sem.adjudicator_max_calls, sem.gate_timeout_s) == (2, 30.0)
        assert sem.impact_max_overlays == 4

    @pytest.mark.parametrize(
        "body",
        [
            "  stale_read: sometimes\n",
            "  type_check: flake8\n",
            "  impact_tests: maybe\n",
            "  adjudicator: haiku\n",
            "  adjudicator: 'nope:model'\n",
            "  gate_timeout_s: 0\n",
            "  adjudicator_max_calls: -1\n",
        ],
    )
    def test_typos_fail_at_load_time(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(ConfigError):
            load_config(_config(tmp_path, "semantic:\n" + body))

    def test_a_non_mapping_section_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_config(_config(tmp_path, "semantic: [1, 2]\n"))

    def test_adjudicator_off_spelled_out(self, tmp_path: Path) -> None:
        config = load_config(_config(tmp_path, "semantic:\n  adjudicator: 'off'\n"))
        assert config.semantic.adjudicator is None


class TestApiFingerprint:
    def test_body_and_docstring_edits_are_invisible(self) -> None:
        old = 'def f(x: int) -> int:\n    """Old doc."""\n    return x\n'
        new = 'def f(x: int) -> int:\n    """New doc."""\n    return x * 2\n'
        assert api_fingerprint(old) == api_fingerprint(new)

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("def f(x):\n    pass\n", "def f(x, y):\n    pass\n"),
            ("def f(x) -> int:\n    pass\n", "def f(x) -> str:\n    pass\n"),
            ("def f(x):\n    pass\n", "@cache\ndef f(x):\n    pass\n"),
            ("def _p(x):\n    pass\n", "def _p():\n    pass\n"),  # private too
            ("class C(A):\n    x: int\n", "class C(B):\n    x: int\n"),
            ("class C:\n    x: int\n", "class C:\n    x: int\n    y: str\n"),
            ("class C:\n    x: int = 0\n", "class C:\n    x: int\n"),
            ("RATE = 0.1\n", "RATE = 10\n"),
            ("import a\n", "import b\n"),
            ("def f():\n    pass\n", "def g():\n    pass\n"),
        ],
    )
    def test_interface_changes_are_visible(self, old: str, new: str) -> None:
        assert api_fingerprint(old) != api_fingerprint(new)

    def test_parameters_false_ignores_only_parameter_shapes(self) -> None:
        old = "def f(x) -> int:\n    pass\n"
        assert api_fingerprint(old, parameters=False) == api_fingerprint(
            "def f(x, *, y=1) -> int:\n    pass\n", parameters=False
        )
        assert api_fingerprint(old, parameters=False) != api_fingerprint(
            "def f(x) -> str:\n    pass\n", parameters=False
        )

    def test_a_class_shell_fragment_is_fingerprinted(self) -> None:
        # The ingestion ``class`` fragment can be just the header line.
        assert api_fingerprint("class C(Base):\n") == "class C(Base):"

    def test_unparseable_source_is_unknown(self) -> None:
        assert api_fingerprint("def f(:\n") is None


class TestNodeStamp:
    def test_stamp_reports_version_and_digest(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "store")
        nid = NodeId("m.py::function::f")
        assert store.stamp(nid) is None
        store.put_node(nid, NodeFragment(nid, "function", "def f():\n    pass\n", 1))
        store.commit_node(nid)
        stamp = store.stamp(nid)
        assert stamp is not None
        assert stamp.version == 1
        assert stamp.digest == source_digest("def f():\n    pass\n")

    def test_the_digest_detects_an_aba_version_reuse(self, tmp_path: Path) -> None:
        # uncommit + recreate restarts at version 1 with different content:
        # the version alone would call the two reads identical.
        store = NodeStore(tmp_path / "store")
        nid = NodeId("m.py::function::f")
        store.put_node(nid, NodeFragment(nid, "function", "def f():\n    pass\n", 1))
        store.commit_node(nid)
        before = store.stamp(nid)
        store.uncommit_node(nid)
        store.put_node(nid, NodeFragment(nid, "function", "def f(x):\n    pass\n", 1))
        store.commit_node(nid)
        after = store.stamp(nid)
        assert before is not None and after is not None
        assert before.version == after.version
        assert before.digest != after.digest


class TestLockResources:
    def test_api_resource_round_trips(self) -> None:
        nid = NodeId("a.py::method::C.get#2")
        res = api_resource(nid)
        assert str(res) == "a.py::method::C.get#2#api"
        assert is_api_resource(res) and is_derived(res)
        assert not is_key_resource(res)
        assert base_node(res) == nid

    def test_key_resource_round_trips_even_for_odd_keys(self) -> None:
        nid = NodeId("r.py::function::_register_all")
        res = key_resource(nid, "/users#api")
        assert is_key_resource(res) and not is_api_resource(res)
        assert base_node(res) == nid

    def test_plain_ids_are_not_derived(self) -> None:
        assert not is_derived("a.py::function::f#2")
        assert base_node("a.py") == NodeId("a.py")


class TestSubTaskDeclarations:
    def test_defaults_are_undeclared(self) -> None:
        task = SubTask("t", "d")
        assert task.changes_api is None
        assert task.api_targets == [] and task.contract == {}
        assert task.registry_keys == {}
