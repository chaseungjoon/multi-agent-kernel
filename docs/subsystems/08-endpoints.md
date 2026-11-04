# 8. Endpoints

> [Contributing guide](../../CONTRIBUTING.md) · ← [7. Agent runner and adapters](07-agent-runner.md) · [9. Planner](09-planner.md) →
>
> **Code:** `mak/endpoints/` · **Tests:** `tests/endpoints/`

`mak/endpoints/` lets MAK talk to any OpenAI-compatible service and to several of
them in one run. Four identities are kept separate:

| Identity | What it is |
|---|---|
| **transport** | the wire protocol an adapter speaks (`openai_chat`, `anthropic`, `ollama_native`, …) |
| **profile** | a named set of documented defaults — URL, key variable, capabilities |
| **endpoint** | one configured service: URL, credential *reference*, capability settings |
| **agent id** | the routing key every scheduler, planner, log line, and git trailer uses |

`type` only selects a constructor. Every agent — endpoint-backed or built-in —
resolves through the same path, via synthesized built-in endpoints for the three
hosted providers and the two local transports (`builtin.py`). Reserved ids
(`anthropic`, `openai`, `gemini`, `google`, `local`, `ollama`) cannot be taken by
a user endpoint, so a spec prefix has exactly one meaning.

| Module | Role |
|---|---|
| `types.py` | leaf module: `Transport`, `Location`, `ModelDiscovery`, `HealthPolicy`, `StructuredOutput`, `TokenParameter`, `ProviderRouting`, `EndpointConfig`. No I/O |
| `profiles.py` | the presets (`nvidia`, `openrouter`, `deepseek`, `zai-general`, `zai-coding`, `custom`) — **the only place** preset URLs and key names may appear; a test enforces it |
| `parse.py` | YAML → `EndpointConfig`; materializes identity, **defers** capabilities so a corrected profile default reaches everyone on `auto` |
| `resolution.py` | precedence at use time: explicit field > profile default > transport default. `None` falls through; an explicit `"none"` stops the walk |
| `agents.py` | `resolve_agents`, `derive_agent_id`, `unique_agent_id` |
| `store.py` | per-user `~/.config/mak/endpoints.json`, atomic `0600`, schema-versioned; merged with project `endpoints:` (project wins on a matching id) |
| `health.py` | failure classification (credentials, not found, rate limited, incompatible, unreachable, SDK missing, model missing, unknown) with secrets redacted; policies `models` / `chat` / `none`. "Not probed" is distinct from "healthy" |
| `capabilities.py` | the structured-output ladder and `CapabilityCache` |
| `error_classification.py` | why a structured request was refused |

**Credentials are names, never values.** `api_key_env` names an environment
variable. An endpoint with none gets the non-secret placeholder. Extra `headers`
take `value` or `value_env`, never both, and may not set `Authorization`,
`Content-Type`, `Host`, or `User-Agent`.

## Structured-output negotiation

The ladder is `json_schema → json_object → none`. Which rung to use is decided by
two kinds of evidence, held in two separate fields of the session-scoped
`CapabilityCache` and never merged:

- **Reported** capabilities — from the model catalog (`supported_parameters`,
  [§15](15-model-catalog.md)). A claim: it lowers the **starting** rung and leaves descent available.
  `structured_outputs` authorizes `json_schema`; `response_format` authorizes
  `json_object`.
- **Proven** mode — a real successful call. It pins the rung exactly.

Both are clamped to the agent's configured `structured_output`: **a seed lowers,
never raises.** The reported set is **tri-state**: `None` (nothing published —
unknown), `frozenset()` (published and empty — also unknown), or a non-empty set
(a non-empty report that omits a parameter is a known negative). Cache keys use
the **whole** model id; `model` and `model:free` can have opposite capabilities.

`CapabilityCache` is owned and injected by the composition root — not a module
global — so two sessions never share what one learned. `discovering()` makes
discovery single-flight: the first caller for an unknown `(endpoint, model)` owns
the ladder, others wait on a bounded per-key event and start from what it proved;
no network call happens under the cache lock; a failed owner does not poison the
key.

**Descending requires a classified refusal.** `error_classification.py` parses,
in order, the HTTP status, `exc.body`, `response.json()`, the provider's
`error.message` / `code` / `metadata`, `error.metadata.raw` (JSON-decoded if it is
a JSON string), and `str(exc)` last. Text is NFKC-normalized, casefolded, and
non-alphanumeric runs collapse to one space, so `structured outputs`,
`structured-outputs`, and `structured_outputs` match one marker. A capability
refusal needs **all three**: a 400/422 status, a marker naming the reply format,
and language asserting an absent capability. Invalid schemas, auth, missing
models, quota, context limits, safety, transport errors, and 5xx all propagate.
The verdict is a typed `RejectionAnalysis`.

**Provider routing guard.** `provider_routing: openrouter` (set only by the
`openrouter` profile, never inferred from a hostname) adds OpenRouter's
`provider.require_parameters` via the SDK's `extra_body` — only on a rung the
catalog **positively** confirms. Its failure is a 404
(`failed_routing_step: "Filter by Parameters"`), so a guard 404 retries the
**same** rung without the guard rather than descending.

**Invariants to preserve:**

- Never widen the marker tuples to match a new provider's wording; fix the
  normalization or the parse order instead.
- An exception without an HTTP status is never a capability rejection.
- Unknown, empty, and reported are three states; `None` must never become
  `frozenset()` on any hop.
- Only positive confirmation sends the routing guard.
- Raw provider bodies are read in memory and never logged; only the short,
  `redact_secrets`-filtered reason (≤200 characters) reaches a log line.

**Debugging a structured-output problem:** `INFO` logs the chosen rung per
endpoint/model with its evidence (`catalog` or `runtime_rejection`); `DEBUG` logs
a dropped routing guard. To see what OpenRouter claims, read
`supported_parameters` for the exact id from `GET /api/v1/models` (per-route
detail at `/models/<id>/endpoints`). `MAK_NO_MODEL_REFRESH=1` freezes the catalog
to separate seed problems from negotiation problems.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [7. Agent runner and adapters](07-agent-runner.md) · [9. Planner](09-planner.md) →
