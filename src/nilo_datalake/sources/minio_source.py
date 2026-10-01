"""Incremental MinIO copy.

An object is skipped when its ETag is already in the catalog. Keys under
``{session_prefix}{session_id}/`` are stored inside that session. Every other
key is stored as a blob and keeps the bucket name in its logical path.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from nilo_datalake.errors import InvalidPathError
from nilo_datalake.paths import sanitize_logical_path, sanitize_session_id
from nilo_datalake.service import Context

log = logging.getLogger(__name__)


def pull_minio(ctx: Context, client=None) -> dict:
    config = ctx.settings.pull.minio
    if not config.enabled:
        return {"source": "minio", "skipped": True}
    if not config.buckets:
        return {"source": "minio", "skipped": False, "error": "no buckets configured"}

    owns_client = client is None
    if owns_client:
        from minio import Minio

        client = Minio(
            config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            region=config.region or None,
        )
    batch_id = uuid4().hex
    ctx.catalog.create_batch(
        batch_id=batch_id,
        site_id=ctx.settings.site_id,
        source="minio",
        volume_id=None,
    )
    stored = 0
    duplicates = 0
    unchanged = 0
    deferred = 0
    errors: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ctx.settings.pull.safety_delay_seconds)
    try:
        for bucket in config.buckets:
            for obj in client.list_objects(bucket.name, prefix=bucket.prefix, recursive=True):
                if getattr(obj, "is_dir", False):
                    continue
                key = obj.object_name
                locator = f"{bucket.name}/{key}"
                try:
                    outcome = _copy_object(ctx, client, bucket.name, obj, batch_id=batch_id, cutoff=cutoff)
                except Exception as exc:
                    log.exception("minio copy failed for %s", locator)
                    errors.append(f"{locator}: {exc}")
                    continue
                if outcome == "deferred":
                    deferred += 1
                elif outcome == "skipped":
                    unchanged += 1
                elif outcome == "stored":
                    stored += 1
                else:
                    duplicates += 1
        ctx.catalog.finish_batch(
            batch_id,
            status="failed" if errors else "committed",
            object_count=stored + duplicates,
            error="; ".join(errors) or None,
        )
    except Exception as exc:
        ctx.catalog.finish_batch(batch_id, status="failed", error=str(exc))
        raise
    return {
        "source": "minio",
        "skipped": False,
        "batch_id": batch_id,
        "stored": stored,
        "duplicates": duplicates,
        "unchanged": unchanged,
        "deferred": deferred,
        "errors": errors,
    }


def split_object_key(key: str, session_prefix: str) -> tuple[str, str]:
    """Return ``(session_id, logical_path)``. An empty session id means a loose blob."""

    if session_prefix and key.startswith(session_prefix):
        rest = key[len(session_prefix) :]
        if "/" in rest:
            session_id, logical = rest.split("/", 1)
            try:
                return sanitize_session_id(session_id), sanitize_logical_path(logical)
            except InvalidPathError:
                pass
    return "", sanitize_logical_path(key)


def _copy_object(ctx: Context, client, bucket: str, obj, *, batch_id: str, cutoff: datetime) -> str:
    key = obj.object_name
    modified = _as_utc(getattr(obj, "last_modified", None))
    if modified is not None and modified > cutoff:
        return "deferred"
    locator = f"{bucket}/{key}"
    remote = ctx.catalog.get_remote("minio", locator)
    etag = getattr(obj, "etag", None)
    if remote is not None and etag and remote["etag"] == etag and remote["stored_path"]:
        if Path(remote["stored_path"]).is_file():
            return "skipped"
    session_id, logical = split_object_key(key, ctx.settings.pull.minio.session_prefix)
    if not session_id:
        logical = sanitize_logical_path(f"{bucket}/{key}")
    size = int(getattr(obj, "size", None) or 1)
    volume = ctx.volumes.pick(size)
    directory = volume.staging() / "_pull"
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="minio-", dir=directory, delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        client.fget_object(bucket, key, str(path))
        result = ctx.writer.store_file(
            path,
            site_id=ctx.settings.site_id,
            session_id=session_id,
            logical_path=logical,
            kind=None,
            content_type=getattr(obj, "content_type", None),
            batch_id=batch_id,
            origin="minio",
            captured_at=modified,
            source_locator=f"minio://{locator}",
            etag=etag,
            volume=volume,
            move=True,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    ctx.catalog.put_remote(
        source_name="minio",
        locator=locator,
        etag=etag,
        size_bytes=result.stored.size_bytes,
        sha256=result.stored.sha256,
        stored_path=result.stored.stored_path,
    )
    return result.status


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
