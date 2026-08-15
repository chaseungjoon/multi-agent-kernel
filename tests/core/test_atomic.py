"""Atomic writes: a reader never observes a half-written file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mak.core.atomic import write_text_atomic


class TestWriteTextAtomic:
    def test_writes_content(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_text_atomic(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "state.json"
        write_text_atomic(target, "x")
        assert target.read_text(encoding="utf-8") == "x"

    def test_overwrites_in_place(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_text_atomic(target, "old-and-much-longer")
        write_text_atomic(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_text_atomic(target, "x")
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_failed_write_keeps_the_previous_content(self, tmp_path: Path) -> None:
        # The property that matters: the old file is still whole. A plain
        # write_text truncates first, so the same failure would leave nothing.
        target = tmp_path / "state.json"
        write_text_atomic(target, "previous")

        class Unwritable:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        with pytest.raises((RuntimeError, TypeError)):
            write_text_atomic(target, Unwritable())  # type: ignore[arg-type]

        assert target.read_text(encoding="utf-8") == "previous"
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_temp_file_shares_the_targets_directory(self, tmp_path: Path) -> None:
        # os.replace is only atomic within one filesystem, so the temp file must
        # be a sibling of the target rather than in the system temp dir.
        seen: list[str] = []
        real_mkstemp = __import__("tempfile").mkstemp

        def spy(**kwargs: object) -> tuple[int, str]:
            seen.append(str(kwargs.get("dir")))
            return real_mkstemp(**kwargs)  # type: ignore[arg-type]

        import tempfile

        original = tempfile.mkstemp
        tempfile.mkstemp = spy  # type: ignore[assignment]
        try:
            write_text_atomic(tmp_path / "sub" / "state.json", "x")
        finally:
            tempfile.mkstemp = original  # type: ignore[assignment]
        assert seen == [str(tmp_path / "sub")]

    def test_replaces_rather_than_appending(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_text_atomic(target, "a" * 100)
        write_text_atomic(target, "b")
        assert os.path.getsize(target) == 1
