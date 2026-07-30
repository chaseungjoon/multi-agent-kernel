"""Tests for mak.conflict_detector.import_check."""

from __future__ import annotations

from mak.conflict_detector.import_check import (
    check_import_conflicts,
    extract_imports,
)


class TestExtractImports:
    def test_plain_import(self) -> None:
        (rec,) = extract_imports("import os")
        assert rec.binding == "os"
        assert rec.target == "os"

    def test_dotted_import_binds_top(self) -> None:
        (rec,) = extract_imports("import os.path")
        assert rec.binding == "os"
        assert rec.target == "os.path"

    def test_import_as(self) -> None:
        (rec,) = extract_imports("import numpy as np")
        assert rec.binding == "np"
        assert rec.target == "numpy"

    def test_from_import(self) -> None:
        (rec,) = extract_imports("from a.b import c")
        assert rec.binding == "c"
        assert rec.target == "a.b.c"

    def test_from_import_as(self) -> None:
        (rec,) = extract_imports("from a import b as d")
        assert rec.binding == "d"
        assert rec.target == "a.b"

    def test_relative_import(self) -> None:
        (rec,) = extract_imports("from . import thing")
        assert rec.binding == "thing"
        assert rec.target == ".thing"


class TestCheckImportConflicts:
    def test_no_conflict_distinct_bindings(self) -> None:
        edits = {
            "agent_a": "import os",
            "agent_b": "import sys",
        }
        assert check_import_conflicts(edits) == []

    def test_conflicting_binding_to_different_targets(self) -> None:
        edits = {
            "agent_a": "from a import config",
            "agent_b": "from b import config",
        }
        reasons = check_import_conflicts(edits)
        assert len(reasons) == 1
        assert "conflicting import" in reasons[0]
        assert "config" in reasons[0]

    def test_duplicate_same_import_two_agents(self) -> None:
        edits = {
            "agent_a": "import os",
            "agent_b": "import os",
        }
        reasons = check_import_conflicts(edits)
        assert len(reasons) == 1
        assert "duplicate import" in reasons[0]

    def test_same_agent_single_import_is_clean(self) -> None:
        assert check_import_conflicts({"agent_a": "import os\nimport sys"}) == []

    def test_alias_conflict(self) -> None:
        edits = {
            "agent_a": "import numpy as np",
            "agent_b": "import nanopy as np",
        }
        reasons = check_import_conflicts(edits)
        assert reasons and "conflicting import" in reasons[0]
        assert "np" in reasons[0]

    def test_conditional_import_in_one_edit_is_not_a_conflict(self) -> None:
        # Wave 11 audit: a single edit binding one name to two targets is the
        # try/except ImportError idiom, not two agents disagreeing.
        source = (
            "try:\n"
            "    import ujson as json\n"
            "except ImportError:\n"
            "    import json\n"
        )
        assert check_import_conflicts({"m.py::module_header::__header__": source}) == []

    def test_same_binding_in_two_files_is_not_a_conflict(self) -> None:
        # Header edits are compared per file: 'config' meaning different things
        # in two modules is normal, and one task may edit both.
        edits = {
            "a.py::module_header::__header__": "from a import config",
            "b.py::module_header::__header__": "from b import config",
        }
        assert check_import_conflicts(edits) == []

    def test_same_file_disagreement_is_still_a_conflict(self) -> None:
        edits = {
            "a.py::module_header::__header__": "from a import config",
            "a.py::module_body::__body__": "from b import config",
        }
        reasons = check_import_conflicts(edits)
        assert reasons and "conflicting import" in reasons[0]

    def test_conflict_and_duplicate_together(self) -> None:
        edits = {
            "agent_a": "from a import config\nimport os",
            "agent_b": "from b import config\nimport os",
        }
        reasons = check_import_conflicts(edits)
        kinds = {r.split(":")[0] for r in reasons}
        assert "conflicting import" in kinds
        assert "duplicate import" in kinds
