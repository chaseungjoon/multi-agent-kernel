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


class TestStrictModuleResolution:
    """A gate may not guess which file an import names (Wave 16)."""

    def test_third_party_tail_collision_is_not_judged(self) -> None:
        # `from PyInstaller.__main__ import run` resolved to the repo's own
        # editor/__main__.py under last-segment matching, and the correct import
        # was reported as a defect.
        sources = {
            "editor/__main__.py": "from editor.main import main\n",
            "editor/main.py": "def main():\n    return 0\n",
            "tools/build_binary.py": (
                "from PyInstaller.__main__ import run as pyinstaller_run\n\n\n"
                "def build():\n    return pyinstaller_run([])\n"
            ),
        }
        assert _check(sources, "tools/build_binary.py") == []

    def test_in_repo_import_still_resolves(self) -> None:
        sources = {
            "editor/__main__.py": (
                "from editor.main import main\n\n\n"
                "def go():\n    return main(['x'])\n"
            ),
            "editor/main.py": "def main():\n    return 0\n",
        }
        assert _check(sources, "editor/__main__.py") == ["signature_mismatch"]

    def test_src_layout_resolves_by_full_tail(self) -> None:
        # `pkg.mod` must still find `src/pkg/mod.py` — strict means the whole
        # dotted tail matches, not that the path is literally the dotted path.
        sources = {
            "src/pkg/alpha.py": _ALPHA,
            "src/pkg/beta.py": (
                "from pkg.alpha import f\n\n\ndef g():\n    return f(1)\n"
            ),
        }
        assert _check(sources, "src/pkg/beta.py") == ["signature_mismatch"]

    def test_same_tail_in_two_places_is_ambiguous_and_skipped(self) -> None:
        sources = {
            "a/pkg/mod.py": "def f(x):\n    return x\n",
            "b/pkg/mod.py": "def f(x, y):\n    return x\n",
            "caller.py": "from pkg.mod import f\n\n\ndef g():\n    return f()\n",
        }
        assert _check(sources, "caller.py") == []
