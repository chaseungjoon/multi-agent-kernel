"""Tests for the CLI multi-provider token counter (finding #9).

The counter must aggregate usage from all three provider SDKs, not just
Anthropic, so a run on openai:/gemini: reports real token usage.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from cli.runner import (
    anthropic_tokens,
    gemini_tokens,
    install_token_counter,
    openai_tokens,
    read_token_counter,
    reset_token_counter,
)


class TestUsageExtractors:
    def test_anthropic_sums_input_and_output(self) -> None:
        assert anthropic_tokens(NS(input_tokens=10, output_tokens=5)) == 15

    def test_openai_prefers_total_then_falls_back(self) -> None:
        assert openai_tokens(NS(total_tokens=42)) == 42
        assert openai_tokens(NS(prompt_tokens=8, completion_tokens=4)) == 12

    def test_gemini_prefers_total_then_falls_back(self) -> None:
        assert gemini_tokens(NS(total_token_count=100)) == 100
        assert (
            gemini_tokens(NS(prompt_token_count=7, candidates_token_count=3)) == 10
        )

    def test_missing_usage_is_zero(self) -> None:
        assert anthropic_tokens(None) == 0
        assert openai_tokens(None) == 0
        assert gemini_tokens(None) == 0


class TestInstall:
    def test_install_is_idempotent_and_resettable(self) -> None:
        install_token_counter()
        install_token_counter()  # second call is a no-op, must not raise
        reset_token_counter()
        assert read_token_counter() == 0
