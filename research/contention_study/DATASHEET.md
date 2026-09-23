# Datasheet — MAK contention study, derived dataset

This datasheet describes the data released under `research/contention_study/data/`. It
follows the shape of Gebru et al.'s *Datasheets for Datasets*, cut down to the
sections that apply to a derived measurement of public repository history.

## Motivation

The dataset exists to make one claim checkable: that concurrent changes to a large
Python codebase collide far more often at **file** granularity than at
**AST-node** granularity, and that most textual merge conflicts therefore involve
no shared node. It was produced for Wave 23 of the Multi Agent Kernel project. No
one funded it; it is derived entirely from public data using a personal GitHub
token's free rate limit.

## Composition

Each repository directory `data/<owner>__<name>/` contains:

| File | One row per | Notes |
|---|---|---|
| `release/changes.csv` | merged or closed PR in the study slice | footprint sizes, filter bucket, fork and merge timestamps, an `automated` flag |
| `release/pairs.csv` | analysed concurrent pair | overlap sets and the three-way merge verdict |
| `release/windows.csv` | *k*-window replay | contended files and nodes, longest lock chain |
| `release/semantic.csv` | pair in the RQ6 probe | static-defect counts for base, each side, and the merge |
| `release/node_writes.csv` | AST node | how many distinct changes wrote to it, and its category |
| `profile.json` | repository | the distributions a synthetic workload generator needs |
| `audit_sample.md` | pair | stratified 2x2 sample laid out for manual review |

`data/RESULTS.md` and `data/results.json` hold the aggregate tables across all
repositories. Exact row counts per file are printed by `mining/release.py` and
recorded in `RESULTS.md`.

**Instances are changes and pairs of changes, not people.** A node id is a path
plus a symbol name — e.g. `src/transformers/models/auto/modeling_auto.py::module_header::__header__`.
Source code is **not** redistributed; the node ids name locations in repositories
that remain the authoritative copy.

**Missing data is labelled, not imputed.** A PR whose head commit or fork point
could not be resolved is kept with `status` set accordingly and bucketed
`unusable`; it is never silently dropped.

## Collection process

- **PR metadata** came from the GitHub REST API's closed-PR listing, paginated at
  100 per page with an authenticated token.
- **PR code** came from git, not the API: each repository was cloned bare and
  `refs/pull/<n>/head` fetched, which exposes every PR's final head commit even
  when the PR was squash-merged.
- **Node decomposition** is produced by the kernel itself
  (`mak.node_store.ingestion.parse_file_into_fragments`), with line spans
  re-attached in `mining/node_map.py`. The study's node model is therefore not an
  approximation of MAK's — it is MAK's.
- **Merge verdicts** come from `git merge-tree --write-tree` after both changes
  are expressed against a shared base; see `mining/rebase.py` for why the naive
  call is unsound here.
- The clone SHA of each repository's integration branch and the fetch timestamp
  are recorded in every `profile.json` under `window`, so a re-run can be pinned
  to the same history.

The sampling frame is the most recent merged PRs inside the study window, taken
as a **contiguous slice** rather than a random sample, because concurrency
structure is the object of study and random sampling would destroy it.

## Preprocessing and filtering

Every filter is a labelled bucket, and each bucket's size is reported in
`RESULTS.md`. Nothing is deleted:

- `bot` — automated authors (`user.type == "Bot"` or a known service-account login).
- `oversize` — more than 100 changed files or more than 5,000 changed lines.
- `whitespace_sweep` — under 20% of changed lines survive `git diff -w`.
- `unusable` — head commit or fork point unresolvable.
- `no_footprint` — no node touches after generated paths are excluded.

Generated and machine-maintained paths (lockfiles, compiled translations,
protobuf stubs, `generated/` directories, `AUTHORS`) are excluded from node sets
and counted separately.

## Uses

Appropriate: replicating the RQ1–RQ6 tables; calibrating a synthetic concurrent
workload; comparing node-level against file-level contention in other tools.

**Not** appropriate: anything about individual contributors. The dataset carries
no author identity beyond a boolean automation flag, and was not built to support
per-person analysis.

Known limitations that bound every use — measured contention is a **lower bound**
because human teams coordinate to avoid it; PRs abandoned *because of* conflicts
are invisible in the merged stream; node decomposition applies to Python only.
These are argued in full in the "Threats to validity" section of
[`CONTENTION_STUDY.md`](CONTENTION_STUDY.md).

## Distribution and maintenance

The dataset ships inside the `multi-agent-kernel` repository under the project's
MIT licence. It describes public repositories that carry their own licences; no
content from them is included. It is a snapshot, not a service: regenerating it
is one command (`./run.sh mining.run_study`), so the intended maintenance model
is re-running the pipeline rather than patching the CSVs.
