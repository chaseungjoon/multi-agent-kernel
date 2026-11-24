"""Wave 28.9: importers in one shared file run side by side and lose nothing."""

from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

from growth_bench import run_arm  # noqa: E402


def test_the_import_channel_lifts_the_header_bottleneck(tmp_path: Path) -> None:
    n = 6
    header = run_arm("header", n, 0.15, tmp_path / "header")
    channel = run_arm("channel", n, 0.15, tmp_path / "channel")

    assert header.ok and channel.ok
    # Granting the header serializes every importer on one WRITE lock.
    assert header.max_concurrency == 1
    # The channel lets them all run at once, with no extra agent calls.
    assert channel.max_concurrency >= n - 1
    assert channel.agent_calls == header.agent_calls == n
    assert channel.wall_s < header.wall_s
    for arm in (header, channel):
        assert (arm.imports_once, arm.imports_lost) == (n, 0), arm
