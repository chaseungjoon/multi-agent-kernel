"""Model registry: all hosted models grouped by provider.

Last refreshed 2026-07 against each provider's public model list. ``recommended``
marks the per-provider default MAK auto-selects (best quality/cost fit for
agentic coding). ``planner_ok`` marks models with roughly claude-sonnet-4-6
capability or better — the bar for reliable task decomposition; models below it
get a "not recommended" warning when chosen as the planner.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model_id: str
    display_name: str
    api_key_env: str
    adapter_type: str
    recommended: bool = False
    # False = below sonnet-4-6 capability; show a warning when used as planner
    planner_ok: bool = True


ALL_MODELS: list[ModelInfo] = [
    # Anthropic — https://platform.claude.com/docs/en/about-claude/models/overview
    ModelInfo("anthropic", "claude-fable-5",    "Claude Fable 5",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=True),
    ModelInfo("anthropic", "claude-opus-4-8",   "Claude Opus 4.8",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=True),
    ModelInfo("anthropic", "claude-sonnet-5",   "Claude Sonnet 5",
              "ANTHROPIC_API_KEY", "anthropic_api", recommended=True, planner_ok=True),
    ModelInfo("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=True),
    ModelInfo("anthropic", "claude-haiku-4-5",  "Claude Haiku 4.5",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=False),
    # OpenAI — https://developers.openai.com/api/docs/models
    ModelInfo("openai", "gpt-5.6-sol",   "GPT-5.6 Sol",
              "OPENAI_API_KEY", "openai_api", recommended=True, planner_ok=True),
    ModelInfo("openai", "gpt-5.6-terra", "GPT-5.6 Terra",
              "OPENAI_API_KEY", "openai_api",                   planner_ok=True),
    ModelInfo("openai", "gpt-5.5",       "GPT-5.5",
              "OPENAI_API_KEY", "openai_api",                   planner_ok=True),
    ModelInfo("openai", "gpt-5.6-luna",  "GPT-5.6 Luna",
              "OPENAI_API_KEY", "openai_api",                   planner_ok=False),
    # Google Gemini — https://ai.google.dev/gemini-api/docs/models
    ModelInfo("gemini", "gemini-3.1-pro-preview", "Gemini 3.1 Pro (Preview)",
              "GEMINI_API_KEY", "gemini_api",                   planner_ok=True),
    ModelInfo("gemini", "gemini-3.5-flash",       "Gemini 3.5 Flash",
              "GEMINI_API_KEY", "gemini_api", recommended=True, planner_ok=True),
    ModelInfo("gemini", "gemini-3.1-flash-lite",  "Gemini 3.1 Flash-Lite",
              "GEMINI_API_KEY", "gemini_api",                   planner_ok=False),
]

PROVIDER_DISPLAY = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google Gemini"}
PROVIDER_ORDER   = ("anthropic", "openai", "gemini")
KEY_ENV_TO_PROVIDER = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY":    "openai",
    "GEMINI_API_KEY":    "gemini",
}


def models_for_provider(provider: str) -> list[ModelInfo]:
    return [m for m in ALL_MODELS if m.provider == provider]


def providers_with_keys(api_keys: dict[str, str]) -> list[str]:
    return [KEY_ENV_TO_PROVIDER[k] for k, v in api_keys.items()
            if v.strip() and k in KEY_ENV_TO_PROVIDER]


def recommended_planner_for_provider(provider: str) -> str:
    candidates = models_for_provider(provider)
    rec = next((m for m in candidates if m.recommended), None)
    return (rec or candidates[0]).model_id if candidates else "claude-sonnet-5"
