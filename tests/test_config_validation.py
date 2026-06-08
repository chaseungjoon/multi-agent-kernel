"""Tests for config validation: loaded configs must name only known agent types."""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.bootstrap import validate_config
from mak.config import load_config
from mak.core.exceptions import ConfigError


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def test_bundled_config_is_valid() -> None:
    config_path = Path(__file__).resolve().parent.parent / "mak" / "config.yaml"
    validate_config(load_config(config_path))  # must not raise


def test_known_api_and_cli_types_pass(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agents:\n"
        "  - type: anthropic_api\n"
        "  - type: gemini_api\n"
        "  - type: claude_code\n"
        "  - type: codex\n"
        "  - type: copilot\n",
    )
    validate_config(load_config(path))  # must not raise


def test_unknown_type_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agents:\n  - type: anthropic_api\n  - type: gemni_api\n",  # typo
    )
    with pytest.raises(ConfigError, match="unknown agent type"):
        validate_config(load_config(path))
