"""Tests for the post-wave cross-module API check (Wave 13, step 5)."""

from __future__ import annotations

from mak.conflict_detector.cross_module_check import check_cross_module_api

_ALPHA = "def f(a, b):\n    return a + b\n"


def _check(sources: dict[str, str], *scope: str) -> list[str]:
    return [
        d.kind for d in check_cross_module_api(sources, frozenset(scope))
    ]


class TestSignatureMismatch:
    def test_too_few_arguments_across_modules(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": "from alpha import f\n\n\ndef g():\n    return f(1)\n",
        }
        assert _check(sources, "beta.py") == ["signature_mismatch"]

    def test_matching_call_is_clean(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": "from alpha import f\n\n\ndef g():\n    return f(1, 2)\n",
        }
        assert _check(sources, "beta.py") == []

    def test_aliased_import_is_checked(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": "from alpha import f as ff\n\n\ndef g():\n    return ff(1)\n",
        }
        assert _check(sources, "beta.py") == ["signature_mismatch"]

    def test_module_attribute_call_is_checked(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": "import alpha\n\n\ndef g():\n    return alpha.f(1)\n",
        }
        assert _check(sources, "beta.py") == ["signature_mismatch"]

    def test_relative_import_resolves(self) -> None:
        sources = {
            "pkg/alpha.py": _ALPHA,
            "pkg/beta.py": (
                "from .alpha import f\n\n\ndef g():\n    return f(1)\n"
            ),
        }
        assert _check(sources, "pkg/beta.py") == ["signature_mismatch"]

    def test_local_definition_shadows_the_import(self) -> None:
        # The file defines its own f, so the call does not provably reach alpha's.
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": (
                "from alpha import f\n\n\n"
                "def f(a):\n    return a\n\n\n"
                "def g():\n    return f(1)\n"
            ),
        }
        assert _check(sources, "beta.py") == []

    def test_external_import_is_never_judged(self) -> None:
        sources = {
            "beta.py": "from requests import get\n\n\ndef g():\n    return get()\n",
        }
        assert _check(sources, "beta.py") == []


class TestUnresolvedImport:
    def test_importing_an_undefined_name(self) -> None:
        sources = {
            "recent.py": "class RecentFiles:\n    pass\n",
            "home.py": "from recent import load_recent\n",
        }
        assert _check(sources, "home.py") == ["unresolved_import"]

    def test_reexported_name_resolves(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "recent.py": "from alpha import f\n",
            "home.py": "from recent import f\n\n\ndef g():\n    return f(1, 2)\n",
        }
        assert _check(sources, "home.py") == []

    def test_importing_a_submodule_is_not_a_defect(self) -> None:
        sources = {
            "pkg/__init__.py": "",
            "pkg/alpha.py": _ALPHA,
            "beta.py": "from pkg import alpha\n",
        }
        assert _check(sources, "beta.py") == []

    def test_star_import_is_ignored(self) -> None:
        sources = {"alpha.py": _ALPHA, "beta.py": "from alpha import *\n"}
        assert _check(sources, "beta.py") == []


class TestScope:
    def test_files_outside_the_scope_are_not_judged(self) -> None:
        sources = {
            "alpha.py": _ALPHA,
            "beta.py": "from alpha import f\n\n\ndef g():\n    return f(1)\n",
        }
        assert _check(sources, "alpha.py") == []

    def test_unparseable_file_yields_nothing(self) -> None:
        # The parse gate already owns this failure; reporting it twice would only
        # send the operator to a check that is not the one that can explain it.
        sources = {"alpha.py": _ALPHA, "beta.py": "def (:\n"}
        assert _check(sources, "beta.py") == []
