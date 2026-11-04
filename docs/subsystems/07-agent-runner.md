# 7. Agent runner and adapters

> [Contributing guide](../../CONTRIBUTING.md) · ← [6. Scheduler](06-scheduler.md) · [8. Endpoints](08-endpoints.md) →
>
> **Code:** `mak/agent_runner/`, `mak/bootstrap.py` · **Tests:** `tests/agent_runner/`, `tests/test_bootstrap.py`

`mak/agent_runner/` is the boundary to agents. **The kernel never calls a model
API directly** — it speaks to an `AgentAdapter`.

## 7.1 The adapter interface

- `AgentAdapter` (`adapters/base_adapter.py`) — `format_task(bundle) -> str`,
  `parse_result(raw) -> TaskResult`, `health_check() -> bool`, and optionally
  `health_detail()` (why a check failed; read with `getattr`).
- `SubprocessAgentAdapter` — adds `spawn(working_dir) -> Popen` for CLI agents.

## 7.2 API adapters (primary)

Direct API calls return structured JSON natively, and a single structured call
matches MAK's agent contract: one node in, one strict `TaskResult` out. Autonomous
file-editing agent loops would bypass the node store and lock manager.

| `agent_type` | Backend | Structured output |
|---|---|---|
| `anthropic_api` | Anthropic Messages API (streamed) | `tool_choice` pinned to a `submit_task_result` tool |
| `openai_api` | OpenAI Chat Completions | JSON schema / JSON mode, per the capability ladder ([§8](08-endpoints.md)) |
| `gemini_api` | Google GenAI `generate_content` | function calling in `ANY` mode restricted to `submit_task_result` |
| `local_api` | any OpenAI-compatible server | same class as `openai_api`, `base_url` required ([§7.8](#78-local-transports)) |
| `ollama_api` | native Ollama API | `format` = JSON schema, compiled to a grammar ([§7.8](#78-local-transports)) |

Each adapter imports its SDK lazily and accepts an injectable client, so tests
never make a real call. `result_schema.py` renders the one `TaskResult` schema in
four dialects (`anthropic`, `gemini`, `openai` strict, `ollama`), so the schema
exists once.

Every system prompt states three contracts from `protocol.py`:

- `NODE_ID_CONTRACT` — copy ids verbatim from `target_nodes`; return a bare-path
  target as one complete file.
- `NO_CHANGE_CONTRACT` — a no-op counts only when `no_changes_required` is set.
- `RETRY_NOTE_CONTRACT` — follow a bundle's `retry_note` rather than repeating
  the failed attempt.

## 7.3 Output budget and stop signals

A truncated reply and a deliberate "nothing to change" look the same once
decoded, so every adapter checks the provider's stop signal **before** reading
the payload.

- `adapters/budget.py::resolve_agent_max_tokens(model)` — floor 8192, ceiling
  32000, fallback 16384 for an unknown model. Declares
  `TRUNCATION_STOP_REASONS` (`max_tokens`, `length`, `MAX_TOKENS`) and
  `REFUSAL_STOP_REASONS` (`refusal`, `content_filter`, `SAFETY`, `RECITATION`,
  `PROHIBITED_CONTENT`, `BLOCKLIST`).
- `stop_signals.py` — `check_stop_reason` raises `AgentTruncatedError` /
  `AgentRefusedError`; `extract_usage` normalizes each provider's token fields;
  `with_response_metadata` merges stop reason and usage into the payload so even
  a good attempt carries them.

| Adapter | Budget sent | Stop signal |
|---|---|---|
| `anthropic_api` | `resolve_agent_max_tokens(model)`; streaming is required at this size | `stop_reason` |
| `openai_api` / `local_api` | only if `max_tokens` is configured (`max_completion_tokens` for cloud OpenAI, per the endpoint's `token_parameter` otherwise) | `finish_reason` |
| `gemini_api` | only if configured (`max_output_tokens`) | `candidate.finish_reason` |
| `ollama_api` | `num_predict` = the resolved budget | `done_reason` |

`AgentConfig.max_tokens` overrides the budget per agent.

## 7.4 CLI adapters (secondary)

`claude_code`, `codex`, and `copilot` share `CliSubprocessAdapter`
(`cli_adapter.py`). Real CLIs do not speak MAK's newline-JSON protocol, so each
adapter launches a **bridge wrapper** — `python -m
mak.agent_runner.wrappers.<name>` — which reads a `TaskBundle`, prompts the CLI
for each target's rewritten source as strict JSON, runs the CLI
non-interactively, and writes back a `TaskResult`. The config `cmd` selects the
binary (`--cli <binary>`); `MAK_<AGENT>_CMD` (e.g. `MAK_CLAUDE_CODE_CMD`) replaces
the whole command line. They can be Docker-sandboxed ([§7.7](#77-the-runner-and-sandbox)).

## 7.5 Registry and composition root

- `AdapterRegistry` (`registry.py`) is an **instance**, never module-global
  state, **keyed by agent id**. `register` / `register_factory` raise
  `ConfigError` on a duplicate id; `replace_factory` exists for tests;
  `list_ids()` returns every id (`list_types()` is a deprecated alias).
- `mak/bootstrap.py` is the composition root:
  - `build_registry(config, …)` registers a config-bound factory per agent,
    binding model, resolved credential, timeout, and endpoint settings. It makes
    **no network call**. The capability lookup is injected ([§8](08-endpoints.md)).
  - `default_agent_type(config)` — the first configured agent.
  - `validate_config(config)` — rejects unknown types and fields set on a type
    that ignores them.
  - `agents_from_specs(specs, *, env, endpoint_ids)` — parse
    `provider[:model][@url]` roster specs ([§13](13-command-line.md)). `env` is where an
    `ollama:`/`local:` spec without a URL reads `$MAK_LOCAL_BASE_URL`;
    `endpoint_ids` is the set a prefix may name.
  - `configured_endpoint_ids(config)` — the user store's endpoint ids plus
    `config`'s own; the caller that holds the run's config passes it, so a
    project config discovered from a work dir is honoured.
  - `planner_from_spec(spec, planner)` — a compatibility wrapper over
    `PlannerRoute.from_spec(...).apply(planner)` ([§13](13-command-line.md)); the grammar lives once.
  - `resolved_agents(config, *, env)` — resolves the roster against the merged
    endpoint set and the given environment.
  - `healthy_agent_types(registry, types)` — the startup health preflight. An
    unhealthy agent is dropped with a warning naming why (`health_detail`); the
    run aborts if the default agent is unusable.
  - `seed_capabilities` — seeds the capability cache from the model catalog.
- The bootstrap builds pieces; `mak/application/` ([§13](13-command-line.md)) is what assembles a
  whole `Session` from them, for both front ends.

## 7.6 The wire protocol (`protocol.py`)

`TaskBundle` / `TaskResult` serialized as newline-delimited JSON,
`protocol_version` `"1.0"`. `decode_task_result` accepts `modified_nodes` (ids
only), a `modified_fragments` array of `{node_id, new_source}`, or a
`new_sources` map, and normalizes them into `new_sources`. It is hardened
against the shapes models actually produce:

- a lone `modified_fragments` object is coerced into a list, and a JSON-encoded
  string is parsed once; any other non-array is rejected;
- every fragment must be an object with a non-empty string `node_id`;
- a missing `task_id` / `success` is a named failure, not a `KeyError`;
- error messages include a bounded excerpt of the rejected value.

No malformed shape escapes as a raw `TypeError` or `KeyError` — every one is an
`AgentProtocolError`, which keeps the provider's `stop_reason` and `usage`.

`map_returned_sources(grant, new_sources, order_key=None)` enforces the
node-granularity contract for both the session and the CLI bridge ([§3.4](03-ast-pipeline.md#34-collection-agent-output--node-store)). A
whole-file rewrite returned under a fragment grant is refused, because it would
touch nodes owned by other tasks.

## 7.7 The runner and sandbox

`AgentRunner.assign(adapter, task, working_dir=None)` (`runner.py`) is the single
entry point. `work_dir` is fixed at construction.

- **API adapters:** `format_task → send → parse_result`, classifying failures
  three ways: `AgentResponseError` (provider answered; reply rejected — its
  `stop_reason`, `usage`, and `retryable` flow through), any other exception from
  `send` (`"api call failed: …"`), and any other exception from `parse_result`
  (`"could not decode agent result: …"`).
- **Subprocess adapters:** an idle-process pool per agent; write a JSON line,
  read the result under a timeout (tolerating preamble and pretty-printed JSON),
  SIGTERM on timeout, discard a process on failure. `max_instances` caps the
  retained pool.

Every path returns a `TaskResult`. Each API adapter takes a request `timeout`
from `AgentConfig.timeout` (the Gemini SDK takes milliseconds; the adapter
converts). `Session.close()` calls `agent_runner.shutdown()`, and on the abnormal
path shuts the thread pool down with `cancel_futures=True`; the per-request
timeout is what bounds a call already in flight.

`sandbox.py::SandboxConfig.wrap(argv, working_dir)` builds a `docker run` argv
that bind-mounts the work dir and defaults to `--network none`. `--sandbox`
applies it to CLI adapters; `docker_available()` fails fast when Docker is
missing.

## 7.8 Local transports

- **`local_api`** — the `OpenAiApiAdapter` class registered under its own type,
  `base_url` required (MAK never guesses a port).
- **Credentials are never leaked.** With a `base_url`, the adapter sends the
  configured `api_key_env` value, or the placeholder `"local"` — always
  *something*, so the SDK can never fall back to an ambient `OPENAI_API_KEY` and
  post it to an unrelated host. `test_a_real_openai_key_in_the_environment_is_never_forwarded`
  guards this.
- **Parse → repair → retry** (`repair.py::repair_loop`, shared by the
  OpenAI-compatible and Ollama adapters). A reply that fails to decode gets one
  short follow-up turn containing the model's own reply plus
  `protocol.REPAIR_INSTRUCTION`, bounded by `agents[].repair_attempts` (default
  `1`, `0` disables). Never after a truncation or refusal. Usage is summed across
  turns and `TaskResult.repairs` records the count, so the spend ceiling sees
  every token.
- **Malformed bodies are `protocol` failures**, so the retry note restates the
  schema.
- **Health.** With a `base_url`, `health_check` probes the server once (5s) and
  `health_detail()` explains a failure ("Ollama is not running at …", "model …
  is not pulled").

**`ollama_api`** (`ollama_api_adapter.py`) is a native adapter because Ollama's
runtime context defaults to a few thousand tokens and **silently truncates** an
over-long prompt. The adapter:

1. reads the model's real context length from `/api/show` (cached per instance);
2. sizes `options.num_ctx` to the bundle — `min(model_context_length,
   round_up(estimate_tokens(prompt) * 1.25 + num_predict))`, floor 4096, where
   `estimate_tokens` is `len(prompt) / 4`;
3. raises `AgentContextExceededError` (non-retryable) when the bundle cannot fit,
   naming the sizes and the settings that fix it. A configured `num_ctx` is used
   verbatim as a hard ceiling.

`keep_alive` keeps the model resident between tasks; `health_check` verifies both
the server (`version()`) and the model (`list_models()`).

---

[Contributing guide](../../CONTRIBUTING.md) · ← [6. Scheduler](06-scheduler.md) · [8. Endpoints](08-endpoints.md) →
