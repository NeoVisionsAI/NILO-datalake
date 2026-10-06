"""Safe directory browsing for storage volume setup in the console."""

from __future__ import annotations

import os
from pathlib import Path

from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError

_ANCHORS = (Path("/media"), Path("/mnt"), Path("/data"), Path("/var"))


def browse_roots(settings: Settings) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for root in _ANCHORS:
        _add_root(root, seen, ordered)
    for volume in settings.storage.volumes:
        if volume.root:
            _add_root(Path(volume.root), seen, ordered)
    return ordered


def _add_root(path: Path, seen: set[str], ordered: list[str]) -> None:
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    if resolved not in seen:
        seen.add(resolved)
        ordered.append(resolved)


def resolve_browse_path(settings: Settings, raw: str) -> Path:
    text = (raw or "").strip() or "/"
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ConfigError("path must be absolute")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ConfigError(f"invalid path: {exc}") from exc
    for root in browse_roots(settings):
        try:
            resolved.relative_to(Path(root).resolve())
            return resolved
        except ValueError:
            continue
    raise ConfigError("path is outside allowed locations (/media, /mnt, /data, volume roots)")


def check_storage_path(settings: Settings, raw: str) -> dict:
    try:
        path = resolve_browse_path(settings, raw)
    except ConfigError as exc:
        return {"ok": False, "exists": False, "is_dir": False, "writable": False, "detail": str(exc)}
    exists = path.exists()
    is_dir = path.is_dir() if exists else False
    writable = os.access(path, os.W_OK) if is_dir else False
    detail = "ready" if exists and is_dir else ("not found" if not exists else "not a directory")
    return {
        "ok": exists and is_dir,
        "path": str(path),
        "exists": exists,
        "is_dir": is_dir,
        "writable": writable,
        "detail": detail,
        "scope": "datalake_container",
        "note": (
            "Checked inside the running datalake container. "
            "/data is usually a Docker volume, not a folder on the NAS host — "
            "ls /data over SSH on the NAS may fail even when this check passes."
        ),
    }


def list_storage_directories(settings: Settings, raw: str) -> dict:
    start = raw.strip() or browse_roots(settings)[0]
    path = resolve_browse_path(settings, start)
    if not path.is_dir():
        raise ConfigError(f"not a directory: {path}")
    directories: list[dict] = []
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        raise ConfigError(f"cannot read directory: {exc}") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                directories.append({"name": child.name, "path": str(child.resolve())})
        except OSError:
            continue
        if len(directories) >= 200:
            break
    parent = None
    if path != path.parent:
        try:
            parent = str(path.parent.resolve())
        except OSError:
            parent = str(path.parent)
    return {"path": str(path), "parent": parent, "directories": directories, "roots": browse_roots(settings)}
