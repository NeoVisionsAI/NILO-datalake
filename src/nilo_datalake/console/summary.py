"""Dashboard numbers for the settings console."""

from __future__ import annotations

import shutil
from pathlib import Path

from nilo_datalake.service import Context


def build_dashboard(ctx: Context, *, last_sync: dict | None) -> dict:
    settings = ctx.settings
    disks: list[dict] = []
    for volume in settings.storage.volumes:
        if not volume.enabled:
            continue
        root = Path(volume.root)
        entry: dict = {"id": volume.id, "path": str(root)}
        try:
            usage = shutil.disk_usage(root)
            entry.update(
                {
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                }
            )
        except OSError as exc:
            entry["error"] = str(exc)
        disks.append(entry)

    backup_buckets = _backup_bucket_names(settings)
    session_row = ctx.catalog.latest_session_archive()
    backup_row = ctx.catalog.latest_database_backup(backup_buckets)

    return {
        "site_id": settings.site_id,
        "disks": disks,
        "archive_bytes": ctx.catalog.total_bytes(),
        "archive_objects": ctx.catalog.object_count(),
        "last_session_backup": _row_summary(session_row, kind="session"),
        "last_database_backup": _row_summary(backup_row, kind="database"),
        "last_sync": last_sync,
        "sync_running": False,
    }


def _backup_bucket_names(settings) -> list[str]:
    names: list[str] = []
    for bucket in settings.pull.minio.buckets:
        label = bucket.name.lower()
        if "backup" in label:
            names.append(bucket.name)
    if not names:
        for bucket in settings.pull.minio.buckets:
            if bucket.name not in names:
                names.append(bucket.name)
    return names


def _row_summary(row, *, kind: str) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    label = data.get("logical_path") or data.get("session_id") or ""
    if kind == "session" and data.get("session_id"):
        label = str(data["session_id"])
    return {
        "at": data.get("stored_at"),
        "label": label,
        "size_bytes": int(data.get("size_bytes") or 0),
        "path": data.get("logical_path"),
    }
