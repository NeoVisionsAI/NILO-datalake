"""Pull completed sessions over SSH with rsync.

The remote directory uses the same layout as the MiniPC spool:

    ready/{session_id}/...
    ready/{session_id}/COMPLETE

A session is ignored until ``COMPLETE`` is present, and that marker is copied
only after the payload. ``delete_after`` removes the remote session once the
archive has accepted every file. Leave it off until the copy has been checked;
the catalog remembers a finished session, so later runs do not download it again.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from nilo_datalake.edge import iter_payload_files
from nilo_datalake.errors import ConfigError
from nilo_datalake.kinds import infer_kind
from nilo_datalake.paths import sanitize_session_id
from nilo_datalake.service import Context

log = logging.getLogger(__name__)


def pull_ssh(ctx: Context) -> dict:
    config = ctx.settings.pull.ssh
    if not config.enabled:
        return {"source": "ssh", "skipped": True}
    if not config.sources:
        return {"source": "ssh", "skipped": False, "error": "no ssh sources configured"}

    stored = 0
    duplicates = 0
    sessions = 0
    errors: list[str] = []
    batch_id = uuid4().hex
    ctx.catalog.create_batch(
        batch_id=batch_id,
        site_id=ctx.settings.site_id,
        source="ssh",
        volume_id=None,
    )
    try:
        for source in config.sources:
            try:
                result = _pull_source(ctx, source, batch_id=batch_id)
            except (ConfigError, OSError, subprocess.CalledProcessError) as exc:
                log.exception("ssh pull failed for %s", source.name)
                errors.append(f"{source.name}: {exc}")
                continue
            stored += result["stored"]
            duplicates += result["duplicates"]
            sessions += result["sessions"]
            errors.extend(result["errors"])
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
        "source": "ssh",
        "skipped": False,
        "batch_id": batch_id,
        "stored": stored,
        "duplicates": duplicates,
        "sessions": sessions,
        "errors": errors,
    }


def _pull_source(ctx: Context, source, *, batch_id: str) -> dict:
    host, remote_root = split_remote(source.remote)
    landing = ctx.volumes.pick_largest()
    scratch = landing.root / "staging"
    probe = scratch / "_ssh_probe"
    if probe.exists():
        shutil.rmtree(probe)
    probe.mkdir(parents=True)
    listing = _rsync(
        ctx,
        f"{_remote_spec(host, remote_root)}/",
        f"{probe}/",
        extra=["--dry-run", "--out-format=%n"],
    )
    shutil.rmtree(probe, ignore_errors=True)
    ready = _completed_sessions(listing)
    stored = 0
    duplicates = 0
    sessions = 0
    errors: list[str] = []
    for session_id in ready:
        locator = f"{source.name}:{session_id}"
        known = ctx.catalog.get_remote("ssh", locator)
        if known is not None and known["stored_path"]:
            if source.delete_after:
                _delete_remote(ctx, host, f"{remote_root.rstrip('/')}/{session_id}")
            continue
        landing = ctx.volumes.pick_largest()
        local = landing.root / "staging" / "_ssh" / source.name / session_id
        if local.exists():
            shutil.rmtree(local)
        local.mkdir(parents=True)
        remote_session = _remote_spec(host, f"{remote_root.rstrip('/')}/{session_id}")
        try:
            _rsync(ctx, f"{remote_session}/", f"{local}/", extra=["--exclude", "COMPLETE"])
            _rsync(ctx, f"{remote_session}/COMPLETE", str(local / "COMPLETE"))
            if not (local / "COMPLETE").is_file():
                raise ConfigError(f"{session_id} has no COMPLETE marker after copy")
            outcome = _archive_session(
                ctx,
                local,
                session_id=session_id,
                batch_id=batch_id,
                origin=source.name,
                volume=landing,
            )
        except (ConfigError, OSError) as exc:
            errors.append(f"{source.name}/{session_id}: {exc}")
            continue
        finally:
            shutil.rmtree(local, ignore_errors=True)
        ctx.catalog.put_remote(
            source_name="ssh",
            locator=locator,
            etag=None,
            size_bytes=outcome["bytes"],
            sha256=None,
            stored_path=outcome["stored_path"],
        )
        stored += outcome["stored"]
        duplicates += outcome["duplicates"]
        sessions += 1
        if source.delete_after:
            _delete_remote(ctx, host, f"{remote_root.rstrip('/')}/{session_id}")
    return {"stored": stored, "duplicates": duplicates, "sessions": sessions, "errors": errors}


def split_remote(spec: str) -> tuple[str | None, str]:
    """Return ``(ssh_host, path)``. ``ssh_host`` is ``None`` for a local directory."""

    if spec.startswith("/"):
        return None, spec
    if ":" not in spec:
        raise ConfigError(f"ssh remote must be a user@host:/absolute/path or a local path: {spec}")
    host, path = spec.split(":", 1)
    if not host or not path.startswith("/"):
        raise ConfigError(f"ssh remote path must be absolute: {spec}")
    return host, path


def _remote_spec(host: str | None, path: str) -> str:
    if host is None:
        return path
    return f"{host}:{path}"


def _completed_sessions(listing: str) -> list[str]:
    found: list[str] = []
    for line in listing.splitlines():
        name = line.strip()
        if not name.endswith("/COMPLETE"):
            continue
        session_id = name[: -len("/COMPLETE")].split("/")[0]
        try:
            sanitize_session_id(session_id)
        except Exception:
            continue
        if session_id not in found:
            found.append(session_id)
    return found


def _archive_session(
    ctx: Context,
    session_dir: Path,
    *,
    session_id: str,
    batch_id: str,
    origin: str,
    volume,
) -> dict:
    files = iter_payload_files(session_dir)
    if not files:
        raise ConfigError(f"session {session_id} has no payload files")
    stored = 0
    duplicates = 0
    total = 0
    last_path = ""
    for path in files:
        logical = path.relative_to(session_dir).as_posix()
        result = ctx.writer.store_file(
            path,
            site_id=ctx.settings.site_id,
            session_id=session_id,
            logical_path=logical,
            kind=infer_kind(logical),
            content_type=None,
            batch_id=batch_id,
            origin=f"ssh:{origin}",
            source_locator=f"ssh:{origin}:{session_id}/{logical}",
            volume=volume,
            move=True,
        )
        total += result.stored.size_bytes
        last_path = result.stored.stored_path
        if result.status == "stored":
            stored += 1
        else:
            duplicates += 1
    return {"stored": stored, "duplicates": duplicates, "bytes": total, "stored_path": last_path}


def _rsync(ctx: Context, source: str, destination: str, *, extra: list[str] | None = None) -> str:
    command = [ctx.settings.pull.ssh.binary, "-a", "--partial"]
    if extra:
        command.extend(extra)
    command.extend(["-e", ctx.settings.pull.ssh.ssh_command, source, destination])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ConfigError(f"rsync failed ({completed.returncode}): {detail}")
    return completed.stdout


def _delete_remote(ctx: Context, host: str | None, path: str) -> None:
    if host is None:
        shutil.rmtree(path, ignore_errors=True)
        return
    command = shlex.split(ctx.settings.pull.ssh.ssh_command) + [host, "rm", "-rf", "--", path]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ConfigError(f"failed to delete {host}:{path}: {detail}")
