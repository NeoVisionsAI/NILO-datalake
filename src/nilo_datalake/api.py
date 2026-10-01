"""HTTP API used by the backend or by a MiniPC to push one batch of files.

A batch is opened, each object is uploaded, and then the batch is committed.
Commit re-hashes the staged files and publishes them into the archive. Sending
the same bytes again is reported as a duplicate and does not create a second copy.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request

from nilo_datalake import __version__
from nilo_datalake.config import Settings
from nilo_datalake.errors import (
    BatchStateError,
    ChecksumMismatchError,
    ConfigError,
    InvalidPathError,
    StorageFullError,
)
from nilo_datalake.kinds import normalize_kind
from nilo_datalake.models import isoformat
from nilo_datalake.paths import is_sha256, sanitize_logical_path, sanitize_session_id, sanitize_site_id
from nilo_datalake.service import Context
from nilo_datalake.sync import start_scheduler

_MAX_PATH_HEADER = 512


def create_app(ctx: Context) -> FastAPI:
    if ctx.settings.ingest.enabled:
        ensure_ingest_auth(ctx.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = start_scheduler(ctx)
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="NILO datalake", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    if ctx.settings.ingest.enabled:
        _install_routes(app)
    return app


def ensure_ingest_auth(settings: Settings) -> None:
    if not settings.ingest.api_key and not settings.ingest.allow_insecure_no_auth:
        raise ConfigError(
            "ingest.api_key is empty. Set it, or set ingest.allow_insecure_no_auth "
            "for a private development machine."
        )


def _install_routes(app: FastAPI) -> None:
    @app.post("/v1/batches", dependencies=[Depends(_authorized)])
    def open_batch(body: dict) -> dict:
        ctx: Context = app.state.ctx
        try:
            site_id = sanitize_site_id(str(body.get("site_id") or ctx.settings.site_id))
            source = str(body.get("source") or "push")
            if not source.strip() or len(source) > 64:
                raise InvalidPathError("source name is missing or too long")
        except InvalidPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            volume = ctx.volumes.pick(ctx.settings.storage.min_free_bytes)
        except StorageFullError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        batch_id = uuid4().hex
        staging = volume.staging() / batch_id
        (staging / "payload").mkdir(parents=True)
        ctx.catalog.create_batch(
            batch_id=batch_id,
            site_id=site_id,
            source=source.strip(),
            volume_id=volume.id,
        )
        return {"batch_id": batch_id, "site_id": site_id, "volume_id": volume.id, "status": "open"}

    @app.put("/v1/batches/{batch_id}/objects", dependencies=[Depends(_authorized)])
    async def upload_object(batch_id: str, request: Request, logical_path: str) -> dict:
        ctx: Context = app.state.ctx
        batch = _open_batch(ctx, batch_id)
        try:
            logical = sanitize_logical_path(logical_path)
            session_id = sanitize_session_id(request.headers.get("x-session-id", ""))
        except InvalidPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        expected = request.headers.get("x-checksum-sha256", "").strip().lower()
        if not is_sha256(expected):
            raise HTTPException(status_code=400, detail="X-Checksum-Sha256 must be 64 hex characters")
        captured_at = _captured_at(request.headers.get("x-captured-at"))
        kind = normalize_kind(request.headers.get("x-kind"), logical)
        content_type = request.headers.get("content-type")
        declared = _content_length(request)
        volume = ctx.volumes.get(batch["volume_id"])
        if declared is not None and ctx.volumes.available_bytes(
            next(item for item in ctx.settings.storage.volumes if item.id == volume.id)
        ) < declared:
            raise HTTPException(status_code=507, detail="volume does not have enough free space")

        target = (volume.staging() / batch_id / "payload" / logical).resolve()
        payload_root = (volume.staging() / batch_id / "payload").resolve()
        if payload_root not in target.parents:
            raise HTTPException(status_code=400, detail="logical path escapes the batch")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    digest.update(chunk)
                    handle.write(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            partial.unlink(missing_ok=True)
            if exc.errno == 28:
                raise HTTPException(status_code=507, detail="volume ran out of space") from exc
            raise
        actual = digest.hexdigest()
        if actual != expected or (declared is not None and size != declared):
            partial.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="checksum or size does not match the upload")
        os.replace(partial, target)
        ctx.catalog.add_staging(
            batch_id=batch_id,
            logical_path=logical,
            session_id=session_id,
            sha256=actual,
            size_bytes=size,
            kind=kind,
            content_type=content_type,
            captured_at=isoformat(captured_at) if captured_at else None,
            staging_path=str(target),
        )
        return {"batch_id": batch_id, "logical_path": logical, "sha256": actual, "size_bytes": size}

    @app.post("/v1/batches/{batch_id}/commit", dependencies=[Depends(_authorized)])
    def commit_batch(batch_id: str) -> dict:
        ctx: Context = app.state.ctx
        batch = ctx.catalog.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        if batch["status"] == "committed":
            count, total = ctx.catalog.count_for_batch(batch_id)
            return _batch_payload(batch, object_count=count, total_bytes=total)
        if batch["status"] != "open":
            raise HTTPException(status_code=409, detail=f"batch is {batch['status']}")
        rows = ctx.catalog.staging_objects(batch_id)
        if not rows:
            count, total = ctx.catalog.count_for_batch(batch_id)
            if count == 0:
                raise HTTPException(status_code=400, detail="batch has no objects")
            ctx.catalog.finish_batch(batch_id, status="committed", object_count=count, total_bytes=total)
            refreshed = ctx.catalog.get_batch(batch_id)
            return _batch_payload(refreshed, object_count=count, total_bytes=total)

        volume = ctx.volumes.get(batch["volume_id"])
        stored = 0
        duplicates = 0
        try:
            for row in rows:
                path = Path(row["staging_path"])
                if not path.is_file():
                    already = ctx.catalog.find_object(
                        batch["site_id"], row["session_id"], row["logical_path"], row["sha256"]
                    )
                    if already is None:
                        raise BatchStateError(f"staged file is missing: {row['logical_path']}")
                    ctx.catalog.delete_staging(batch_id, row["logical_path"])
                    duplicates += 1
                    continue
                captured = _captured_at(row["captured_at"])
                result = ctx.writer.store_file(
                    path,
                    site_id=batch["site_id"],
                    session_id=row["session_id"],
                    logical_path=row["logical_path"],
                    kind=row["kind"],
                    content_type=row["content_type"],
                    batch_id=batch_id,
                    origin=batch["source"],
                    expected_sha256=row["sha256"],
                    captured_at=captured,
                    volume=volume,
                    move=True,
                )
                ctx.catalog.delete_staging(batch_id, row["logical_path"])
                if result.status == "stored":
                    stored += 1
                else:
                    duplicates += 1
        except ChecksumMismatchError as exc:
            ctx.catalog.finish_batch(batch_id, status="open", error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StorageFullError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        except BatchStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        count, archived_bytes = ctx.catalog.count_for_batch(batch_id)
        ctx.catalog.finish_batch(
            batch_id,
            status="committed",
            object_count=count,
            total_bytes=archived_bytes,
        )
        shutil.rmtree(volume.staging() / batch_id, ignore_errors=True)
        refreshed = ctx.catalog.get_batch(batch_id)
        return _batch_payload(refreshed, object_count=count, total_bytes=archived_bytes, stored=stored, duplicates=duplicates)

    @app.get("/v1/batches/{batch_id}", dependencies=[Depends(_authorized)])
    def get_batch(batch_id: str) -> dict:
        ctx: Context = app.state.ctx
        batch = ctx.catalog.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        count, total = ctx.catalog.count_for_batch(batch_id)
        return _batch_payload(batch, object_count=count, total_bytes=total)

    @app.get("/v1/status", dependencies=[Depends(_authorized)])
    def status() -> dict:
        return status_payload(app.state.ctx)


def status_payload(ctx: Context) -> dict:
    recent = []
    for row in ctx.catalog.list_batches(limit=10):
        recent.append(
            {
                "batch_id": row["batch_id"],
                "site_id": row["site_id"],
                "source": row["source"],
                "status": row["status"],
                "created_at": row["created_at"],
                "object_count": row["object_count"],
                "total_bytes": row["total_bytes"],
            }
        )
    return {
        "site_id": ctx.settings.site_id,
        "objects": ctx.catalog.object_count(),
        "total_bytes": ctx.catalog.total_bytes(),
        "volumes": ctx.volumes.describe(),
        "recent_batches": recent,
    }


def _authorized(request: Request) -> None:
    settings: Settings = request.app.state.ctx.settings
    if settings.ingest.allow_insecure_no_auth and not settings.ingest.api_key:
        return
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = header[len(prefix) :]
    expected = settings.ingest.api_key
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _open_batch(ctx: Context, batch_id: str):
    batch = ctx.catalog.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch["status"] != "open":
        raise HTTPException(status_code=409, detail=f"batch is {batch['status']}")
    if not batch["volume_id"]:
        raise HTTPException(status_code=409, detail="batch has no volume")
    return batch


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Content-Length is not an integer") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="Content-Length cannot be negative")
    return value


def _captured_at(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="captured_at is not ISO-8601") from exc


def _batch_payload(batch, *, object_count: int, total_bytes: int, stored: int | None = None, duplicates: int | None = None) -> dict:
    payload = {
        "batch_id": batch["batch_id"],
        "site_id": batch["site_id"],
        "source": batch["source"],
        "status": batch["status"],
        "volume_id": batch["volume_id"],
        "created_at": batch["created_at"],
        "committed_at": batch["committed_at"],
        "object_count": object_count,
        "total_bytes": total_bytes,
        "error": batch["error"],
    }
    if stored is not None:
        payload["stored"] = stored
        payload["duplicates"] = duplicates
    return payload


