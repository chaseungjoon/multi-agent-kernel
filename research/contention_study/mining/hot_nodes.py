"""RQ4 — how concentrated node-level contention is, and what the hot nodes are.

Two questions. First, the *shape*: is the write-frequency distribution heavy
tailed, and does a discrete power law fit its tail? Second, the *content*: are
the hottest nodes the ones MAK's design predicts — import headers, registries,
settings tables and URL maps — rather than ordinary business logic.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from mining.cache import CacheHandle
from mining.filters import is_generated

CATEGORY_HEADER = "import_header"
CATEGORY_REGISTRY = "registry"
CATEGORY_SETTINGS = "settings"
CATEGORY_URLS = "url_table"
CATEGORY_DOCS = "docs_changelog"
CATEGORY_TESTS = "tests"
CATEGORY_BUILD = "build_ci"
CATEGORY_OTHER = "other"

_REGISTRY_PATH = re.compile(
    r"(^|/)([a-z_]*(const|constants|registry|registries|catalog|manifest|"
    r"factory|factories|features|plugins?|providers?|backends?|loader))\.py$"
    r"|(^|/)__init__\.py$"
)
_REGISTRY_NAME = re.compile(
    r"(REGISTRY|_MAP$|_MAPPING|_TABLE|_REGISTRY|ALL_|_ALL$|CHOICES|FEATURES|"
    r"register|_register|get_provider|_LOOKUP|Features$|Registry$|Catalog$)"
)
_SETTINGS_PATH = re.compile(
    r"(^|/)[a-z_]*(settings|config|conf|defaults|options)\.py$"
    r"|(^|/)conf/|"
    r"^(pyproject\.toml|setup\.cfg|setup\.py|tox\.ini|mypy\.ini|\.pre-commit-config\.yaml)$"
)
_URL_PATH = re.compile(r"(^|/)(urls|routes|routers|endpoints)\.py$")
# Deliberately narrow: matching every ``.txt`` would swallow pinned dependency
# manifests, which are build configuration rather than prose.
_DOC_PATH = re.compile(
    r"(^|/)(docs?|doc|documentation)/|\.(rst|md)$"
    r"|(^|/)(CHANGELOG|CHANGES|NEWS|AUTHORS|CONTRIBUTORS|HISTORY|README)"
)
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$")
_BUILD_PATH = re.compile(
    r"^\.github/|(^|/)(Makefile|Dockerfile|CODEOWNERS)$"
    r"|(^|/)[a-z_]*(requirements|constraints|package_constraints)[^/]*\.txt$"
    r"|\.(ya?ml|cfg|ini|toml)$|(^|/)\.[a-z-]+$"
)


@dataclass(frozen=True, slots=True)
class HotNode:
    """One node and how many distinct changes wrote to it."""

    node_id: str
    path: str
    kind: str
    writes: int
    category: str


@dataclass(frozen=True, slots=True)
class PowerLawFit:
    """A discrete power-law fit of the write-frequency tail.

    ``alpha`` is the Clauset-Shalizi-Newman MLE exponent for counts at or above
    ``x_min``; ``r_squared`` is the goodness of an ordinary least-squares line
    through the log-log rank-frequency plot, reported because it is the figure
    readers will actually look at.
    """

    alpha: float
    x_min: int
    tail_size: int
    r_squared: float
    gini: float


def categorise(node_id: str, kind: str) -> str:
    """Bucket a node by what kind of shared structure it is.

    Order matters: a header inside a registry module is counted as a header,
    because the import list is the thing two changes actually collide on.
    """
    path = node_id.split("::", 1)[0]
    name = node_id.rsplit("::", 1)[-1]
    if kind == "module_header":
        return CATEGORY_HEADER
    if _TEST_PATH.search(path):
        return CATEGORY_TESTS
    if _BUILD_PATH.search(path):
        return CATEGORY_BUILD
    if _DOC_PATH.search(path):
        return CATEGORY_DOCS
    if _URL_PATH.search(path):
        return CATEGORY_URLS
    if _SETTINGS_PATH.search(path):
        return CATEGORY_SETTINGS
    if _REGISTRY_PATH.search(path) or _REGISTRY_NAME.search(name):
        return CATEGORY_REGISTRY
    return CATEGORY_OTHER


def write_counts(handle: CacheHandle, *, python_only: bool) -> Counter[str]:
    """Count the distinct changes that wrote to each node, over the kept bucket.

    Generated paths are excluded here for the same reason they are excluded from
    the footprints: contention in a file a code generator rewrites wholesale says
    nothing about how developers divide work.
    """
    clause = " AND nd.is_python = 1 AND nd.kind != 'file'" if python_only else ""
    rows = handle.conn.execute(
        "SELECT nd.node_id, nd.path, COUNT(DISTINCT nd.number) AS writes"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        f" WHERE ch.bucket = 'kept'{clause}"
        " GROUP BY nd.node_id, nd.path",
    ).fetchall()
    return Counter(
        {
            str(row["node_id"]): int(row["writes"])
            for row in rows
            if not is_generated(str(row["path"]))
        }
    )


def hot_nodes(handle: CacheHandle, limit: int, *, python_only: bool) -> list[HotNode]:
    """Return the ``limit`` most-written nodes, each tagged with its category."""
    clause = " AND nd.is_python = 1 AND nd.kind != 'file'" if python_only else ""
    rows = handle.conn.execute(
        "SELECT nd.node_id, nd.path, nd.kind, COUNT(DISTINCT nd.number) AS writes"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        f" WHERE ch.bucket = 'kept'{clause}"
        " GROUP BY nd.node_id, nd.path ORDER BY writes DESC LIMIT ?",
        (limit * 2,),
    ).fetchall()
    rows = [row for row in rows if not is_generated(str(row["path"]))][:limit]
    return [
        HotNode(
            node_id=str(row["node_id"]),
            path=str(row["path"]),
            kind=str(row["kind"]),
            writes=int(row["writes"]),
            category=categorise(str(row["node_id"]), str(row["kind"])),
        )
        for row in rows
    ]


def gini(counts: list[int]) -> float:
    """Gini coefficient of a write-count distribution (0 = flat, 1 = one node)."""
    if not counts:
        return 0.0
    ordered = sorted(counts)
    total = sum(ordered)
    if total == 0:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    n = len(ordered)
    return (2 * weighted) / (n * total) - (n + 1) / n


def fit_power_law(counts: list[int]) -> PowerLawFit:
    """Fit a discrete power law to the tail of a write-frequency distribution.

    ``x_min`` is chosen as the value minimising the Kolmogorov-Smirnov distance
    between the empirical tail and the fitted model, over the candidate range
    1..10 — the standard Clauset procedure, restricted because the study's tails
    are short enough that a wider sweep only adds noise.
    """
    positive = [c for c in counts if c > 0]
    if len(positive) < 20:
        return PowerLawFit(alpha=float("nan"), x_min=0, tail_size=0,
                           r_squared=float("nan"), gini=gini(counts))

    best = (float("inf"), 1, float("nan"), 0)
    for x_min in range(1, 11):
        tail = [c for c in positive if c >= x_min]
        if len(tail) < 20:
            break
        denominator = sum(math.log(c / (x_min - 0.5)) for c in tail)
        if denominator <= 0:
            continue
        alpha = 1.0 + len(tail) / denominator
        distance = _ks_distance(tail, x_min, alpha)
        if distance < best[0]:
            best = (distance, x_min, alpha, len(tail))
    _, x_min, alpha, tail_size = best

    return PowerLawFit(
        alpha=alpha,
        x_min=x_min,
        tail_size=tail_size,
        r_squared=_rank_frequency_r2(positive),
        gini=gini(counts),
    )


def _ks_distance(tail: list[int], x_min: int, alpha: float) -> float:
    """Kolmogorov-Smirnov distance between the empirical tail and the fit.

    The model CDF is built once as a prefix sum over the support, so the whole
    comparison is linear rather than quadratic in the largest observed count.
    """
    ordered = sorted(tail)
    n = len(ordered)
    support = ordered[-1] + 1
    weights = [(value - x_min + 1) ** -alpha for value in range(x_min, support + 1)]
    normaliser = sum(weights)
    if normaliser <= 0:
        return float("inf")
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running / normaliser)

    worst = 0.0
    for index, value in enumerate(ordered):
        model = cumulative[min(value - x_min, len(cumulative) - 1)]
        worst = max(worst, abs((index + 1) / n - model))
    return worst


def _rank_frequency_r2(counts: list[int]) -> float:
    """R^2 of a least-squares line through the log-log rank-frequency plot."""
    ordered = sorted(counts, reverse=True)
    xs = [math.log(index + 1) for index in range(len(ordered))]
    ys = [math.log(value) for value in ordered]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    if sxx == 0:
        return float("nan")
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residual = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    total = sum((y - mean_y) ** 2 for y in ys)
    return 1.0 - residual / total if total else float("nan")


def category_shares(nodes: list[HotNode]) -> list[tuple[str, int, int]]:
    """``(category, node_count, total_writes)`` over a set of hot nodes."""
    node_counts: Counter[str] = Counter()
    write_totals: Counter[str] = Counter()
    for node in nodes:
        node_counts[node.category] += 1
        write_totals[node.category] += node.writes
    return sorted(
        (
            (category, node_counts[category], write_totals[category])
            for category in node_counts
        ),
        key=lambda row: row[2],
        reverse=True,
    )
