"""Tests for the filter predicates and the distribution helpers."""

from __future__ import annotations

import math

import pytest

from mining.filters import is_bot, is_generated
from mining.hot_nodes import categorise, fit_power_law, gini
from mining.stats import percentile, summarise, wilson_interval


@pytest.mark.parametrize(
    ("login", "user_type"),
    [
        ("dependabot[bot]", "Bot"),
        ("renovate[bot]", "User"),
        ("pre-commit-ci[bot]", "User"),
        ("someone", "Bot"),
    ],
)
def test_automation_authors_are_detected(login: str, user_type: str) -> None:
    assert is_bot(login, user_type)


def test_human_authors_are_not_flagged() -> None:
    assert not is_bot("adamchainz", "User")
    assert not is_bot("robert", "User")


@pytest.mark.parametrize(
    "path",
    ["uv.lock", "package-lock.json", "pkg/locale/de/LC_MESSAGES/django.po",
     "api/service_pb2.py", "AUTHORS", "web/static/app.min.js"],
)
def test_generated_paths_are_detected(path: str) -> None:
    assert is_generated(path)


@pytest.mark.parametrize("path", ["mak/core/types.py", "docs/index.rst", "setup.py"])
def test_source_paths_are_not_generated(path: str) -> None:
    assert not is_generated(path)


def test_headers_are_categorised_before_anything_else() -> None:
    node_id = "docs/conf.py::module_header::__header__"
    assert categorise(node_id, "module_header") == "import_header"


def test_registry_and_settings_categories() -> None:
    assert categorise("pkg/registry.py::function::register", "function") == "registry"
    settings = "pkg/settings.py::module_body::__body__"
    assert categorise(settings, "module_body") == "settings"
    urls = "pkg/urls.py::module_body::__body__"
    assert categorise(urls, "module_body") == "url_table"
    assert categorise("tests/test_x.py::function::test_y", "function") == "tests"


def test_summary_of_an_empty_distribution_is_all_zero() -> None:
    summary = summarise([])
    assert summary.count == 0
    assert summary.maximum == 0


def test_percentiles_interpolate() -> None:
    assert percentile([0, 10], 0.5) == pytest.approx(5.0)
    assert percentile([1, 2, 3, 4], 0.0) == pytest.approx(1.0)
    assert percentile([1, 2, 3, 4], 1.0) == pytest.approx(4.0)


def test_gini_is_zero_for_a_flat_distribution() -> None:
    assert gini([5, 5, 5, 5]) == pytest.approx(0.0, abs=1e-9)


def test_gini_rises_with_concentration() -> None:
    assert gini([1, 1, 1, 100]) > gini([1, 1, 1, 5])


def test_wilson_interval_brackets_the_point_estimate() -> None:
    low, high = wilson_interval(10, 100)
    assert low < 0.10 < high
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_power_law_fit_is_undefined_for_a_tiny_sample() -> None:
    fit = fit_power_law([1, 2, 3])
    assert math.isnan(fit.alpha)


def test_power_law_fit_recovers_a_heavy_tail() -> None:
    counts = [max(1, int(1000 / (rank ** 2))) for rank in range(1, 400)]
    fit = fit_power_law(counts)
    assert fit.alpha > 1.0
    assert 0.0 <= fit.gini <= 1.0
