"""Persist connected local-model hosts to ``~/.config/mak/local_hosts.json``.

A host named with ``/local url`` (or chosen in the ``/local`` wizard) is
remembered, so the next session starts already connected to it. Only the
endpoint, its kind, and the model list it last reported are stored — never
anything that would need a network call to restore, so startup stays offline.

This is a cache, like ``models.json``, not a config file: a missing or corrupt
file is simply "no saved hosts".
"""
from __future__ import annotations

import json
from pathlib import Path

from cli.core.state import LocalHost
from mak.config import user_config_dir


def hosts_path() -> Path:
    """Return the path of the saved-hosts file."""
    return user_config_dir() / "local_hosts.json"


def load_hosts() -> tuple[str, list[LocalHost]]:
    """Return ``(active_url, hosts)``; empty on a missing or unreadable file."""
    try:
        payload = json.loads(hosts_path().read_text("utf-8"))
    except (OSError, ValueError):
        return "", []
    if not isinstance(payload, dict):
        return "", []
    hosts: list[LocalHost] = []
    for entry in payload.get("hosts", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            continue
        models = entry.get("models", [])
        hosts.append(
            LocalHost(
                url=entry["url"],
                kind=str(entry.get("kind") or "ollama"),
                models=[m for m in models if isinstance(m, str)]
                if isinstance(models, list) else [],
            )
        )
    active = payload.get("active", "")
    if not isinstance(active, str) or active not in {h.url for h in hosts}:
        active = ""
    return active, hosts


def save_hosts(active_url: str, hosts: list[LocalHost]) -> None:
    """Write the saved hosts. Never raises: persistence is a convenience."""
    body = {
        "active": active_url,
        "hosts": [
            {"url": h.url, "kind": h.kind, "models": list(h.models)} for h in hosts
        ],
    }
    path = hosts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
