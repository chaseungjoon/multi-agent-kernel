"""Wave 11.4: the conflict detector's false-positive and true-positive corpora.

Nothing in the suite asserted the detector's *false-positive* rate before this
file, only its true positives — which is how a check that rejected ordinary
Python (a class with two ``@staticmethod`` helpers) shipped and failed a real
task three times over.

Two corpora, and both matter:

- ``CORRECT_SOURCES`` — code that is correct and must produce **zero** conflicts.
  Each entry is an idiom the detector previously mis-read, or one adjacent enough
  to it to be worth pinning.
- ``BREAKING_PAIRS`` — genuine incompatibilities that must still be reported, so
  the fixes above are not a blanket weakening of the check.
"""

from __future__ import annotations

import pytest

from mak.conflict_detector.detector import ConflictDetector, EditRound

# The exact source shape that produced conflicts #10-#12 in the codeeditor run:
# a class with two @staticmethod helpers and a ``get`` method whose body calls
# ``self._data.get(...)`` on a plain dict.
REGISTERS = '''\
class Registers:
    """Vim-style named registers."""

    def __init__(self):
        self._data = {}

    @staticmethod
    def is_valid_name(name):
        return len(name) == 1 and name.isalnum()

    @staticmethod
    def _normalise(name):
        return name.lower()

    def get(self, name):
        if not self.is_valid_name(name):
            return ""
        return self._data.get(self._normalise(name), "")

    def set(self, name, value):
        if self.is_valid_name(name):
            self._data[self._normalise(name)] = value
'''

_STDLIB_SHADOWING = '''\
class Buffer:
    """Defines get/append/write/pop — the names stdlib containers also use."""

    def __init__(self):
        self._lines = []
        self._meta = {}

    def get(self, index):
        return self._lines[index]

    def append(self, line):
        self._lines.append(line)
        self._meta.update({"dirty": True})

    def write(self, handle):
        handle.write("\\n".join(self._lines))

    def pop(self):
        self._meta.pop("dirty", None)
        return self._lines.pop()
'''

_TWO_CLASSES_SAME_METHOD = '''\
class Registers:
    def get(self, name):
        return name


class Marks:
    def get(self, name, default):
        return default

    def lookup(self, name):
        return self.get(name, None)
'''

_CLASSMETHOD_AND_STATIC = '''\
class Config:
    def __init__(self, values):
        self.values = values

    @classmethod
    def from_env(cls, env):
        return cls(dict(env))

    @staticmethod
    def normalise(key):
        return key.strip().lower()

    def read(self, key):
        return self.values.get(Config.normalise(key))


def build(env):
    return Config.from_env(env)
'''

_PASSTHROUGH = '''\
def target(a, b, *, c=1):
    return a + b + c


def forward(*args, **kwargs):
    return target(*args, **kwargs)


class Wrapper:
    def __init__(self, inner):
        self._inner = inner

    def run(self, *args, **kwargs):
        return self._inner(*args, **kwargs)
'''

_NESTED_AND_PROPERTIES = '''\
import functools
from dataclasses import dataclass


@dataclass
class Point:
    x: int
    y: int


class Outer:
    class Inner:
        def render(self, width):
            return " " * width

    @property
    def inner(self):
        return Outer.Inner()

    @functools.lru_cache(maxsize=8)
    def cached(self, key, fallback=None):
        return fallback

    def draw(self, width):
        point = Point(1, 2)
        return self.inner.render(width), point.x, self.cached("k")
'''

_CONDITIONAL_IMPORTS = '''\
from typing import TYPE_CHECKING

try:
    import ujson as json
except ImportError:
    import json

if TYPE_CHECKING:
    from collections.abc import Sequence
'''

_ASYNC_AND_OVERRIDE = '''\
from typing import override


class Base:
    async def fetch(self, url, timeout=5):
        return url


class Child(Base):
    @override
    async def fetch(self, url, timeout=5):
        return await super().fetch(url, timeout)
'''

CORRECT_SOURCES: dict[str, str] = {
    "editor/registers.py": REGISTERS,
    "editor/buffer.py": _STDLIB_SHADOWING,
    "editor/two_classes.py": _TWO_CLASSES_SAME_METHOD,
    "editor/config.py": _CLASSMETHOD_AND_STATIC,
    "editor/passthrough.py": _PASSTHROUGH,
    "editor/nested.py": _NESTED_AND_PROPERTIES,
    "editor/imports.py": _CONDITIONAL_IMPORTS,
    "editor/async_override.py": _ASYNC_AND_OVERRIDE,
}

# (definition source, calling source) pairs that are genuinely incompatible.
BREAKING_PAIRS: dict[str, tuple[str, str]] = {
    "too_many_positional": (
        "def render(width):\n    return width\n",
        "def caller():\n    return render(1, 2)\n",
    ),
    "missing_required": (
        "def render(width, height):\n    return width\n",
        "def caller():\n    return render(1)\n",
    ),
    "unknown_keyword": (
        "def render(width):\n    return width\n",
        "def caller():\n    return render(width=1, depth=2)\n",
    ),
    "self_call_arity": (
        "class View:\n    def render(self, width, height):\n        return width\n",
        "class View:\n    def draw(self):\n        return self.render(1)\n",
    ),
    "staticmethod_arity": (
        "class View:\n    @staticmethod\n    def scale(width):\n        return width\n",
        "class View:\n    def draw(self):\n        return self.scale(1, 2)\n",
    ),
    "classmethod_via_class_name": (
        "class View:\n"
        "    @classmethod\n"
        "    def make(cls, width):\n"
        "        return cls\n",
        "def caller():\n    return View.make(1, 2)\n",
    ),
}


def _round(node_id: str, source: str) -> EditRound:
    """One node acting as definition, caller, header, and symbol source at once."""
    return EditRound(
        definitions={node_id: source},
        callers={node_id: source},
        header_edits={node_id: source},
        symbol_edits={node_id: source},
    )


class TestCorrectCodeCorpus:
    """11.4a — correct Python must produce zero conflicts."""

    @pytest.mark.parametrize("node_id", sorted(CORRECT_SOURCES))
    def test_no_conflicts(self, node_id: str) -> None:
        report = ConflictDetector().detect(_round(node_id, CORRECT_SOURCES[node_id]))
        assert report.ok, report.reasons

    def test_whole_corpus_in_one_round(self) -> None:
        # Every file at once: cross-file merging of definitions must not invent
        # conflicts either (``get`` is defined in three of these files).
        report = ConflictDetector().detect(
            EditRound(
                definitions=dict(CORRECT_SOURCES),
                callers=dict(CORRECT_SOURCES),
                header_edits=dict(CORRECT_SOURCES),
                symbol_edits=dict(CORRECT_SOURCES),
            )
        )
        assert report.ok, report.reasons

    @pytest.mark.parametrize(
        "logged_reason",
        [
            "passes 1 positional args but '_normalise' accepts at most 0",
            "passes 1 positional args but 'is_valid_name' accepts at most 0",
            "passes 2 positional args but 'get' accepts at most 1",
        ],
    )
    def test_session_log_strings_are_unreachable(self, logged_reason: str) -> None:
        # The three conflicts that failed `registers_module` three times, verbatim
        # from .mak/session.log. None may be producible from this source again.
        report = ConflictDetector().detect(_round("editor/registers.py", REGISTERS))
        assert not any(logged_reason in reason for reason in report.reasons)

    def test_dedented_method_fragment_is_not_read_as_a_function(self) -> None:
        # How the store actually hands a method to the detector: dedented, so it
        # parses as a module-level ``def get(self, name)``. Framing must restore
        # the class, or ``self`` is counted as a real parameter.
        method = 'def get(self, name):\n    return self._data.get(name, "")\n'
        report = ConflictDetector().detect(
            EditRound(
                definitions={"editor/registers.py::method::Registers.get": method},
                callers={"editor/registers.py::method::Registers.get": method},
            )
        )
        assert report.ok, report.reasons


class TestTruePositivesStillCaught:
    """11.4b — the fixes must not be a blanket weakening of the check."""

    @pytest.mark.parametrize("case", sorted(BREAKING_PAIRS))
    def test_breakage_is_reported(self, case: str) -> None:
        defining, calling = BREAKING_PAIRS[case]
        report = ConflictDetector().detect(
            EditRound(
                definitions={"a.py::function::defs": defining},
                callers={"b.py::function::calls": calling},
            )
        )
        assert report.by_check("signature"), f"{case} went unreported"

    def test_cross_agent_signature_change_is_caught(self) -> None:
        # The behaviour Wave 3 shipped this check for: agent A narrows a
        # signature, agent B's fragment still calls it the old way.
        report = ConflictDetector().detect(
            EditRound(
                definitions={"lib.py::function::save": "def save(path):\n    ...\n"},
                callers={
                    "app.py::function::run": (
                        "def run():\n    return save('a', 'b')\n"
                    )
                },
            )
        )
        (conflict,) = report.by_check("signature")
        assert "save" in conflict.message

    def test_real_name_collision_still_reported(self) -> None:
        report = ConflictDetector().detect(
            EditRound(
                symbol_edits={
                    "m.py::function::a": "def helper():\n    return 1\n",
                    "m.py::function::b": "def helper():\n    return 2\n",
                }
            )
        )
        assert report.by_check("name_collision")

    def test_real_import_conflict_still_reported(self) -> None:
        report = ConflictDetector().detect(
            EditRound(
                header_edits={
                    "m.py::module_header::__header__": "from a import config",
                    "m.py::module_body::__body__": "from b import config",
                }
            )
        )
        assert report.by_check("import")
