"""Promote rsync/scp drops and garbage-collect unfinished uploads.

A drop is a directory under ``{volume}/inbox/{batch_id}/``:

    manifest.json
    payload/...
    READY

``READY`` is created last, after every payload file is in place. Until that
marker exists the directory is ignored, so a copy still in progress is never
archived.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from nilo_datalake.catalog import parse_time
from nilo_datalake.checksums import sha256_file
from nilo_datalake.errors import ChecksumMismatchError, ManifestError
from nilo_datalake.models import BatchManifest, isoformat, utcnow
from nilo_datalake.paths import resolve_inside
from nilo_datalake.service import Context

log = logging.getLogger(__name__)


def promote_inbox(ctx: Context) -> dict:
    stored = 0
    duplicates = 0
    failed = 0
    promoted = 0
    errors: list[str] = []
    for volume in ctx.volumes.enabled():
        inbox = volume.inbox()
        if not inbox.is_dir():
            continue
        for batch_dir in sorted(path for path in inbox.iterdir() if path.is_dir()):
            if not (batch_dir / "READY").is_file():
                continue
            try:
                result = _promote_one(ctx, volume.id, batch_dir)
            except (ManifestError, ChecksumMismatchError, ValidationError, OSError) as exc:
                failed += 1
                message = f"{batch_dir.name}: {exc}"
                errors.append(message)
                log.exception("inbox batch failed: %s", batch_dir.name)
                _quarantine(volume.quarantine(), batch_dir, str(exc))
                continue
            stored += result["stored"]
            duplicates += result["duplicates"]
            promoted += 1
    return {
        "source": "inbox",
        "promoted": promoted,
        "stored": stored,
        "duplicates": duplicates,
        "failed": failed,
        "errors": errors,
    }


def _promote_one(ctx: Context, volume_id: str, batch_dir: Path) -> dict:
    manifest_path = batch_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ManifestError("manifest.json is missing")
    manifest = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if manifest.batch_id != batch_dir.name:
        raise ManifestError("manifest batch_id does not match the directory name")
    if not manifest.objects:
        raise ManifestError("manifest has no objects")

    staged: list[tuple] = []
    for item in manifest.objects:
        payload = resolve_inside(batch_dir, item.payload_path)
        if not payload.is_file():
            raise ManifestError(f"payload missing: {item.payload_path}")
        size = payload.stat().st_size
        if size != item.size_bytes:
            raise ManifestError(
                f"size mismatch for {item.logical_path}: manifest {item.size_bytes}, file {size}"
            )
        actual = sha256_file(payload)
        if actual != item.sha256:
            raise ChecksumMismatchError(
                f"checksum mismatch for {item.logical_path}: expected {item.sha256}, got {actual}"
            )
        staged.append((item, payload, actual))

    volume = ctx.volumes.get(volume_id)
    existing = ctx.catalog.get_batch(manifest.batch_id)
    if existing is None:
        ctx.catalog.create_batch(
            batch_id=manifest.batch_id,
            site_id=manifest.site_id,
            source=manifest.source,
            volume_id=volume.id,
        )
    elif existing["status"] == "committed":
        _discard_drop(batch_dir)
        return {"stored": 0, "duplicates": len(staged)}

    stored = 0
    duplicates = 0
    total = 0
    for item, payload, actual in staged:
        result = ctx.writer.store_file(
            payload,
            site_id=manifest.site_id,
            session_id=item.session_id,
            logical_path=item.logical_path,
            kind=item.kind,
            content_type=item.content_type,
            batch_id=manifest.batch_id,
            origin=manifest.source,
            expected_sha256=item.sha256,
            known_sha256=actual,
            captured_at=item.captured_at,
            volume=volume,
            move=True,
        )
        total += item.size_bytes
        if result.status == "stored":
            stored += 1
        else:
            duplicates += 1
    ctx.catalog.finish_batch(
        manifest.batch_id,
        status="committed",
        object_count=stored + duplicates,
        total_bytes=total,
    )
    _write_receipt(volume.root, manifest.batch_id, stored, duplicates)
    _discard_drop(batch_dir)
    log.info("promoted inbox batch %s stored=%s duplicates=%s", manifest.batch_id, stored, duplicates)
    return {"stored": stored, "duplicates": duplicates}


def gc_incomplete(ctx: Context, *, older_than_hours: int) -> dict:
    """Remove abandoned API staging dirs and inbox drops that never became READY."""

    cutoff = utcnow() - timedelta(hours=older_than_hours)
    abandoned = 0
    for batch in ctx.catalog.list_batches(status="open"):
        created = parse_time(batch["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=utcnow().tzinfo)
        if created >= cutoff:
            continue
        volume_id = batch["volume_id"]
        if volume_id:
            staging = ctx.volumes.get(volume_id).staging() / batch["batch_id"]
            shutil.rmtree(staging, ignore_errors=True)
        ctx.catalog.delete_staging(batch["batch_id"])
        ctx.catalog.finish_batch(batch["batch_id"], status="abandoned", error="abandoned by gc")
        abandoned += 1

    removed_drops = 0
    removed_partials = 0
    for volume in ctx.volumes.enabled():
        inbox = volume.inbox()
        if inbox.is_dir():
            for batch_dir in list(inbox.iterdir()):
                if not batch_dir.is_dir() or (batch_dir / "READY").exists():
                    continue
                modified = datetime_from_mtime(batch_dir)
                if modified < cutoff:
                    shutil.rmtree(batch_dir, ignore_errors=True)
                    removed_drops += 1
        for partial in volume.root.rglob("*.partial"):
            if datetime_from_mtime(partial) < cutoff:
                partial.unlink(missing_ok=True)
                removed_partials += 1
    return {
        "abandoned_batches": abandoned,
        "removed_incomplete_drops": removed_drops,
        "removed_partials": removed_partials,
    }


def quarantine_tree(quarantine_root: Path, batch_dir: Path, reason: str) -> None:
    _quarantine(quarantine_root, batch_dir, reason)


def _quarantine(quarantine_root: Path, batch_dir: Path, reason: str) -> None:
    quarantine_root.mkdir(parents=True, exist_ok=True)
    target = quarantine_root / batch_dir.name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(batch_dir), str(target))
    (target / "ERROR.txt").write_text(reason + "\n", encoding="utf-8")


def _discard_drop(batch_dir: Path) -> None:
    shutil.rmtree(batch_dir, ignore_errors=True)


def _write_receipt(volume_root: Path, batch_id: str, stored: int, duplicates: int) -> None:
    receipt_dir = volume_root / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": batch_id,
        "stored": stored,
        "duplicates": duplicates,
        "finished_at": isoformat(utcnow()),
    }
    (receipt_dir / f"{batch_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def datetime_from_mtime(path: Path):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
