"""Wave 20 static checks: true positives and the explicitly skipped cases."""

from __future__ import annotations

import pytest

from mak.conflict_detector.attribute_check import check_module_attributes
from mak.conflict_detector.constructor_check import check_constructors
from mak.conflict_detector.cycle_check import check_new_cycles
from mak.conflict_detector.detector import ConflictDetector, EditRound
from mak.conflict_detector.duplicate_check import CreatedFunction, check_duplicates
from mak.conflict_detector.module_index import ModuleIndex
from mak.conflict_detector.override_check import check_overrides
from mak.conflict_detector.registry_key_check import check_registry_keys


def _kinds(defects: list[object]) -> list[str]:
    return [d.kind for d in defects]  # type: ignore[attr-defined]


def _attr(sources: dict[str, str], scope: set[str] | None = None) -> list[str]:
    index = ModuleIndex(sources)
    return _kinds(check_module_attributes(index, frozenset(scope or sources)))


class TestAttributeCheck:
    HELPERS = "def make_slug(t):\n    return t\n"

    def test_deleted_name_via_module_alias(self) -> None:
        sources = {
            "pkg/helpers.py": self.HELPERS,
            "pkg/report.py": "import pkg.helpers as helpers\n\n"
            "def build(t):\n    return helpers.slugify(t)\n",
        }
        assert _attr(sources, {"pkg/report.py"}) == ["unresolved_attribute"]

    def test_from_package_import_module(self) -> None:
        sources = {
            "pkg/helpers.py": self.HELPERS,
            "pkg/report.py": "from pkg import helpers\n\nx = helpers.slugify\n",
        }
        assert _attr(sources) == ["unresolved_attribute"]

    def test_defined_name_is_clean(self) -> None:
        sources = {
            "helpers.py": self.HELPERS,
            "report.py": "import helpers\n\nx = helpers.make_slug('a')\n",
        }
        assert _attr(sources) == []

    @pytest.mark.parametrize(
        "defining",
        [
            "def __getattr__(name):\n    return name\n",
            "from other import *\n",
            "globals()['slugify'] = 1\n",
            "def f(:\n",
            "try:\n    from fast import slugify\n"
            "except ImportError:\n    slugify = None\n",
            "if True:\n    def slugify(t):\n        return t\n",
        ],
    )
    def test_dynamic_or_conditional_modules_are_skipped(self, defining: str) -> None:
        sources = {
            "helpers.py": defining,
            "report.py": "import helpers\n\nx = helpers.slugify\n",
        }
        assert _attr(sources) == []

    def test_rebound_local_is_skipped(self) -> None:
        sources = {
            "helpers.py": self.HELPERS,
            "report.py":
                "import helpers\n\ndef f(helpers):\n    return helpers.slugify\n",
        }
        assert _attr(sources) == []

    def test_store_context_and_dunders_are_skipped(self) -> None:
        sources = {
            "helpers.py": self.HELPERS,
            "report.py":
                "import helpers\n\nhelpers.slugify = 1\nx = helpers.__name__\n",
        }
        assert _attr(sources) == []

    def test_third_party_module_is_skipped(self) -> None:
        sources = {"report.py": "import os\n\nx = os.nonexistent\n"}
        assert _attr(sources) == []

    def test_out_of_scope_file_is_not_judged(self) -> None:
        sources = {
            "helpers.py": self.HELPERS,
            "report.py": "import helpers\n\nx = helpers.slugify\n",
        }
        assert _attr(sources, {"helpers.py"}) == []


def _overrides(sources: dict[str, str], scope: set[str] | None = None) -> list[str]:
    return _kinds(check_overrides(ModuleIndex(sources), frozenset(scope or sources)))


class TestOverrideCheck:
    BASE = "class Base:\n    def save(self, x):\n        pass\n"

    def test_override_dropping_a_parameter(self) -> None:
        sources = {
            "base.py": self.BASE,
            "child.py": "from base import Base\n\n"
            "class Child(Base):\n    def save(self):\n        pass\n",
        }
        assert _overrides(sources) == ["override_mismatch"]

    def test_base_change_checks_untouched_subclasses(self) -> None:
        sources = {
            "base.py": self.BASE,
            "child.py": "from base import Base\n\n"
            "class Child(Base):\n    def save(self):\n        pass\n",
        }
        assert _overrides(sources, {"base.py"}) == ["override_mismatch"]
        assert _overrides(sources, {"other.py"}) == []

    @pytest.mark.parametrize(
        "method",
        [
            "def save(self, x, y=None):\n        pass\n",
            "def save(self, *args):\n        pass\n",
            "def save(self, y):\n        pass\n",  # renamed positional: skipped
            "@staticmethod\n    def save(x):\n        pass\n",  # receiver mismatch
            "@cached\n    def save(self):\n        pass\n",  # unknown decorator
        ],
    )
    def test_compatible_or_unknowable_overrides(self, method: str) -> None:
        sources = {
            "m.py": self.BASE + f"\n\nclass Child(Base):\n    {method}",
        }
        assert _overrides(sources) == []

    def test_new_required_keyword_is_reported(self) -> None:
        sources = {"m.py": self.BASE + (
            "\n\nclass Child(Base):\n    def save(self, x, *, force):\n        pass\n"
        )}
        assert _overrides(sources) == ["override_mismatch"]

    def test_dropped_kwargs_is_reported(self) -> None:
        sources = {"m.py": (
            "class Base:\n    def run(self, **kw):\n        pass\n\n\n"
            "class Child(Base):\n    def run(self):\n        pass\n"
        )}
        assert _overrides(sources) == ["override_mismatch"]

    def test_init_and_external_bases_are_skipped(self) -> None:
        sources = {"m.py": (
            "class Base:\n    def __init__(self, a):\n        pass\n\n\n"
            "class Child(Base):\n    def __init__(self):\n        pass\n\n\n"
            "class E(Exception):\n    def with_traceback(self):\n        pass\n"
        )}
        assert _overrides(sources) == []

    def test_grandparent_definition_is_found(self) -> None:
        sources = {"m.py": self.BASE + (
            "\n\nclass Mid(Base):\n    pass\n\n\n"
            "class Leaf(Mid):\n    def save(self):\n        pass\n"
        )}
        assert _overrides(sources) == ["override_mismatch"]


def _ctor(sources: dict[str, str], scope: set[str] | None = None) -> list[str]:
    return _kinds(check_constructors(ModuleIndex(sources), frozenset(scope or sources)))


class TestConstructorCheck:
    ORDER = (
        "from dataclasses import dataclass, field\n\n\n"
        "@dataclass\nclass Order:\n    id: int\n    total: int\n    currency: str\n"
    )

    def test_new_required_dataclass_field(self) -> None:
        sources = {
            "orders.py": self.ORDER,
            "shop.py": "from orders import Order\n\nx = Order(1, 100)\n",
        }
        assert _ctor(sources) == ["constructor_mismatch"]

    def test_class_change_checks_untouched_callers(self) -> None:
        sources = {
            "orders.py": self.ORDER,
            "shop.py": "from orders import Order\n\nx = Order(1, 100)\n",
        }
        assert _ctor(sources, {"orders.py"}) == ["constructor_mismatch"]

    @pytest.mark.parametrize(
        "call",
        [
            "Order(1, 100, 'EUR')",
            "Order(id=1, total=100, currency='EUR')",
            "Order(*args)",
            "Order(**kw)",
        ],
    )
    def test_valid_calls(self, call: str) -> None:
        sources = {
            "orders.py": self.ORDER,
            "shop.py": f"from orders import Order\n\nx = {call}\n",
        }
        assert _ctor(sources) == []

    def test_defaults_classvar_init_false_and_kw_only(self) -> None:
        source = (
            "from dataclasses import dataclass, field, KW_ONLY\n"
            "from typing import ClassVar\n\n\n"
            "@dataclass\nclass P:\n"
            "    a: int\n    b: int = 0\n    c: list = field(default_factory=list)\n"
            "    d: int = field(init=False)\n    e: ClassVar[int] = 1\n"
            "    _: KW_ONLY\n    f: int\n"
        )
        ok = {"p.py": source + "\nx = P(1, f=2)\ny = P(1, 2, [], f=3)\n"}
        bad = {"p.py": source + "\nx = P(1)\n"}
        assert _ctor(ok) == []
        assert _ctor(bad) == ["constructor_mismatch"]

    def test_explicit_init_and_inheritance(self) -> None:
        sources = {"m.py": (
            "class A:\n    def __init__(self, x, y):\n        pass\n\n\n"
            "class B(A):\n    pass\n\n\nb = B(1)\n"
        )}
        assert _ctor(sources) == ["constructor_mismatch"]

    def test_plain_class_takes_no_arguments(self) -> None:
        assert _ctor({"m.py": "class A:\n    pass\n\n\na = A(1)\n"}) == [
            "constructor_mismatch"
        ]

    @pytest.mark.parametrize(
        "cls",
        [
            "class A(Exception):\n    pass\n",  # external base
            "class A(metaclass=M):\n    pass\n",
            "class A:\n    def __new__(cls, *a):\n        pass\n",
            "@attr.s\nclass A:\n    x: int\n",
            "class A(B, C):\n    pass\n\n\n"
            "class B:\n    pass\n\n\nclass C:\n    pass\n",
        ],
    )
    def test_unknowable_constructors_are_skipped(self, cls: str) -> None:
        assert _ctor({"m.py": cls + "\n\na = A(1, 2, 3)\n"}) == []

    def test_shadowed_class_name_is_skipped(self) -> None:
        sources = {
            "orders.py": self.ORDER,
            "shop.py": "from orders import Order\n\n"
            "def f(Order):\n    return Order(1)\n",
        }
        assert _ctor(sources) == []


class TestRegistryKeyCheck:
    TABLE = 'def _r() -> None:\n    register("/a", a)\n'

    def test_duplicate_introduced_by_the_edit(self) -> None:
        edit = self.TABLE + '    register("/a", b)\n'
        reasons = check_registry_keys({"n": edit}, {"n": self.TABLE})
        assert len(reasons) == 1 and "'/a'" in reasons[0]

    def test_pre_existing_duplicate_is_not_this_edits_fault(self) -> None:
        dup = self.TABLE + '    register("/a", b)\n'
        edit = dup + '    register("/c", c)\n'
        assert check_registry_keys({"n": edit}, {"n": dup}) == []

    def test_non_registrars_are_ignored(self) -> None:
        body = 'def f():\n    print("x")\n    print("x")\n    y = 1\n'
        assert check_registry_keys({"n": body}) == []

    def test_detector_reports_it_as_a_registry_key_conflict(self) -> None:
        edit = self.TABLE + '    register("/a", b)\n'
        report = ConflictDetector().detect(
            EditRound(registry_edits={"r.py::function::_r": edit},
                      previous={"r.py::function::_r": self.TABLE})
        )
        assert [c.check for c in report.conflicts] == ["registry_key"]


class TestCycleCheck:
    def test_new_from_import_cycle(self) -> None:
        before = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
        after = {
            "a.py": "from b import y\nx = 1\n",
            "b.py": "from a import x\ny = 2\n",
        }
        defects = check_new_cycles(before, after, frozenset({"a.py"}))
        assert _kinds(defects) == ["import_cycle"]

    def test_existing_cycle_is_not_new(self) -> None:
        cyclic = {
            "a.py": "from b import y\nx = 1\n",
            "b.py": "from a import x\ny = 2\n",
        }
        assert check_new_cycles(cyclic, cyclic, frozenset({"a.py"})) == []

    @pytest.mark.parametrize(
        "a_src",
        [
            "import b\nx = 1\n",  # module import only: works at runtime
            "def f():\n    from b import y\n    return y\n",  # function-local
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from b import y\n",
        ],
    )
    def test_benign_cycles_are_skipped(self, a_src: str) -> None:
        before = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
        after = {"a.py": a_src, "b.py": "import a\ny = 2\n"}
        assert check_new_cycles(before, after, frozenset({"a.py", "b.py"})) == []

    def test_cycle_outside_scope_is_not_reported(self) -> None:
        after = {
            "a.py": "from b import y\nx = 1\n",
            "b.py": "from a import x\ny = 2\n",
        }
        assert check_new_cycles({}, after, frozenset({"c.py"})) == []


class TestDuplicateCheck:
    NORM = "def _normalize_email(e):\n    return e.strip().lower()\n"

    def test_same_function_from_two_tasks(self) -> None:
        created = [
            CreatedFunction("users.py", "_normalize_email", self.NORM, "a"),
            CreatedFunction(
                "contacts.py", "_normalize_email",
                'def _normalize_email(e):\n    """Doc."""\n'
                "    return e.strip().lower()\n",
                "b",
            ),
        ]
        assert _kinds(check_duplicates(created)) == ["duplicate_implementation"]

    @pytest.mark.parametrize(
        ("second", "task", "file"),
        [
            ("def _normalize_email(e):\n    return e.upper()\n", "b", "c.py"),
            ("def _normalize_email(e):\n    return e.strip().lower()\n", "a", "c.py"),
            (
                "def _normalize_email(e):\n    return e.strip().lower()\n",
                "b", "users.py",
            ),
        ],
    )
    def test_different_body_same_task_or_same_file_is_skipped(
        self, second: str, task: str, file: str
    ) -> None:
        created = [
            CreatedFunction("users.py", "_normalize_email", self.NORM, "a"),
            CreatedFunction(file, "_normalize_email", second, task),
        ]
        assert check_duplicates(created) == []

    def test_conventional_names_are_skipped(self) -> None:
        body = "def main():\n    return run()\n"
        created = [
            CreatedFunction("a.py", "main", body, "a"),
            CreatedFunction("b.py", "main", body, "b"),
        ]
        assert check_duplicates(created) == []
