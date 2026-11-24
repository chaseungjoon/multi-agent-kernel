"""Wave 28: merging import statements into a module header."""

from __future__ import annotations

import pytest

from mak.semantic.import_merge import added_imports, merge_imports

HEADER = '"""Doc."""\nimport os\n# keep me\nfrom typing import Any\n\nLIMIT = 3\n'


class TestMergeImports:
    def test_new_imports_land_after_the_last_import(self) -> None:
        plan = merge_imports(HEADER, ["import json", "from re import compile"])
        assert plan.source == (
            '"""Doc."""\nimport os\n# keep me\nfrom typing import Any\n'
            "import json\nfrom re import compile\n\nLIMIT = 3\n"
        )
        assert plan.added == ("import json", "from re import compile")
        assert plan.names == ("json", "compile")

    def test_an_identical_import_is_skipped(self) -> None:
        plan = merge_imports(HEADER, ["import os", "from typing import Any"])
        assert plan.source is None and plan.conflicts == ()
        assert plan.skipped == ("import os", "from typing import Any")

    def test_only_the_new_names_of_a_from_import_are_added(self) -> None:
        plan = merge_imports(HEADER, ["from typing import Any, cast"])
        assert plan.added == ("from typing import cast",)

    def test_a_name_bound_to_another_target_conflicts(self) -> None:
        plan = merge_imports(HEADER, ["from json import Any"])
        assert plan.source is None
        assert plan.conflicts and "Any" in plan.conflicts[0]

    def test_a_conflict_merges_nothing(self) -> None:
        plan = merge_imports(HEADER, ["import json", "from json import Any"])
        assert plan.source is None and plan.added == ()

    def test_an_import_shadowing_a_definition_conflicts(self) -> None:
        assert merge_imports(HEADER, ["from x import LIMIT"]).conflicts
        assert merge_imports(HEADER, ["from x import f"], reserved=["f"]).conflicts

    def test_aliases_bind_their_alias(self) -> None:
        plan = merge_imports(HEADER, ["import numpy as np"])
        assert plan.names == ("np",)
        assert merge_imports(HEADER, ["import json as os"]).conflicts

    def test_a_submodule_of_a_bound_package_is_added(self) -> None:
        plan = merge_imports("import os\n", ["import os.path"])
        assert plan.source == "import os\nimport os.path\n"

    def test_future_imports_go_after_the_docstring_and_first(self) -> None:
        plan = merge_imports(HEADER, ["from __future__ import annotations"])
        assert plan.source is not None
        assert plan.source.startswith(
            '"""Doc."""\nfrom __future__ import annotations\nimport os\n'
        )

    def test_an_empty_header_gets_the_import(self) -> None:
        assert merge_imports("", ["import os"]).source == "import os\n"

    def test_a_header_with_no_imports_keeps_its_docstring_first(self) -> None:
        plan = merge_imports('"""Doc."""\n\nX = 1\n', ["import os"])
        assert plan.source is not None
        assert plan.source.index('"""Doc."""') < plan.source.index("import os")
        assert plan.source.index("import os") < plan.source.index("X = 1")

    def test_a_header_that_does_not_parse_is_a_conflict(self) -> None:
        assert merge_imports("import (\n", ["import os"]).conflicts

    def test_merging_is_order_independent(self) -> None:
        a = ["import json"]
        b = ["import re"]
        one = merge_imports(merge_imports(HEADER, a).source or "", b).source or ""
        two = merge_imports(merge_imports(HEADER, b).source or "", a).source or ""
        assert sorted(one.splitlines()) == sorted(two.splitlines())

    @pytest.mark.parametrize(
        "statement", ["import json", "from re import compile as rc"]
    )
    def test_merging_twice_is_idempotent(self, statement: str) -> None:
        once = merge_imports(HEADER, [statement]).source
        assert once is not None
        assert merge_imports(once, [statement]).source is None


class TestAddedImports:
    def test_a_header_that_only_gained_imports(self) -> None:
        new = HEADER.replace("import os\n", "import os\nimport json\n")
        assert added_imports(HEADER, new) == ["import json"]

    def test_any_other_change_is_a_real_edit(self) -> None:
        assert added_imports(HEADER, HEADER.replace("LIMIT = 3", "LIMIT = 4")) is None
        assert added_imports(HEADER, HEADER.replace("import os\n", "")) is None
        assert added_imports(HEADER, HEADER) is None
        assert added_imports(HEADER, "import (") is None
