"""HTTP API used by the backend or by a MiniPC to push one batch of files.

A batch is opened, each object is uploaded, and then the batch is committed.
Commit re-hashes the staged files and publishes them into the archive. Sending
the same bytes again is reported as a duplicate and does not create a second copy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

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
from nilo_datalake.console.routes import install_console
from nilo_datalake.service import Context
from nilo_datalake.sync import start_scheduler
from nilo_datalake.tracing import begin_trace, bound, end_trace

log = logging.getLogger(__name__)

_MAX_PATH_HEADER = 512
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def create_app(ctx: Context, config_path: Path | None = None) -> FastAPI:
    if ctx.settings.ingest.enabled:
        ensure_ingest_auth(ctx.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = start_scheduler(ctx)
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="NILO datalake",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx

    @app.middleware("http")
    async def trace_request(request: Request, call_next):
        if request.url.path in {"/v1/health", "/", "/console"} or request.url.path.startswith("/console/static"):
            return _secure(await call_next(request), request)
        incoming = request.headers.get("x-trace-id", "")
        trace_id, token = begin_trace(incoming if _valid_trace_id(incoming) else None)
        started = time.perf_counter()
        operation = f"{request.method} {request.url.path}"
        try:
            with bound("api", operation):
                log.info("start")
                try:
                    response = await call_next(request)
                except Exception:
                    log.exception("unhandled")
                    response = JSONResponse(
                        status_code=500,
                        content={"detail": "internal error", "trace_id": trace_id},
                    )
                elapsed = int((time.perf_counter() - started) * 1000)
                if response.status_code >= 500:
                    log.error("status=%s duration_ms=%s", response.status_code, elapsed)
                else:
                    log.info("status=%s duration_ms=%s", response.status_code, elapsed)
            response.headers["X-Trace-Id"] = trace_id
            return _secure(response, request)
        finally:
            end_trace(token)

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    if ctx.settings.ingest.enabled:
        _install_routes(app)
    install_console(app, config_path)
    return app


def _secure(response, request: Request):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    if request.url.path.startswith("/console"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _valid_trace_id(value: str) -> bool:
    return 8 <= len(value) <= 64 and value.replace("-", "").isalnum()


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
            if not source.strip() or len(source) > 64 or any(ord(char) < 32 for char in source):
                raise InvalidPathError("source name is missing, too long, or contains control characters")
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
        if declared is None:
            raise HTTPException(status_code=411, detail="Content-Length is required")
        limit = ctx.settings.ingest.max_object_bytes
        if declared > limit:
            raise HTTPException(status_code=413, detail="object exceeds ingest.max_object_bytes")
        volume = ctx.volumes.get(batch["volume_id"])
        if ctx.volumes.available_bytes(
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
                    size += len(chunk)
                    if size > declared:
                        raise HTTPException(status_code=400, detail="upload is larger than Content-Length")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except HTTPException:
            partial.unlink(missing_ok=True)
            raise
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
        _check_batch_id(batch_id)
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
        _check_batch_id(batch_id)
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
    if not header.startswith(prefix) or not _token_matches(header[len(prefix) :], settings.ingest.api_key):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _token_matches(presented: str, expected: str) -> bool:
    left = presented.encode()
    right = expected.encode()
    if not right or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _check_batch_id(batch_id: str) -> None:
    if not _BATCH_ID.fullmatch(batch_id or ""):
        raise HTTPException(status_code=400, detail="invalid batch id")


def _open_batch(ctx: Context, batch_id: str):
    _check_batch_id(batch_id)
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


