"""Stage 23.7b — render the study's figures into ``contention_study/plots``.

Every figure is written twice, in light and dark, from the same data and the same
validated categorical order. Series are direct-labelled rather than relying on a
legend box alone, which is what the palette's light-mode contrast warning
requires and what keeps identity from being carried by colour alone.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from mining.config import StudyConfig, load_config
from mining.jsonval import as_float, as_int, as_mapping, as_sequence, as_text
from mining.theme import THEMES, Theme, apply, strip_spines

SHORT_NAMES = {
    "home-assistant/core": "home-assistant",
    "apache/airflow": "airflow",
    "huggingface/transformers": "transformers",
    "pandas-dev/pandas": "pandas",
    "scikit-learn/scikit-learn": "scikit-learn",
    "django/django": "django",
}


def short(slug: str) -> str:
    """Compact repository label for axes and panel titles."""
    return SHORT_NAMES.get(slug, slug.split("/")[-1])


def _load(config: StudyConfig) -> list[dict[str, object]]:
    """Read the combined results document produced by ``analysis.py``."""
    path = config.data_root / "results.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing; run `mining.analysis` first")
    return list(json.loads(path.read_text())["repos"])


def _curves(repo: dict[str, object]) -> list[object]:
    """Return the per-k window curve of one repository's profile."""
    return as_sequence(as_mapping(repo["profile"]).get("window_curves"))


def _probability(repo: dict[str, object], key: str) -> float:
    """Return one per-pair probability out of a repository's profile."""
    probabilities = as_mapping(as_mapping(repo["profile"]).get("pair_probabilities"))
    return as_float(probabilities.get(key))


def _grid(count: int) -> tuple[int, int]:
    """Rows and columns for a small-multiples grid of ``count`` panels."""
    columns = min(3, count)
    rows = math.ceil(count / columns)
    return rows, columns


def _label_edges(
    panels: list[Axes], rows: int, columns: int, xlabel: str, ylabel: str, theme: Theme
) -> None:
    """Label only the outer panels of a grid.

    Figure-level ``supxlabel``/``supylabel`` collide with an outside legend under
    constrained layout, so the edge panels carry the labels instead.
    """
    for index, panel in enumerate(panels):
        if index // columns == rows - 1 or index + columns >= len(panels):
            panel.set_xlabel(xlabel, color=theme.text_secondary, fontsize=9)
        if index % columns == 0:
            panel.set_ylabel(ylabel, color=theme.text_secondary, fontsize=9)


def figure_collision_vs_k(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """Headline figure: collision probability against concurrency k.

    Three series, because two would mislead. "any path" includes the non-Python
    files MAK can only lock whole, and it is the ceiling the kernel cannot get
    under today. Within Python, the gap between file and node granularity is the
    parallelism the node decomposition actually buys.
    """
    rows, columns = _grid(len(repos))
    figure, axes = plt.subplots(
        rows, columns, figsize=(4.2 * columns, 3.4 * rows),
        sharex=True, sharey=True, layout="constrained",
    )
    flat = axes.ravel() if hasattr(axes, "ravel") else [axes]
    series = (
        ("p_file_collision", "any path", 0, "o"),
        ("p_py_file_collision", "Python file", 1, "s"),
        ("p_py_node_collision", "Python AST node", 2, "^"),
    )
    for panel, repo in zip(flat, repos, strict=False):
        curves = [as_mapping(item) for item in _curves(repo)]
        ks = [as_int(point["k"]) for point in curves]
        for key, label, slot, marker in series:
            values = [100 * as_float(point.get(key)) for point in curves]
            panel.plot(
                ks, values, color=theme.color(slot), marker=marker, label=label
            )
            if ks:
                # Labels go above a low curve and below a high one, staggered by
                # slot, so three series near the same value stay readable.
                above = values[-1] < 60
                offset = (9 + 12 * slot) if above else -(11 + 12 * slot)
                panel.annotate(
                    label, (ks[-1], values[-1]), textcoords="offset points",
                    xytext=(-8, offset), ha="right", fontsize=8,
                    color=theme.text_secondary,
                )
        panel.set_xscale("log", base=2)
        panel.set_xticks(ks)
        panel.set_xticklabels([str(k) for k in ks])
        panel.set_ylim(-3, 105)
        panel.set_title(short(as_text(repo["slug"])))
        strip_spines(panel)
    for extra in flat[len(repos) :]:
        extra.set_visible(False)
    _label_edges(
        list(flat[: len(repos)]), rows, columns,
        "changes dispatched from one base (k)",
        "windows with a collision (%)", theme,
    )
    figure.suptitle(
        "Node-level collision saturates later than file-level collision",
        color=theme.text_primary, fontsize=13, fontweight="semibold",
    )
    handles, labels = flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="outside lower center", ncols=3)
    return figure


def figure_pair_overlap(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """RQ1: per-pair probability of touching the same file versus the same node."""
    figure, panel = plt.subplots(
        figsize=(8.4, 0.62 * len(repos) + 2.4), layout="constrained"
    )
    labels = [short(str(repo["slug"])) for repo in repos]
    positions = range(len(repos))
    height = 0.36
    file_share = [100 * _probability(repo, "same_python_file") for repo in repos]
    node_share = [100 * _probability(repo, "same_python_node") for repo in repos]
    paired = zip(file_share, node_share, strict=True)
    for index, (file_value, node_value) in enumerate(paired):
        panel.barh(
            index + height / 2 + 0.01, file_value, height=height,
            color=theme.color(0), label="same Python file" if index == 0 else None,
        )
        panel.barh(
            index - height / 2 - 0.01, node_value, height=height,
            color=theme.color(1), label="same Python AST node" if index == 0 else None,
        )
        panel.text(
            file_value, index + height / 2 + 0.01, f"  {file_value:.2f}%",
            va="center", fontsize=9, color=theme.text_secondary,
        )
        panel.text(
            node_value, index - height / 2 - 0.01, f"  {node_value:.2f}%",
            va="center", fontsize=9, color=theme.text_secondary,
        )
    panel.set_yticks(list(positions))
    panel.set_yticklabels(labels)
    panel.set_xlabel("share of concurrent pairs (%)")
    panel.set_title(
        "Two concurrent changes rarely meet at node granularity",
        color=theme.text_primary,
    )
    panel.grid(axis="y", visible=False)
    panel.legend(loc="lower right")
    strip_spines(panel)
    return figure


def figure_node_popularity(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """RQ4: complementary CDF of node write frequency, log-log."""
    figure, panel = plt.subplots(figsize=(8.0, 5.4), layout="constrained")
    for index, repo in enumerate(repos):
        popularity = as_mapping(as_mapping(repo["profile"]).get("node_popularity"))
        histogram = [
            (as_int(as_sequence(item)[0]), as_int(as_sequence(item)[1]))
            for item in as_sequence(popularity.get("writes_histogram"))
        ]
        if not histogram:
            continue
        total = sum(count for _value, count in histogram)
        xs: list[int] = []
        ys: list[float] = []
        remaining = total
        for value, count in histogram:
            xs.append(int(value))
            ys.append(remaining / total)
            remaining -= count
        panel.plot(xs, ys, color=theme.color(index), marker="o", markersize=3.5,
                   label=short(str(repo["slug"])))
        panel.annotate(
            short(str(repo["slug"])), (xs[-1], ys[-1]), textcoords="offset points",
            xytext=(6, 0), fontsize=9, color=theme.text_secondary,
        )
    panel.set_xscale("log")
    panel.set_yscale("log")
    panel.set_xlabel("distinct changes writing to a node")
    panel.set_ylabel("share of nodes written at least that often")
    panel.set_title(
        "Node write frequency is heavy tailed: a few nodes absorb most writes",
        color=theme.text_primary,
    )
    panel.legend(loc="lower left", ncols=2)
    strip_spines(panel)
    return figure


def figure_hot_categories(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """RQ4: what kind of structure the hottest nodes are."""
    keep = (
        "import_header", "registry", "settings", "url_table",
        "build_ci", "tests", "docs_changelog",
    )
    figure, panel = plt.subplots(
        figsize=(8.6, 0.62 * len(repos) + 2.8), layout="constrained"
    )
    labels = [short(str(repo["slug"])) for repo in repos]
    bottoms = [0.0] * len(repos)
    for slot, category in enumerate((*keep, "other")):
        values: list[float] = []
        for repo in repos:
            rows = [
                as_mapping(item)
                for item in as_sequence(
                    as_mapping(repo["profile"]).get("hot_node_categories")
                )
            ]
            total = sum(as_int(row["writes"]) for row in rows) or 1
            if category == "other":
                writes = sum(
                    as_int(row["writes"])
                    for row in rows
                    if as_text(row["category"]) not in keep
                )
            else:
                writes = sum(
                    as_int(row["writes"])
                    for row in rows
                    if as_text(row["category"]) == category
                )
            values.append(100 * writes / total)
        color = theme.muted if category == "other" else theme.color(slot)
        panel.barh(
            labels, values, left=bottoms, height=0.6, color=color,
            label=category.replace("_", " "), edgecolor=theme.surface, linewidth=2,
        )
        for index, value in enumerate(values):
            if value >= 9:
                panel.text(
                    bottoms[index] + value / 2, index, f"{value:.0f}%",
                    ha="center", va="center", fontsize=8.5, color=theme.surface,
                )
        bottoms = [b + v for b, v in zip(bottoms, values, strict=True)]
    panel.set_xlabel("share of writes to the 200 hottest nodes (%)")
    panel.set_xlim(0, 100)
    panel.grid(axis="y", visible=False)
    panel.set_title(
        "Import headers recur in every repository's hot-node mix",
        color=theme.text_primary,
    )
    figure.legend(loc="outside lower center", ncols=4)
    strip_spines(panel)
    return figure


def figure_naive_bias(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """How much of a bare ``git merge-tree`` conflict rate is mainline progress.

    This is the study's methodological result, and the reason the 2x2 of textual
    conflict against shared node is almost empty: measured the obvious way, these
    repositories look full of conflicts between concurrent pull requests. Almost
    none of those conflicts are between the two pull requests.
    """
    figure, panel = plt.subplots(
        figsize=(8.6, 0.68 * len(repos) + 2.6), layout="constrained"
    )
    labels = [short(as_text(repo["slug"])) for repo in repos]
    height = 0.36
    for index, repo in enumerate(repos):
        bias = as_mapping(as_mapping(repo["validation"]).get("naive_bias"))
        naive = 100 * as_float(bias.get("naive_rate"))
        corrected = 100 * as_float(bias.get("corrected_rate"))
        panel.barh(
            index + height / 2 + 0.01, naive, height=height, color=theme.color(1),
            label="lifetime overlap + bare merge-tree" if index == 0 else None,
        )
        panel.barh(
            index - height / 2 - 0.01, corrected, height=height,
            color=theme.color(2),
            label="both changes on a shared base" if index == 0 else None,
        )
        panel.text(
            naive, index + height / 2 + 0.01, f"  {naive:.1f}%", va="center",
            fontsize=9, color=theme.text_secondary,
        )
        panel.text(
            corrected, index - height / 2 - 0.01, f"  {corrected:.2f}%", va="center",
            fontsize=9, color=theme.text_secondary,
        )
    panel.set_xlim(left=0)
    panel.set_yticks(list(range(len(repos))))
    panel.set_yticklabels(labels)
    panel.set_xlabel("concurrent pairs reported as conflicting (%)")
    panel.grid(axis="y", visible=False)
    panel.set_title(
        "Most 'conflicts' between concurrent pull requests are mainline progress",
        color=theme.text_primary,
    )
    figure.legend(loc="outside lower center", ncols=2)
    strip_spines(panel)
    return figure


def figure_lock_chain(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """How deep a queue forms behind the busiest lock, file versus node."""
    rows, columns = _grid(len(repos))
    figure, axes = plt.subplots(
        rows, columns, figsize=(4.2 * columns, 3.4 * rows),
        sharex=True, sharey=True, layout="constrained",
    )
    flat = axes.ravel() if hasattr(axes, "ravel") else [axes]
    for panel, repo in zip(flat, repos, strict=False):
        curves = [as_mapping(item) for item in _curves(repo)]
        ks = [as_int(point["k"]) for point in curves]
        file_chain = [as_float(point.get("mean_max_py_file_chain")) for point in curves]
        node_chain = [as_float(point.get("mean_max_py_node_chain")) for point in curves]
        panel.plot(
            ks, file_chain, color=theme.color(1), marker="s", label="Python file lock"
        )
        panel.plot(
            ks, node_chain, color=theme.color(2), marker="^", label="Python node lock"
        )
        panel.plot(
            ks, ks, color=theme.muted, linestyle=":", linewidth=1.4,
            label="full serialisation",
        )
        if ks:
            panel.annotate(
                "file", (ks[-1], file_chain[-1]), textcoords="offset points",
                xytext=(-8, 8), ha="right", fontsize=8, color=theme.text_secondary,
            )
            panel.annotate(
                "node", (ks[-1], node_chain[-1]), textcoords="offset points",
                xytext=(-8, -14), ha="right", fontsize=8, color=theme.text_secondary,
            )
        panel.set_xscale("log", base=2)
        panel.set_yscale("log", base=2)
        panel.set_xticks(ks)
        panel.set_xticklabels([str(k) for k in ks])
        panel.set_title(short(as_text(repo["slug"])))
        strip_spines(panel)
    for extra in flat[len(repos) :]:
        extra.set_visible(False)
    _label_edges(
        list(flat[: len(repos)]), rows, columns,
        "changes dispatched from one base (k)",
        "queued behind the busiest lock", theme,
    )
    figure.suptitle(
        "Node locks queue a fraction of what file locks queue",
        color=theme.text_primary, fontsize=13, fontweight="semibold",
    )
    handles, labels = flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="outside lower center", ncols=3)
    return figure


def figure_footprints(repos: list[dict[str, object]], theme: Theme) -> Figure:
    """How wide one change is, in files and in nodes."""
    figure, panel = plt.subplots(
        figsize=(8.4, 0.62 * len(repos) + 2.4), layout="constrained"
    )
    labels = [short(str(repo["slug"])) for repo in repos]
    height = 0.36
    for index, repo in enumerate(repos):
        changes = as_mapping(as_mapping(repo["profile"]).get("changes"))
        files = as_float(as_mapping(changes.get("files_per_change")).get("p50"))
        nodes = as_float(as_mapping(changes.get("nodes_per_change")).get("p50"))
        panel.barh(
            index + height / 2 + 0.01, files, height=height, color=theme.color(0),
            label="files (median)" if index == 0 else None,
        )
        panel.barh(
            index - height / 2 - 0.01, nodes, height=height, color=theme.color(1),
            label="nodes (median)" if index == 0 else None,
        )
        panel.text(files, index + height / 2 + 0.01, f"  {files:.0f}", va="center",
                   fontsize=9, color=theme.text_secondary)
        panel.text(nodes, index - height / 2 - 0.01, f"  {nodes:.0f}", va="center",
                   fontsize=9, color=theme.text_secondary)
    panel.set_yticks(list(range(len(repos))))
    panel.set_yticklabels(labels)
    panel.set_xlabel("median per merged change")
    panel.set_title("A change touches a handful of files and a few more nodes",
                    color=theme.text_primary)
    panel.grid(axis="y", visible=False)
    panel.legend(loc="lower right")
    strip_spines(panel)
    return figure


FIGURES: tuple[tuple[str, Callable[[list[dict[str, object]], Theme], Figure]], ...] = (
    ("01-collision-vs-k", figure_collision_vs_k),
    ("02-pair-overlap", figure_pair_overlap),
    ("03-node-popularity", figure_node_popularity),
    ("04-hot-node-categories", figure_hot_categories),
    ("05-naive-merge-bias", figure_naive_bias),
    ("06-lock-chain-vs-k", figure_lock_chain),
    ("07-change-footprint", figure_footprints),
)


def render(config: StudyConfig) -> list[Path]:
    """Render every figure in both modes; return the paths written."""
    repos = _load(config)
    config.plots_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, builder in FIGURES:
        for theme in THEMES:
            apply(theme)
            figure = builder(repos, theme)
            suffix = "" if theme.name == "light" else "-dark"
            path = config.plots_root / f"{name}{suffix}.png"
            figure.savefig(path)
            plt.close(figure)
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.plots``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    config = load_config()
    for path in render(config):
        print(f"  wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
