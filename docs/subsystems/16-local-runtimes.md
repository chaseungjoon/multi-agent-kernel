# 16. Local runtimes

> [Contributing guide](../../CONTRIBUTING.md) · ← [15. Model catalog](15-model-catalog.md)
>
> **Code:** `mak/local/` · **Tests:** `tests/local/`

`mak/local/` asks a running local server what it has, live, every time — no
cache, no manifest, no retirement — because a local model list is authoritative
and changes the moment the user pulls a model. It is separate from `mak/models/`,
whose machinery is keyed by API-key variables and hosted list endpoints. It
shares one principle: the fact/judgment split.

- **`ollama_client.py`** — a dependency-free client over Ollama's native API
  (`urllib.request` + `json`), so a fully local install needs no provider SDK
  (which is why the `[local]` extra is empty). `version()`, `list_models()`,
  `show(model)` (context length read by key suffix across `model_info`),
  `running()`, `chat(...)` (non-streaming), `pull(model)` (streamed, interruptible,
  resumable). Every failure is a typed `OllamaError` naming the endpoint. Stateless
  and injectable — no test in `tests/local/` opens a socket.
- **`runtime.py`** — `LocalRuntime` value object (`kind`, `name`, `base_url`,
  `version`, `models`). Kinds: `ollama` (probed via `/api/version`, `/api/tags`)
  and `openai_compatible` (via `/v1/models`: LM Studio, vLLM, llama.cpp, LocalAI).
- **`discovery.py`** — `discover(*, extra_urls=(), timeout=0.4, prober=None)`
  scans well-known ports concurrently, returns what answered (Ollama first,
  deduplicated by URL), and **never raises**. `$MAK_LOCAL_BASE_URL` and
  `extra_urls` are scanned too.
- **`recommended.py`** — the judgment half: exact Ollama tags with size and
  purpose, **smallest first**. `RecommendedModel.is_small()` drives the planner
  recommendation. A human edits this list; MAK does not rank local models.

MAK never manages the Ollama daemon, and there is no native adapter for other
runtimes — `local_api` covers them. Ollama gets one because only its native API
can read and set the real context window.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [15. Model catalog](15-model-catalog.md)
