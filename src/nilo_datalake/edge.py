"""MiniPC agent. Ships sessions that the capture app has marked complete.

The capture app writes a session directory and creates an empty ``COMPLETE``
file last. This agent never reads a directory that lacks that marker, so a
video that is still being recorded stays on the MiniPC.

HTTP uploads go to the datalake API. The rsync transport writes a drop folder
and copies ``READY`` in a second call, after the payload, so the NAS does not
promote a partial copy.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from nilo_datalake.checksums import sha256_file
from nilo_datalake.client import DatalakeClient
from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError
from nilo_datalake.kinds import infer_kind
from nilo_datalake.models import BatchManifest, ManifestObject, isoformat, utcnow
from nilo_datalake.paths import sanitize_session_id
from nilo_datalake.tracing import begin_trace, end_trace, span

log = logging.getLogger(__name__)


def run_once(settings: Settings, client: DatalakeClient | None = None) -> dict:
    ready_root = settings.edge.spool_dir / "ready"
    sent_root = settings.edge.spool_dir / "sent"
    ready_root.mkdir(parents=True, exist_ok=True)
    sent_root.mkdir(parents=True, exist_ok=True)
    shipped: list[str] = []
    errors: list[dict] = []
    for session_dir in sorted(path for path in ready_root.iterdir() if path.is_dir()):
        if not (session_dir / "COMPLETE").is_file():
            continue
        trace_id, token = begin_trace()
        try:
            with span("edge", "ship", session_id=session_dir.name):
                sanitize_session_id(session_dir.name)
                _ship(settings, session_dir, client)
                destination = sent_root / session_dir.name
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(session_dir), str(destination))
            shipped.append(session_dir.name)
            log.info("shipped session %s trace=%s", session_dir.name, trace_id)
        except Exception as exc:
            errors.append({"session_id": session_dir.name, "error": str(exc), "trace_id": trace_id})
        finally:
            end_trace(token)
    return {"shipped": shipped, "errors": errors}


def run_forever(settings: Settings) -> None:
    interval = settings.edge.interval_seconds
    if interval <= 0:
        raise ConfigError("edge.interval_seconds must be positive")
    log.info("edge agent looping every %s seconds", interval)
    while True:
        try:
            result = run_once(settings)
            if result["errors"]:
                log.error("edge pass finished with %s error(s)", len(result["errors"]))
        except Exception:
            log.exception("edge pass failed")
        time.sleep(interval)


def iter_payload_files(session_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(session_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == "COMPLETE" or path.name.startswith("."):
            continue
        files.append(path)
    return files


def _ship(settings: Settings, session_dir: Path, client: DatalakeClient | None) -> None:
    if settings.edge.transport == "http":
        _ship_http(settings, session_dir, client)
        return
    if settings.edge.transport == "rsync":
        _ship_rsync(settings, session_dir)
        return
    raise ConfigError(f"unknown edge transport: {settings.edge.transport}")


def _ship_http(settings: Settings, session_dir: Path, client: DatalakeClient | None) -> None:
    files = iter_payload_files(session_dir)
    if not files:
        raise ConfigError(f"session {session_dir.name} has no files")
    owns_client = client is None
    if client is None:
        client = DatalakeClient(
            settings.edge.datalake_url,
            settings.edge.api_key,
            timeout=settings.edge.timeout_seconds,
        )
    try:
        batch_id = client.open_batch(settings.site_id, settings.edge.source_name)
        for path in files:
            logical = path.relative_to(session_dir).as_posix()
            client.upload_file(
                batch_id,
                logical,
                path,
                session_id=session_dir.name,
                kind=infer_kind(logical),
            )
        client.commit(batch_id)
    finally:
        if owns_client:
            client.close()


def _ship_rsync(settings: Settings, session_dir: Path) -> None:
    if not settings.edge.rsync_target:
        raise ConfigError("edge.rsync_target is empty")
    files = iter_payload_files(session_dir)
    if not files:
        raise ConfigError(f"session {session_dir.name} has no files")
    batch_id = uuid4().hex
    outgoing = settings.edge.spool_dir / ".outgoing" / batch_id
    payload_root = outgoing / "payload"
    payload_root.mkdir(parents=True)
    objects: list[ManifestObject] = []
    try:
        for path in files:
            logical = path.relative_to(session_dir).as_posix()
            target = payload_root / logical
            target.parent.mkdir(parents=True, exist_ok=True)
            _link_or_copy(path, target)
            objects.append(
                ManifestObject(
                    logical_path=logical,
                    sha256=sha256_file(path),
                    size_bytes=path.stat().st_size,
                    kind=infer_kind(logical),
                    session_id=session_dir.name,
                    payload_path=f"payload/{logical}",
                )
            )
        manifest = BatchManifest(
            batch_id=batch_id,
            site_id=settings.site_id,
            source=settings.edge.source_name,
            created_at=utcnow(),
            objects=objects,
        )
        (outgoing / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        remote = settings.edge.rsync_target.rstrip("/") + f"/{batch_id}/"
        _rsync(settings, f"{outgoing}/", remote)
        ready = outgoing / "READY"
        ready.write_text(isoformat(utcnow()) + "\n", encoding="utf-8")
        _rsync(settings, str(ready), remote + "READY")
    finally:
        shutil.rmtree(outgoing, ignore_errors=True)


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _rsync(settings: Settings, source: str, destination: str) -> None:
    completed = subprocess.run(
        [settings.edge.rsync_binary, "-a", "--partial", source, destination],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ConfigError(f"rsync failed ({completed.returncode}): {detail}")
