"""Unit tests for the unified-diff parser."""

from __future__ import annotations

import textwrap

from mining.diff_parse import parse_diff

MODIFY = textwrap.dedent(
    """\
    diff --git a/pkg/mod.py b/pkg/mod.py
    index 1111111..2222222 100644
    --- a/pkg/mod.py
    +++ b/pkg/mod.py
    @@ -10,2 +10,3 @@ def f():
    -old
    -old
    +new
    +new
    +new
    @@ -40 +41 @@
    -x
    +y
    """
)

ADD_DELETE_RENAME = textwrap.dedent(
    """\
    diff --git a/new.py b/new.py
    new file mode 100644
    --- /dev/null
    +++ b/new.py
    @@ -0,0 +1,3 @@
    +a
    +b
    +c
    diff --git a/gone.py b/gone.py
    deleted file mode 100644
    --- a/gone.py
    +++ /dev/null
    @@ -1,2 +0,0 @@
    -a
    -b
    diff --git a/old/name.py b/new/name.py
    similarity index 95%
    rename from old/name.py
    rename to new/name.py
    --- a/old/name.py
    +++ b/new/name.py
    @@ -3 +3 @@
    -a
    +b
    diff --git a/img.png b/img.png
    index 3333333..4444444 100644
    Binary files a/img.png and b/img.png differ
    """
)


def test_hunk_ranges_use_explicit_and_implicit_counts() -> None:
    files = parse_diff(MODIFY)
    assert len(files) == 1
    hunks = files[0].hunks
    assert (hunks[0].old_start, hunks[0].old_count) == (10, 2)
    assert (hunks[0].new_start, hunks[0].new_count) == (10, 3)
    # "@@ -40 +41 @@" means one line on each side.
    assert (hunks[1].old_start, hunks[1].old_count) == (40, 1)
    assert (hunks[1].new_start, hunks[1].new_count) == (41, 1)


def test_added_file_has_no_old_path() -> None:
    added = parse_diff(ADD_DELETE_RENAME)[0]
    assert added.old_path is None
    assert added.new_path == "new.py"
    assert added.path == "new.py"


def test_deleted_file_has_no_new_path() -> None:
    deleted = parse_diff(ADD_DELETE_RENAME)[1]
    assert deleted.new_path is None
    assert deleted.old_path == "gone.py"
    assert deleted.path == "gone.py"


def test_rename_keeps_both_paths() -> None:
    renamed = parse_diff(ADD_DELETE_RENAME)[2]
    assert renamed.renamed
    assert renamed.old_path == "old/name.py"
    assert renamed.new_path == "new/name.py"
    assert renamed.path == "new/name.py"


def test_binary_file_is_flagged_and_has_no_hunks() -> None:
    binary = parse_diff(ADD_DELETE_RENAME)[3]
    assert binary.binary
    assert binary.hunks == ()


def test_pure_insertion_has_zero_old_count() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/m.py b/m.py
        --- a/m.py
        +++ b/m.py
        @@ -7,0 +8,2 @@
        +one
        +two
        """
    )
    hunk = parse_diff(diff)[0].hunks[0]
    assert hunk.old_count == 0
    assert hunk.old_start == 7
    assert (hunk.new_start, hunk.new_count) == (8, 2)
