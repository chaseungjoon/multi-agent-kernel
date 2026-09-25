"""Entry point: ``python -m cli`` or the ``mak`` console script.

``mak`` with no arguments opens the interactive TUI. ``mak run --task "..."``
forwards to the one-shot kernel CLI (``python -m mak``). ``mak gc`` prunes this
project's node store. ``mak update`` re-runs the uv install command so the tool
moves to the newest **release** — updating is always explicit; launching mak
never touches the network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

_REPO_URL = "https://github.com/chaseungjoon/multi-agent-kernel"
_REPO_SPEC = f"git+{_REPO_URL}"


def _is_uv_tool_install() -> bool:
    """Whether this process runs from a ``uv tool`` environment.

    uv tool venvs live under ``.../uv/tools/<name>/``; a source checkout or a
    plain pip/venv install does not, and must never be "updated" by installing
    a second copy from GitHub over it.
    """
    return "/uv/tools/" in sys.executable.replace("\\", "/")


def _installed_commit() -> str | None:
    """Return the git commit this install was built from, or None if unknown.

    uv records the resolved revision in the package's PEP 610
    ``direct_url.json`` (``vcs_info.commit_id``) inside the tool environment's
    site-packages; the tool root is two levels above the interpreter.
    """
    # No resolve(): the venv's bin/python is a symlink to the base interpreter,
    # and resolving it would escape the tool directory entirely.
    root = Path(sys.executable).parent.parent
    try:
        path = next(root.rglob("multi_agent_kernel-*.dist-info/direct_url.json"))
        commit = json.loads(path.read_text(encoding="utf-8"))["vcs_info"]["commit_id"]
    except (StopIteration, OSError, ValueError, KeyError, TypeError):
        return None
    return commit if isinstance(commit, str) and commit else None


def _ls_remote(*args: str) -> str | None:
    """Run ``git ls-remote`` against the repo; return stdout, or None on failure."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "ls-remote", *args, _REPO_URL],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _remote_commit() -> str | None:
    """Return the repo's current HEAD commit, or None if it can't be fetched."""
    out = _ls_remote("HEAD")
    if out is None:
        return None
    head = out.split()
    return head[0] if head and re.fullmatch(r"[0-9a-f]{40}", head[0]) else None


_TAG_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")


def _version_key(tag: str) -> tuple[tuple[int, ...], int, str] | None:
    """Sort key for a release tag, or None if it does not look like a version.

    Deliberately tolerant rather than a full PEP 440 parser: ``packaging`` is not
    a declared dependency, and the only ordering this has to get right is between
    this project's own tags. A plain release sorts above any pre-release of the
    same number (``0.5.10`` > ``0.5.10b0``), and pre-release suffixes compare
    lexically, which puts ``a`` before ``b`` before ``rc``.
    """
    match = _TAG_VERSION.match(tag)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2)
    return (numbers, 0 if suffix else 1, suffix)


def _latest_release_tag() -> tuple[str, str] | None:
    """Return the newest ``(tag, commit)`` the remote publishes, or None.

    Annotated tags appear twice in ``ls-remote`` output: ``refs/tags/x`` (the tag
    object) and ``refs/tags/x^{}`` (the commit it points at). The peeled entry is
    the one to keep — it is what an install of that tag actually builds from, so
    it is what the PEP 610 commit comparison can be checked against.
    """
    out = _ls_remote("--tags")
    if not out:
        return None
    commits: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        commit, ref = parts
        tag = ref[len("refs/tags/") :]
        peeled = tag.endswith("^{}")
        tag = tag[:-3] if peeled else tag
        if _version_key(tag) is None:
            continue
        if peeled or tag not in commits:
            commits[tag] = commit
    if not commits:
        return None
    newest = max(commits, key=lambda t: _version_key(t) or ((), 0, ""))
    return newest, commits[newest]


@dataclass(frozen=True, slots=True)
class _UpdateTarget:
    """What ``mak update`` would move to: an install spec and its label/commit."""

    spec: str
    label: str
    commit: str | None


def _resolve_update_target() -> _UpdateTarget:
    """Resolve the revision ``mak update`` installs.

    A release tag, when the remote publishes one. Installing an unpinned
    ``git+<url>`` meant every user who ran ``update`` adopted whatever had last
    been pushed to ``main`` — including a half-finished branch merge — with no
    tag, no pin, and nothing naming what they were moving to. Falls back to
    ``HEAD`` only when the repo has no version tags at all, which is the honest
    answer for a project that has not cut a release yet.
    """
    tag = _latest_release_tag()
    if tag is not None:
        name, commit = tag
        return _UpdateTarget(f"{_REPO_SPEC}@{name}", name, commit)
    return _UpdateTarget(
        _REPO_SPEC,
        "the latest commit on main (no release tag is published)",
        _remote_commit(),
    )


def _update() -> int:
    """Update mak to the newest published release (``mak update``).

    ``uv tool install`` with a git spec reinstalls even when the resolved
    commit is unchanged, so uv's own "Installed …" output does not mean an
    update happened. The real signal is the installed commit (PEP 610
    ``direct_url.json``): a fast ``git ls-remote`` pre-check skips the
    reinstall entirely when already current, and otherwise the installed
    commit is compared before/after to report honestly.
    """
    if not _is_uv_tool_install():
        print(
            "mak: this copy is not a `uv tool` install — update your source "
            "checkout with `git pull` (then `pip install -e .`) instead.",
            file=sys.stderr,
        )
        return 1
    uv = shutil.which("uv")
    if uv is None:
        print("mak: `uv` was not found on PATH; cannot update.", file=sys.stderr)
        return 1

    target = _resolve_update_target()
    installed = _installed_commit()
    if installed is not None and installed == target.commit:
        print(f"mak: already up to date ({target.label}).")
        return 0

    print(f"mak: updating to {target.label}…")
    try:
        result = subprocess.run(
            [uv, "tool", "install", target.spec], capture_output=True, text=True
        )
    except OSError as exc:
        print(f"mak: update failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        print("mak: update failed.", file=sys.stderr)
        return result.returncode

    after = _installed_commit()
    if installed is not None and after == installed:
        print(f"mak: already up to date ({target.label}).")
    elif after is not None:
        print(
            f"mak: updated to {target.label} ({after[:8]}) — restart mak to use it."
        )
    else:
        print(f"mak: updated to {target.label} — restart mak to use it.")
    return 0


def _gc(argv: list[str]) -> int:
    """Prune this project's node store (``mak gc``).

    Every commit writes a new ``v{n}.py`` and, before Wave 18, nothing ever
    removed one — so a store written by an older MAK carries versions and
    superseded fragment directories that no run will ever read again. A fresh
    store stays bounded on its own; this is the one-time sweep for the rest.
    """
    from mak.config import anchor_mak_dir, discover_config_path, load_config
    from mak.core.exceptions import MakError
    from mak.lock_manager.project_lease import ProjectLease
    from mak.node_store.store import NodeStore

    work_dir = argv[0] if argv and not argv[0].startswith("-") else None
    try:
        config = load_config(discover_config_path(work_dir))
        if work_dir is not None:
            config = replace(
                config, session=replace(config.session, work_dir=work_dir)
            )
        config = anchor_mak_dir(config)
        mak_dir = Path(config.session.mak_dir)
        store_root = mak_dir / "node_store"
        if not store_root.is_dir():
            print(f"mak: no node store at {store_root} — nothing to collect.")
            return 0
        # gc *mutates* the store — it deletes version files and whole fragment
        # directories. Doing that underneath a running session would remove the
        # versions that session's open transaction may still need to roll back
        # to, so maintenance takes the same single-owner lease a run does.
        with ProjectLease(mak_dir, "mak-gc"):
            store = NodeStore(
                store_root, version_retention=config.node_store.version_retention
            )
            removed = store.gc()
    except MakError as exc:
        print(f"mak: gc failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"mak: pruned {removed['versions']} stale version file(s) and "
        f"{removed['directories']} orphaned fragment director"
        f"{'y' if removed['directories'] == 1 else 'ies'} from {store_root}."
    )
    return 0


def _examples(argv: list[str]) -> int:
    """List the packaged example configs, or print one to stdout.

    ``mak examples local-ollama > .mak/config.yaml`` is the whole non-interactive
    quickstart for a local run, which is why this prints the file rather than
    writing it: redirecting is the user's decision, and MAK does not create a
    config file on its own.
    """
    from mak.config import example_path, list_examples
    from mak.core.exceptions import MakError

    if not argv:
        print("Packaged example configs — mak examples <name> prints one:\n")
        for name in list_examples():
            print(f"  {name}")
        print("\ne.g.  mak examples local-ollama > .mak/config.yaml")
        return 0
    try:
        path = example_path(argv[0])
    except MakError as exc:
        print(f"mak: {exc}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def main() -> int:
    """Dispatch ``mak``: TUI, ``run``, ``gc``, ``update``, or ``--version``."""
    argv = sys.argv[1:]

    if argv and argv[0] in ("--version", "-V"):
        from mak._version import __version__
        print(f"mak {__version__}")
        return 0

    if argv and argv[0] in ("--help", "-h"):
        print(
            "usage: mak                 launch the interactive TUI\n"
            "       mak run --task ...  run one task non-interactively "
            "(see: mak run --help)\n"
            "       mak gc [work_dir]   prune this project's node store\n"
            "       mak examples [name] list packaged example configs, or "
            "print one\n"
            "       mak update          update mak to the newest release\n"
            "       mak --version       print the version"
        )
        return 0

    if argv and argv[0] == "update":
        return _update()

    if argv and argv[0] == "gc":
        return _gc(argv[1:])

    if argv and argv[0] == "examples":
        return _examples(argv[1:])

    if argv and argv[0] == "run":
        from mak.__main__ import main as run_main
        return run_main(argv[1:])

    from cli.app import MakCli
    MakCli().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
