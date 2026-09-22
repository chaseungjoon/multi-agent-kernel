"""Unit tests for the line-range to AST-node mapping."""

from __future__ import annotations

import textwrap

from mining.node_map import empty_index, index_file, whole_file_node_id

SAMPLE = textwrap.dedent(
    '''\
    """Module docstring."""

    import os
    import sys


    CONSTANT = 1


    @decorator
    def top_level(a, b):
        """Doc."""
        return a + b


    class Widget:
        """A widget."""

        attribute = 2

        def method_one(self):
            return 1

        # a comment between methods
        def method_two(self):
            return 2

        OTHER = 3


    def trailing():
        pass
    '''
)


def _ids(source: str) -> list[str]:
    return [span.node_id for span in index_file("m.py", source).spans]


def test_spans_are_ordered_and_non_overlapping() -> None:
    spans = index_file("m.py", SAMPLE).spans
    assert spans, "expected at least one span"
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier.end < later.start
        assert earlier.start <= earlier.end


def test_every_non_blank_line_belongs_to_exactly_one_node() -> None:
    index = index_file("m.py", SAMPLE)
    lines = SAMPLE.splitlines()
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        message = f"line {number} ({text!r}) is unmapped"
        assert index.node_at(number) is not None, message


def test_decorator_line_belongs_to_its_function() -> None:
    index = index_file("m.py", SAMPLE)
    decorator_line = SAMPLE.splitlines().index("@decorator") + 1
    span = index.node_at(decorator_line)
    assert span is not None
    assert span.node_id == "m.py::function::top_level"


def test_methods_are_separate_nodes_from_the_class_shell() -> None:
    ids = _ids(SAMPLE)
    assert "m.py::method::Widget.method_one" in ids
    assert "m.py::method::Widget.method_two" in ids
    assert "m.py::class::Widget" in ids


def test_header_covers_the_imports() -> None:
    index = index_file("m.py", SAMPLE)
    import_line = SAMPLE.splitlines().index("import os") + 1
    span = index.node_at(import_line)
    assert span is not None
    assert span.kind == "module_header"


def test_nodes_in_range_returns_every_intersecting_node() -> None:
    index = index_file("m.py", SAMPLE)
    first = index.spans[0]
    last = index.spans[-1]
    covering = index.nodes_in(first.start, last.end)
    assert len(covering) == len(index.spans)


def test_unparseable_source_falls_back_to_one_whole_file_node() -> None:
    index = index_file("broken.py", "def f(:\n    pass\n")
    assert index.parsed is False
    assert [span.node_id for span in index.spans] == [whole_file_node_id("broken.py")]


def test_empty_index_has_no_spans() -> None:
    index = empty_index("gone.py")
    assert index.spans == ()
    assert index.node_at(1) is None


def test_nested_class_members_attach_to_the_outer_class() -> None:
    source = textwrap.dedent(
        """\
        class Outer:
            class Inner:
                def deep(self):
                    return 1

            def shallow(self):
                return 2
        """
    )
    ids = _ids(source)
    assert "m.py::method::Outer.shallow" in ids
    # The inner class is not a top-level method, so it stays inside the shell.
    assert any(item.endswith("::class::Outer") for item in ids)
