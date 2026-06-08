"""Tests for mak.agent_runner.sandbox: docker argv construction + availability."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mak.agent_runner import sandbox as sandbox_mod
from mak.agent_runner.sandbox import SandboxConfig, docker_available


class TestWrap:
    def test_wraps_command_in_docker_run(self, tmp_path: Path) -> None:
        cfg = SandboxConfig()
        argv = cfg.wrap(["claude", "--flag"], str(tmp_path))
        assert argv[0] == "docker"
        assert argv[1] == "run"
        assert "--rm" in argv and "-i" in argv
        # the agent command is appended after the image
        image_idx = argv.index(cfg.image)
        assert argv[image_idx + 1 :] == ["claude", "--flag"]

    def test_mounts_working_dir_and_sets_workdir(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(container_workdir="/work")
        argv = cfg.wrap(["agent"], str(tmp_path))
        host = str(tmp_path.resolve())
        assert "--volume" in argv
        assert f"{host}:/work" in argv
        assert argv[argv.index("--workdir") + 1] == "/work"

    def test_network_restricted_by_default(self, tmp_path: Path) -> None:
        argv = SandboxConfig().wrap(["agent"], str(tmp_path))
        assert argv[argv.index("--network") + 1] == "none"

    def test_custom_image_network_and_extra_args(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(
            image="busybox", network="mak-egress", extra_args=("--memory", "512m")
        )
        argv = cfg.wrap(["agent"], str(tmp_path))
        assert "busybox" in argv
        assert argv[argv.index("--network") + 1] == "mak-egress"
        assert "--memory" in argv and "512m" in argv


class TestDockerAvailable:
    def test_true_when_version_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(args=args, returncode=0)

        monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
        assert docker_available() is True

    def test_false_when_version_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(args=args, returncode=1)

        monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
        assert docker_available() is False

    def test_false_when_docker_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
        assert docker_available() is False
