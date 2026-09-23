# Real-world contention in open-source Python history

## Question

MAK decomposes a repository into independently lockable AST nodes rather than
locking whole files. That design assumes concurrent changes often meet in one
file without touching the same symbol. This study asks:

> When independent changes to a large Python codebase happen concurrently, how
> often do they collide at file level versus AST-node level, and what does each
> granularity cost as concurrency grows?

The corpus covers six Python-dominant repositories from 2025-01-01 through
2026-09-01: `home-assistant/core`, `apache/airflow`,
`huggingface/transformers`, `pandas-dev/pandas`,
`scikit-learn/scikit-learn`, and `django/django`.

Every number quoted below is generated from the per-repository caches. The full
tables are in [`data/RESULTS.md`](data/RESULTS.md), the machine-readable form is
[`data/results.json`](data/results.json), and every figure is in [`plots/`](plots/).

## Findings

### 1. Node granularity materially reduces contention

For a genuinely concurrent pair, the chance of sharing a Python AST node is
between **2.2x and 10.3x lower** than the chance of sharing a Python file:

| repository | same Python file | same Python node | reduction |
|---|---:|---:|---:|
| home-assistant | 0.13% | 0.06% | 2.2x |
| airflow | 0.29% | 0.11% | 2.8x |
| transformers | 1.46% | 0.46% | 3.2x |
| pandas | 2.54% | 0.25% | 10.3x |
| scikit-learn | 1.14% | 0.36% | 3.2x |
| django | 0.84% | 0.15% | 5.7x |

The advantage persists in the counterfactual wave replay. With eight changes
dispatched from one base, Python-file collision ranges from 15.2% to 53.0%; the
corresponding node collision ranges from 11.4% to 27.7%. At larger *k*, both
curves eventually saturate, but node-level saturation consistently arrives
later and produces a shorter queue behind the busiest lock.

This supports the kernel's decomposition choice. It does not support the
stronger claim that AST locking makes contention disappear.

### 2. Whole-file resources set the practical ceiling

The all-path collision curve rises faster than either Python curve. At *k*=16,
78.9% to 99.4% of windows have some path collision, while 32.9% to 66.5% have a
Python-node collision. Non-Python files account for 4.4% to 22.9% of measured
node touches and are necessarily represented as whole-file locks.

The recurring hot resources include build and CI configuration, dependency
sets, documentation and changelogs, registries, and machine-maintained files.
Improving Python granularity cannot remove those queues. Structured append
support for list-like text files, plus deterministic regeneration for generated
artifacts, is likely a higher-value next step than splitting Python nodes more
finely.

### 3. Textual merge conflicts are not the main observed cost

The corrected pair analysis found **no change-versus-change textual conflict**
among 124,473 resolvable sampled pairs. Nine additional pairs failed while
forward-porting a change across mainline and are reported separately as
`rebase_conflict`; they are not charged to the other change.

This makes RQ2 unidentifiable in this sample: with no textual conflicts, there
is no denominator for estimating the fraction that shared no node. It does,
however, answer RQ3 strongly. All **5,316 shared-node pairs** merged cleanly,
and 3,324 of those involved append-only edits to every shared node.

Sequential replay begins to produce a few textual conflicts only at larger
waves: Transformers at *k*=64, pandas from *k*=16, and Django from *k*=32.
Home Assistant, Airflow, and scikit-learn remained conflict-free in the sampled
replays through *k*=64.

The data therefore supports a scheduling argument for MAK—shorter lock queues
and fewer stale reads—more directly than a merge-conflict argument.

### 4. Contention is heavy tailed, but its shape varies by repository

Node write frequencies have fitted power-law exponents from 1.91 to 2.22 and
log-log R² from 0.766 to 0.928. Gini coefficients range from 0.178 to 0.400.
Contention is concentrated, but not uniformly so.

Import headers recur among the hottest Python nodes in every repository. Other
leaders are repository-specific: Django settings and database feature tables,
scikit-learn's dependency definitions and estimators, Airflow scheduling and
selective-check machinery, and model registration files in Transformers.
Append-only shares also vary widely, so a commutative import/header operation is
promising but should be measured per workload rather than assumed universal.

### 5. The semantic probe found no shallow emergent defect

None of the **2,400 cleanly merged pairs** introduced a MAK detector finding
that was absent from the base and both sides independently. Each repository's
0/400 result has a 95% Wilson upper bound of 0.95%; pooling the sample gives an
upper bound of approximately 0.16%.

This is a narrow negative result. The detector covers structural checks such as
signatures, imports, name collisions, and registry keys. It is not a type
checker, does not execute tests, and does not establish behavioral correctness.

### 6. Survivorship bias is real but not one-directional in every repository

Abandoned PRs collide more often than merged PRs in Home Assistant,
Transformers, and Django, and less often in Airflow, pandas, and scikit-learn.
Pooling all repositories yields 5.31% node overlap for abandoned changes versus
3.49% for merged changes, but that pooled comparison is sensitive to the
different repository mix and should not be treated as a causal estimate.

The defensible conclusion is that restricting the study to merged history
changes the measured contention distribution. It does not justify a universal
multiplier.

## Method

### Data acquisition

PR metadata is fetched from GitHub's public REST API at 100 records per page.
Code comes from bare git clones: `refs/pull/<n>/head` is fetched in batches, so
the pipeline can reconstruct squash-merged PRs without one API request per diff.
Clone SHAs and fetch timestamps are recorded in each repository profile.

The sampling frame is a contiguous recent slice rather than a random set of
PRs, because random sampling would destroy the overlap structure being studied.
Bot changes, oversized sweeps, whitespace-only sweeps, unresolvable heads, and
empty footprints are assigned explicit buckets and retained in the cache.
Generated paths are labelled and excluded from the contention sets.

### Mapping diffs to MAK nodes

`git diff -U0 -M` supplies changed line ranges. Zero context avoids charging a
one-line edit with its unchanged neighbors. Removed lines are mapped against the
base tree and added lines against the PR head, so a newly added function becomes
a new node rather than an edit to whichever node preceded it.

Python files are decomposed by
`mak.node_store.ingestion.parse_file_into_fragments`. `mining/node_map.py`
re-attaches line spans to those fragments and handles whitespace gaps that the
ingestion representation omits. Non-Python, binary, and unparseable files become
one whole-file node instead of being discarded.

### Defining concurrency

Lifetime overlap—both PRs open at once—is the initial candidate relation. It is
not sufficient for merge analysis because GitHub exposes a PR's final head after
rebases and force pushes. A long-lived PR may therefore have a final fork point
that already contains another PR whose lifetime it overlapped.

The reported pair results use **base overlap**: neither change's fork point
postdates the other's merge. This is the subset for which the final heads still
represent independent changes.

### Removing mainline from the merge test

A bare `git merge-tree A B` uses the earlier fork as its implicit merge base and
therefore charges intervening mainline commits to one side. The naive method
reported conflict rates from 10.7% to 29.6%; 51.7% to 98.4% of those reported
conflicts occurred between PR diffs that shared no file.

The corrected procedure uses the later fork as the pair's shared base,
forward-ports the earlier change onto it, and then merges the two change trees
against that base. A forward-port failure is recorded separately. On 156 audited
pairs, the corrected verdict agreed with a real `git merge` 100% of the time.

### Replaying waves

For each *k* in 2, 4, 8, 16, 32, and 64, the study takes consecutive merged
changes and computes file and node contention over every window. A sample of
windows is also replayed through git. Each change is expressed against the
window's earliest fork, then folded into an accumulated tree. A failed git
operation invalidates the replay window; it is never counted as a source-level
conflict.

### Validation

The node mapper is checked against an independent Python `ast` walk. Across the
six repositories, 97.79% to 98.72% of audited files were fully covered and
symbol recall was 98.85% to 99.38%. The merge oracle uses sparse throwaway
worktrees and real `git merge` commands. The study implementation also has 54
unit and end-to-end tests, including regressions for mainline contamination and
for infrastructure failures being misclassified as conflicts.

## Implications for MAK

1. **Keep AST-node locking.** The observed reduction is large enough to matter,
   especially as a wave grows.
2. **Add representations for recurring non-Python structures.** Dependency
   lists, changelogs, configuration tables, and generated manifests become the
   ceiling once Python is decomposed.
3. **Prototype commutative header edits.** Import headers are repeatedly hot,
   and many observed touches append rather than replace content.
4. **Frame the benefit as concurrency control.** Human pairwise merge conflicts
   are nearly absent after correcting the base. The measured gain is reduced
   serialization and stale-work risk in larger waves.
5. **Retain explicit failure categories.** Change-versus-mainline conflicts,
   change-versus-change conflicts, and git/infrastructure failures answer
   different questions and must never share one counter.

## Threats to validity

**Human coordination lowers observed contention.** Developers divide work,
communicate, rebase, and abandon changes. Agent waves dispatched from one base
need not exhibit the same behavior, so historical rates are a lower-bound proxy,
not a direct forecast.

**Final heads erase evidence.** A conflict resolved during review is already
absent from GitHub's final PR head. Base overlap prevents vacuous merges but
cannot recover intermediate revisions that were force-pushed away.

**The wave replay is counterfactual.** Human PRs unfold over days; agent tasks
may take minutes. Consecutive historical changes are realistic workloads but
were not actually authored simultaneously from one base.

**The corpus is selected.** Six large, Python-heavy, well-maintained projects
are relevant to MAK's target workload but are not a random sample of software.

**The mapper is not perfect.** Its measured recall is high but below 100%.
Fallback whole-file nodes are conservative: they can overstate contention rather
than silently erase an unparseable file.

**Pair verdicts are sampled in four repositories.** The sample is complete for
scikit-learn and Django and covers 16.3% to 97.1% of the base-overlap population
elsewhere. Population overlap rates use all pairs; merge verdicts use the sample.

**The semantic oracle is shallow.** Zero detector findings is not evidence that
all merged behavior is correct.

## Related work

This study complements empirical work on merge conflicts (Ghiotto et al.,
*IEEE TSE*, 2020), proactive collaboration-conflict detection in Crystal (Brun
et al., ESEC/FSE 2011), Palantír (Sarma et al., ICSE 2003), and ConE (Maddila et
al., *ACM TOSEM*, 2022), and conflict-minimizing task scheduling in Cassandra
(Kasi and Sarma, ICSE 2013). Structured merge systems such as JDime and Spork
operate after branches diverge; MAK's locks operate before edits are admitted.

The concurrency-control analogy is the database tradition of two-phase locking,
optimistic concurrency control, and multiversion concurrency control. The
contribution here is empirical: source code decomposed into AST nodes has a
contention profile that makes the finer lock granularity useful.
