# 15. Model catalog

> [Contributing guide](../../CONTRIBUTING.md) · ← [14. Interactive app (`cli/`)](14-interactive-app.md) · [16. Local runtimes](16-local-runtimes.md) →
>
> **Code:** `mak/models/`, `cli/core/models.py` · **Tests:** `tests/models/`

`mak/models/` answers *which models each provider currently offers*. It never
decides *which model MAK uses* — that is `planner.model` / `agents[].model` — and
a refresh never writes `config.yaml` (an acceptance test byte-compares it).

- **Facts vs. judgment.** A `ModelEntry` carries facts from the provider
  (`display_name`, `context_window`, `max_output`, `supported_parameters`) and
  judgment (`recommended`, `planner_ok`, `planner_recommended`) that comes **only**
  from the hand-maintained exact-id table `curation.py::CURATED`. No heuristics;
  an uncurated model gets a neutral `Judgment()`.
  `test_only_curated_ids_carry_stars` pins this. `curation.py::DENY` separately
  filters non-chat models and collapses dated snapshots onto their aliases.
- **Persistence.** `manifest.py` caches entries in `~/.config/mak/models.json`
  (atomic write), keyed by `(endpoint_id, model_id)`, schema version 3. Older
  schemas are migrated on read with no refetch.
- **Schedule.** `is_refresh_due` implements catch-up scheduling on the 1st and
  15th of each month; a 6-hour cooldown follows a failed attempt.
- **Failure isolation.** `refresh.py` replaces a provider's (or endpoint's)
  entries only on its own successful fetch. A model a provider stops offering is
  marked `retired`, not deleted; `recommended_planner` skips retired entries.
- **Endpoint catalogs.** `sources_for_endpoints` builds an
  `OpenAiCompatibleSource` per configured endpoint. `providers.py::reported_parameters`
  reads `supported_parameters` (from the SDK's `model_extra`) into the tri-state
  field [§8](08-endpoints.md) relies on. `evaluated` (whether curation applies) is derived from the
  provider at load time.
- **Runtime registry.** `ModelRegistry` composes the packaged `seed.json` (the
  offline floor), the manifest, and `judgment_for()` on every load. The snapshot
  is an immutable tuple swapped in one assignment, so reads need no lock.
  Lookups: `for_endpoint(endpoint_id)`, `find(model, endpoint_id)`.
- **Triggering.** `maybe_auto_refresh` runs on a daemon thread from
  `cli/app.py::_init_state`, gated by `models.auto_refresh` and
  `MAK_NO_MODEL_REFRESH`, and never prints. `/refresh-models` is the synchronous
  counterpart. Listing a local runtime during a refresh is capped at 2 seconds.
- **Credential and adapter identity.** `ModelEntry.api_key_env` /
  `.adapter_type` answer for the three built-in providers and are `None` for any
  endpoint's entry — its endpoint owns both. They never raise and never guess a
  variable name.
- **`cli/core/models.py`** is a thin adapter: `ModelInfo` is `ModelEntry`, and
  every helper (`all_models`, `models_for_provider`,
  `recommended_planner_for_provider`, `default_planner_route`) takes the registry
  explicitly. There is no module-level registry and no import-time I/O.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [14. Interactive app (`cli/`)](14-interactive-app.md) · [16. Local runtimes](16-local-runtimes.md) →
