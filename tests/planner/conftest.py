"""Shared planner fixtures: synthetic repositories, built once per session."""

from __future__ import annotations

import pytest

from tests.planner.synthetic_repo import SyntheticRepo, build_synthetic_store


@pytest.fixture(scope="session")
def synthetic_10(tmp_path_factory: pytest.TempPathFactory) -> SyntheticRepo:
    return build_synthetic_store(tmp_path_factory.mktemp("synthetic-10"), 10)


@pytest.fixture(scope="session")
def synthetic_100(tmp_path_factory: pytest.TempPathFactory) -> SyntheticRepo:
    return build_synthetic_store(tmp_path_factory.mktemp("synthetic-100"), 100)


@pytest.fixture(scope="session")
def synthetic_1000(tmp_path_factory: pytest.TempPathFactory) -> SyntheticRepo:
    return build_synthetic_store(tmp_path_factory.mktemp("synthetic-1000"), 1000)
