# Contention study (Wave 23)

A measurement of how often independent changes to large Python codebases collide,
at **file** granularity versus **AST-node** granularity, taken from public
open-source history.

The question this answers is the one MAK's design rests on: if concurrent changes
usually collide at file level but rarely at node level, then decomposing a
repository into independently lockable AST nodes buys real parallelism, and the
textual merge conflicts teams live with today are mostly *false* — two changes to
one file that never touch the same symbol.

## Layout

```
contention_study/
  mining/              the pipeline, one module per stage
  tests/               unit and end-to-end tests for the measurement code
  data/<owner__repo>/  per-repository cache, profile.json, audit sample
  data/RESULTS.md      every RQ table, generated
  data/results.json    the same numbers, machine readable
  plots/               figures, light and dark
  CONTENTION_STUDY.md  the write-up: method, results, threats
  DATASHEET.md         what the released dataset contains and how it was derived
  run.sh               launcher (isolated venv + PYTHONPATH)
```

## Setup

```bash
cd research/contention_study
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export GITHUB_TOKEN=...        # any token; only public metadata is read
```

Repository clones live **outside** the project tree, under
`$MAK_STUDY_CACHE` (default `~/.cache/mak-contention-study`), so the kernel's git
history never carries third-party source. The six bare clones total about 2.5 GB.

## Running

```bash
./run.sh mining.run_study                    # whole corpus, every stage
./run.sh mining.run_study django/django      # one repository
./run.sh mining.run_study --from pairs       # resume at a stage
./run.sh pytest tests -q                     # the measurement tests
```

Stages, in order: `prs` → `refs` → `map` → `filter` → `pairs` → `windows` →
`semantic` → `survivorship` → `audit` → `profile` → `release`, then the global
`analysis` and `plots`. Every stage skips work already in the cache, so a re-run
completes in seconds; pass `--force` to recompute anyway.

## The pipeline

| Module | Stage | What it does |
|---|---|---|
| `github_api.py` | — | rate-limit-aware REST client, standard library only |
| `fetch_prs.py` | 23.1a | pages closed PRs into a per-repo SQLite cache |
| `fetch_refs.py` | 23.1b | bare clone + `refs/pull/<n>/head`, so no per-diff API calls |
| `node_map.py` | 23.2 | re-attaches line spans to MAK's own fragment decomposition |
| `diff_parse.py` | 23.2 | parses `git diff -U0 -M` into hunks |
| `hunks_to_nodes.py` | 23.2 | maps each PR's hunks onto the node ids it writes |
| `filters.py` | — | buckets bots, sweeps, generated paths; counts, never drops silently |
| `rebase.py` | — | expresses two changes against one base before merging them |
| `pair_analysis.py` | 23.3 | concurrent pairs, overlap sets, `merge-tree` verdict |
| `window_analysis.py` | 23.4 | the *k*-window replay |
| `semantic_probe.py` | 23.5 | RQ6, using MAK's own cross-node checks |
| `profile_export.py` | 23.6 | `profile.json` per repo |
| `survivorship.py` | — | do abandoned PRs collide more than merged ones? |
| `release.py` | 23.8 | the derived dataset, as CSV |
| `analysis.py` / `plots.py` | 23.7 | tables and figures |
| `audit.py` | — | mapper accuracy, `merge-tree` versus a real `git merge`, and the naive-merge bias |

## Reading the results

Start with [`CONTENTION_STUDY.md`](CONTENTION_STUDY.md), then
[`data/RESULTS.md`](data/RESULTS.md) for the generated tables.

| Figure | What it shows |
|---|---|
| `01-collision-vs-k` | **the headline** — collision probability against concurrency, at three granularities |
| `02-pair-overlap` | per-pair probability of sharing a Python file versus a Python AST node |
| `03-node-popularity` | how heavy the tail of node write frequency is |
| `04-hot-node-categories` | what kind of structure the hottest nodes are |
| `05-naive-merge-bias` | why the obvious way to measure conflicts is wrong |
| `06-lock-chain-vs-k` | how deep a queue forms behind one file lock versus one node lock |
| `07-change-footprint` | how wide a single merged change is |

Each figure is written twice, `<name>.png` and `<name>-dark.png`.
