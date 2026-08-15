"""The escape corpus: node ids that must never become a filesystem write."""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.core.exceptions import UnsafeNodeIdError
from mak.core.paths import (
    check_node_id,
    node_file_path,
    safe_path_under,
    unsafe_node_id_reason,
)

# Every one of these is a well-formed Python path that resolves outside the tree
# MAK may write to. The ".py" rule the planner already applied accepts all of
# them, which is exactly why this list exists.
ESCAPES = [
    "/etc/cron.d/payload.py",
    "/tmp/anywhere.py",
    "../escaped.py",
    "../../../.ssh/authorized_keys.py",
    "pkg/../../out.py",
    "~/.bashrc.py",
    "~root/x.py",
    r"C:\Windows\System32\x.py",
    r"C:x.py",
    r"\\server\share\x.py",
    ".mak/node_store/x.py",
    "pkg/.mak/x.py",
    "",
    "   ",
]

SAFE = [
    "a.py",
    "pkg/mod.py",
    "pkg/sub/mod.py",
    "a.py::function::f",
    "pkg/mod.py::method::C.m",
    "pkg/mod.py::module_header::__header__",
    "./a.py",
    "a.py::function::f#2",
    # A directory merely *named* like a traversal is not one.
    "pkg/..hidden/x.py",
]


class TestLexicalCheck:
    @pytest.mark.parametrize("node_id", ESCAPES)
    def test_escaping_ids_are_refused(self, node_id: str) -> None:
        assert unsafe_node_id_reason(node_id) is not None
        with pytest.raises(UnsafeNodeIdError):
            check_node_id(node_id)

    @pytest.mark.parametrize("node_id", SAFE)
    def test_ordinary_ids_pass(self, node_id: str) -> None:
        assert unsafe_node_id_reason(node_id) is None
        check_node_id(node_id)

    def test_reason_names_the_offending_path(self) -> None:
        reason = unsafe_node_id_reason("/etc/x.py")
        assert reason is not None
        assert "/etc/x.py" in reason
        assert "absolute" in reason

    def test_the_check_reads_the_file_component_not_the_whole_id(self) -> None:
        # The kind/name segments are not a path and must not be scanned for "..".
        assert node_file_path("a.py::function::f") == "a.py"
        assert unsafe_node_id_reason("a.py::function::..") is None

    def test_mak_dir_rule_is_opt_out(self) -> None:
        # The node store disables it so the Wave 11 prune can address — and
        # therefore delete — nodes an older MAK ingested from its own store.
        assert unsafe_node_id_reason(".mak/x.py") is not None
        assert unsafe_node_id_reason(".mak/x.py", mak_dir_name=None) is None
        # Containment still applies with the rule off.
        assert unsafe_node_id_reason("../x.py", mak_dir_name=None) is not None

    def test_a_renamed_mak_dir_is_honored(self) -> None:
        assert unsafe_node_id_reason("state/x.py", mak_dir_name="state") is not None


class TestResolvedContainment:
    def test_a_path_inside_the_root_resolves(self, tmp_path: Path) -> None:
        resolved = safe_path_under(tmp_path, "pkg/mod.py")
        assert resolved == (tmp_path / "pkg" / "mod.py").resolve()

    def test_the_root_need_not_exist_yet(self, tmp_path: Path) -> None:
        root = tmp_path / "not-created"
        assert safe_path_under(root, "a.py") == (root / "a.py").resolve()

    @pytest.mark.parametrize("relative", ["../out.py", "/etc/x.py", "a/../../b.py"])
    def test_escapes_are_refused(self, tmp_path: Path, relative: str) -> None:
        with pytest.raises(UnsafeNodeIdError):
            safe_path_under(tmp_path, relative)

    def test_a_symlinked_directory_cannot_smuggle_a_write_out(
        self, tmp_path: Path
    ) -> None:
        # The case the lexical check provably cannot catch: every component is
        # an ordinary name, and only resolution reveals the escape.
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "project"
        root.mkdir()
        (root / "vendor").symlink_to(outside, target_is_directory=True)

        assert unsafe_node_id_reason("vendor/x.py") is None  # lexically fine
        with pytest.raises(UnsafeNodeIdError, match="outside"):
            safe_path_under(root, "vendor/x.py")

    def test_error_names_both_paths(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeNodeIdError) as excinfo:
            safe_path_under(tmp_path, "../out.py")
        message = str(excinfo.value)
        assert "out.py" in message and str(tmp_path.resolve()) in message
