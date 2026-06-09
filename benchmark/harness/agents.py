"""The agent layer, shared by both runners so the comparison is fair.

A *backend* turns a stub function into an implementation and can resolve a registry
merge conflict. Two implementations:

- ``MockBackend`` — keyless and deterministic (returns the reference solution and a
  correct union merge). It costs zero tokens and near-zero time, so its job is to
  *validate the harness mechanics and the coordination difference*, not to produce
  representative time/token numbers.
- ``RealBackend`` — calls a hosted model (Anthropic / OpenAI / Gemini) and reports
  real token usage. This is what produces the real benchmark numbers.

Both runners call the *same* backend for the *same* operations, so the only thing
that varies between MAK and the worktree baseline is the coordination model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from harness.workload import Operation, add_registration

_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n```$", re.MULTILINE)


@dataclass(frozen=True)
class Usage:
    """Token usage and call count for one or more model calls."""

    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            self.calls + other.calls,
        )


class Backend(Protocol):
    """A model (or mock) that implements functions and resolves registry conflicts."""

    name: str

    def implement(self, op: Operation, stub_source: str) -> tuple[str, Usage]:
        """Return the rewritten source for ``op``'s function, plus token usage."""
        ...

    def resolve(self, versions: list[str]) -> tuple[str, Usage]:
        """Merge several conflicting ``_register_all`` sources into one, plus usage."""
        ...


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip() + "\n"


def _union_registry(versions: list[str]) -> str:
    """Deterministically merge ``_register_all`` versions by unioning their lines."""
    merged = "def _register_all() -> None:\n    pass\n"
    for version in versions:
        for line in version.splitlines():
            if line.strip().startswith("register("):
                merged = add_registration(merged, line)
    return merged


class MockBackend:
    """Deterministic, free backend used for the keyless harness self-test."""

    def __init__(self, name: str) -> None:
        self.name = name

    def implement(self, op: Operation, stub_source: str) -> tuple[str, Usage]:
        return op.reference, Usage(calls=1)

    def resolve(self, versions: list[str]) -> tuple[str, Usage]:
        # A correct union merge: the baseline *can* reconcile, but every conflict is
        # still counted (in a real run each one is a model call that costs tokens).
        return _union_registry(versions), Usage(calls=1)


_IMPLEMENT_SYS = (
    "You implement a single Python function. You are given the function's current "
    "stub (with its docstring). Replace the body with a correct implementation that "
    "satisfies the docstring. Keep the exact same function name and signature. "
    "Return ONLY the complete function source — no prose, no markdown fences."
)

_RESOLVE_SYS = (
    "You are merging several versions of one Python function, '_register_all', that "
    "were edited in parallel. Each version added some 'register(...)' lines. Produce "
    "a single merged version of '_register_all' that contains EVERY register(...) "
    "line from EVERY version, with no duplicates. Return ONLY the function source — "
    "no prose, no markdown fences."
)


class RealBackend:
    """Calls a hosted model and reports real token usage."""

    def __init__(self, name: str, provider: str, model: str, client: Any = None) -> None:
        self.name = name
        self.provider = provider
        self.model = model
        self._client = client

    def implement(self, op: Operation, stub_source: str) -> tuple[str, Usage]:
        prompt = f"Implement this function:\n\n{stub_source}"
        text, usage = self._call(_IMPLEMENT_SYS, prompt)
        return _strip_fence(text), usage

    def resolve(self, versions: list[str]) -> tuple[str, Usage]:
        joined = "\n\n# ---- version ----\n".join(versions)
        text, usage = self._call(_RESOLVE_SYS, joined)
        return _strip_fence(text), usage

    # -- provider dispatch -------------------------------------------------

    def _call(self, system: str, prompt: str) -> tuple[str, Usage]:
        if self.provider == "anthropic":
            return self._call_anthropic(system, prompt)
        if self.provider == "openai":
            return self._call_openai(system, prompt)
        if self.provider == "gemini":
            return self._call_gemini(system, prompt)
        raise ValueError(f"unknown provider: {self.provider}")

    def _anthropic(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _call_anthropic(self, system: str, prompt: str) -> tuple[str, Usage]:
        resp = self._anthropic().messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = Usage(resp.usage.input_tokens, resp.usage.output_tokens, 1)
        return text, usage

    def _openai(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI()
        return self._client

    def _call_openai(self, system: str, prompt: str) -> tuple[str, Usage]:
        resp = self._openai().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        text = resp.choices[0].message.content or ""
        usage = Usage(resp.usage.prompt_tokens, resp.usage.completion_tokens, 1)
        return text, usage

    def _gemini(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client()
        return self._client

    def _call_gemini(self, system: str, prompt: str) -> tuple[str, Usage]:
        resp = self._gemini().models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": system},
        )
        meta = resp.usage_metadata
        usage = Usage(meta.prompt_token_count, meta.candidates_token_count, 1)
        return resp.text or "", usage


@dataclass(frozen=True)
class AgentSpec:
    """One agent: a display name, a provider, and a model id."""

    name: str
    provider: str
    model: str


def make_backends(specs: list[AgentSpec], *, mock: bool) -> list[Backend]:
    """Build one backend per agent spec (mock or real)."""
    if mock:
        return [MockBackend(spec.name) for spec in specs]
    return [RealBackend(spec.name, spec.provider, spec.model) for spec in specs]
