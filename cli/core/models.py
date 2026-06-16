"""Model registry: all hosted models grouped by provider."""
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
    ModelInfo("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6",
              "ANTHROPIC_API_KEY", "anthropic_api", recommended=True, planner_ok=True),
    ModelInfo("anthropic", "claude-opus-4-8",   "Claude Opus 4.8",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=True),
    ModelInfo("anthropic", "claude-haiku-4-5",  "Claude Haiku 4.5",
              "ANTHROPIC_API_KEY", "anthropic_api",                   planner_ok=False),
    ModelInfo("openai", "gpt-5.5",     "GPT-5.5",
              "OPENAI_API_KEY", "openai_api", recommended=True, planner_ok=True),
    ModelInfo("openai", "gpt-4o",      "GPT-4o",
              "OPENAI_API_KEY", "openai_api",                  planner_ok=True),
    ModelInfo("openai", "gpt-4o-mini", "GPT-4o Mini",
              "OPENAI_API_KEY", "openai_api",                  planner_ok=False),
    ModelInfo("gemini", "gemini-3-pro",     "Gemini 3 Pro",
              "GEMINI_API_KEY", "gemini_api", recommended=True, planner_ok=True),
    ModelInfo("gemini", "gemini-2.0-flash", "Gemini 2.0 Flash",
              "GEMINI_API_KEY", "gemini_api",                   planner_ok=False),
    ModelInfo("gemini", "gemini-1.5-pro",   "Gemini 1.5 Pro",
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
    return (rec or candidates[0]).model_id if candidates else "claude-sonnet-4-6"
