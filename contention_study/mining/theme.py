"""Chart theming: one validated categorical palette, in light and dark.

The eight slots are the reference categorical order, re-stepped per mode, and
were checked with the palette validator before use: every hard gate passes in
both modes on the adjacent pairlist, which is the one that applies to the stacked
bars and multi-line charts here. Light mode raises a contrast warning for three
slots, so every chart in this study direct-labels its series — that is the
relief the warning requires, not an optional nicety.
"""

from __future__ import annotations

from dataclasses import dataclass

import matplotlib as mpl


@dataclass(frozen=True, slots=True)
class Theme:
    """Every colour a figure needs, for one mode."""

    name: str
    surface: str
    text_primary: str
    text_secondary: str
    grid: str
    muted: str
    series: tuple[str, ...]

    def color(self, index: int) -> str:
        """Categorical slot ``index``, assigned in fixed order and never cycled."""
        if index >= len(self.series):
            raise IndexError(
                f"slot {index} exceeds the {len(self.series)}-colour "
                f"categorical order; "
                "fold the tail into 'other' or facet instead of generating a hue"
            )
        return self.series[index]


LIGHT = Theme(
    name="light",
    surface="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    grid="#e3e2de",
    muted="#8f8e88",
    series=(
        "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
        "#e87ba4", "#008300", "#4a3aa7", "#e34948",
    ),
)

DARK = Theme(
    name="dark",
    surface="#1a1a19",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    grid="#3a3a38",
    muted="#7c7b74",
    series=(
        "#3987e5", "#d95926", "#199e70", "#c98500",
        "#d55181", "#008300", "#9085e9", "#e66767",
    ),
)

THEMES = (LIGHT, DARK)


def apply(theme: Theme) -> None:
    """Install a theme as matplotlib's global style for the next figure."""
    mpl.rcParams.update(
        {
            "figure.facecolor": theme.surface,
            "axes.facecolor": theme.surface,
            "savefig.facecolor": theme.surface,
            "axes.edgecolor": theme.grid,
            "axes.labelcolor": theme.text_secondary,
            "axes.titlecolor": theme.text_primary,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": theme.grid,
            "grid.linewidth": 0.8,
            "xtick.color": theme.text_secondary,
            "ytick.color": theme.text_secondary,
            "text.color": theme.text_primary,
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.titleweight": "semibold",
            "legend.frameon": False,
            "legend.labelcolor": theme.text_secondary,
            "lines.linewidth": 2.0,
            "lines.markersize": 5,
            "figure.dpi": 140,
            "savefig.dpi": 140,
            "savefig.bbox": "tight",
        }
    )


def strip_spines(axes: object) -> None:
    """Drop the top and right spines so the marks, not the frame, carry the eye."""
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)  # type: ignore[attr-defined]
