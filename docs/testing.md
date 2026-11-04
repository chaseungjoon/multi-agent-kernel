# Testing

> [Contributing guide](../CONTRIBUTING.md) · ← [Getting started](getting-started.md) · [Workflow](workflow.md) →

Every change has to pass three gates. Some areas also have their own guard
tests, listed below. Kernel refactors additionally have to leave the golden
event logs unchanged.

## Quality gates

All three must be green for every change:

```bash
pytest -q                  # full suite
mypy --strict mak cli      # zero errors
ruff check mak cli tests   # zero findings
```

CI (`.github/workflows/ci.yml`) runs all three on Python 3.11 and 3.13, on
pushes to `main` and on pull requests. Focused runs while iterating:
`pytest tests/node_store/ -q`, `pytest tests/test_session.py tests/session -q`.

## Golden event logs

Golden event logs are the behaviour-preservation net for kernel refactors.
`python -m tests.golden.golden compare` replays 34 recorded scenarios: the
concurrency corpus, every semantic-corpus shape through each MAK arm, and the MAK
arm of all four benchmark templates in mock mode. It fails when any task's
ordered `(event, payload)` sequence or any run result differs from
`tests/golden/data/`. Timings are excluded, and so are the
thread-schedule-dependent fields in the deliberately racy overlap corpus. The
non-benchmark scenarios also run in the suite (`tests/golden/test_goldens.py`).

After an *intended* behaviour change, re-record with
`python -m tests.golden.golden record` and review the data diff like code.

## Guard tests by area

| If you change… | Keep green / extend | Why |
|---|---|---|
| Ingestion or reconstruction | `tests/node_store/test_roundtrip.py` (mandatory) | the [round-trip invariant](subsystems/03-ast-pipeline.md#36-the-round-trip-invariant) |
| Locking | `tests/lock_manager/test_concurrency.py` | no two conflicting holders may ever coexist ([§4.2](subsystems/04-lock-manager.md#42-lock-table)) |
| A structural conflict check | extend **both** corpora in `tests/conflict_detector/test_false_positive_corpus.py` | precision over recall ([§5.1](subsystems/05-conflict-detection.md#51-structural-checks-makconflict_detector)) |
| The semantic layer | `tests/test_semantic_corpus.py` | the ten-shape table and the corpus's validity contract ([corpus](benchmarks-and-research.md#semantic-conflict-corpus)) |
| The session | `tests/test_module_budgets.py`; a new per-wave field goes on `WaveState`, a new commit-time rule is a new `CommitCheck` | size budgets and per-wave state by construction ([§11](subsystems/11-session-lifecycle.md)) |
| The commit pipeline order | the order-pinning test in `tests/session/test_commit_pipeline.py` | reordering checks is a reviewed change ([§5.3](subsystems/05-conflict-detection.md#detection-at-commit)) |
| Concurrent dispatch | `tests/test_concurrency_integration.py` | the concurrency gate ([Overview](overview.md#the-mental-model)) |
| Config keys or examples | `tests/test_example_configs.py` | examples cannot drift from the schema ([§12](subsystems/12-configuration.md)) |
| Either front end's settings path | `tests/application/test_parity.py` | `mak run` and the app must build an identical `MakConfig` ([§13](subsystems/13-command-line.md#the-application-api-makapplication)) |
| Planner inventory rendering | goldens in `tests/planner/golden/` | providers cache the prompt prefix built from them ([§9](subsystems/09-planner.md)) |

## Rules for every test

- **Add tests with every feature.**
- **Performance rewrites of subtle behaviour** need a **differential** test
  against the implementation being replaced.
- **No test may read the real user configuration.** `tests/conftest.py`'s
  `pytest_configure` points `HOME` and `XDG_CONFIG_HOME` at a throwaway directory
  before any test module is imported, and a per-test fixture isolates the `.env`
  lookups. `tests/test_hermetic.py` fails if the manifest, endpoint store,
  `local_hosts.json`, user `.env` or user `config.yaml` resolve outside it. Never
  read per-user state at import time.
- **Behaviour must not depend on the host Python.** When a standard-library
  answer differs between versions (as `Path.glob`'s trailing `**` does), MAK
  decides the meaning and the test states it explicitly.
- **No test touches the network.** Provider fetches use fake sources,
  local-runtime tests inject the client, and endpoint tests use
  `tests/support/fake_openai_server.py` on loopback.

---

[Contributing guide](../CONTRIBUTING.md) · ← [Getting started](getting-started.md) · [Workflow](workflow.md) →
