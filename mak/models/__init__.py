"""Model catalog subsystem: what models each provider currently offers.

The catalog answers *"which models exist"*. It never answers *"which model do we
use"* — that stays in ``config.yaml``, which this subsystem never writes to.

Two layers, kept strictly apart (see ``mak.models.curation``):

* **Facts** — fetched from each provider's list-models endpoint and cached in
  ``~/.config/mak/models.json``, refreshed on the 1st and 15th (or on demand via
  the CLI's ``/refresh-models``).
* **Judgment** — ``recommended`` / ``planner_ok`` / ``planner_recommended``, set
  only by the hand-maintained table in ``curation.CURATED``. MAK never infers it.
"""

from mak.models.catalog import (
    KEY_ENV_TO_PROVIDER,
    PROVIDER_ADAPTER,
    PROVIDER_DISPLAY,
    PROVIDER_KEY_ENV,
    PROVIDER_ORDER,
    ModelEntry,
    load_seed,
)
from mak.models.curation import CURATED, Judgment, filter_ids, judgment_for
from mak.models.manifest import (
    Manifest,
    is_refresh_due,
    load_manifest,
    manifest_path,
    save_manifest,
)
from mak.models.providers import (
    FetchedModel,
    ModelFetchError,
    ModelSource,
    default_sources,
)
from mak.models.refresh import ProviderResult, RefreshReport, refresh
from mak.models.registry import NO_REFRESH_ENV, ModelRegistry

__all__ = [
    "CURATED",
    "KEY_ENV_TO_PROVIDER",
    "NO_REFRESH_ENV",
    "PROVIDER_ADAPTER",
    "PROVIDER_DISPLAY",
    "PROVIDER_KEY_ENV",
    "PROVIDER_ORDER",
    "FetchedModel",
    "Judgment",
    "Manifest",
    "ModelEntry",
    "ModelFetchError",
    "ModelRegistry",
    "ModelSource",
    "ProviderResult",
    "RefreshReport",
    "default_sources",
    "filter_ids",
    "is_refresh_due",
    "judgment_for",
    "load_manifest",
    "load_seed",
    "manifest_path",
    "refresh",
    "save_manifest",
]
