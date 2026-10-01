"""Move a file into the archive and record it.

The file is hashed before it is published. A second delivery of the same bytes
is a no-op. A second delivery of different bytes for the same logical path is
kept beside the original, with the checksum prefix in the file name.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from nilo_datalake.catalog import Catalog
from nilo_datalake.checksums import copy_file, sha256_file
from nilo_datalake.config import Settings
from nilo_datalake.errors import ChecksumMismatchError
from nilo_datalake.kinds import normalize_kind
from nilo_datalake.layout import destination, revision_path
from nilo_datalake.models import StoredObject, StoreResult, isoformat, utcnow
from nilo_datalake.paths import sanitize_logical_path, sanitize_session_id, sanitize_site_id
from nilo_datalake.volumes import Volume, VolumeManager

log = logging.getLogger(__name__)


class ArchiveWriter:
    def __init__(self, settings: Settings, catalog: Catalog, volumes: VolumeManager):
        self.settings = settings
        self.catalog = catalog
        self.volumes = volumes

    def store_file(
        self,
        source: Path,
        *,
        site_id: str,
        session_id: str,
        logical_path: str,
        kind: str | None,
        content_type: str | None,
        batch_id: str,
        origin: str,
        expected_sha256: str | None = None,
        known_sha256: str | None = None,
        captured_at: datetime | None = None,
        source_locator: str | None = None,
        etag: str | None = None,
        collection: str | None = None,
        volume: Volume | None = None,
        move: bool = False,
    ) -> StoreResult:
        site = sanitize_site_id(site_id)
        session = sanitize_session_id(session_id)
        logical = sanitize_logical_path(logical_path)
        actual = known_sha256.lower() if known_sha256 is not None else sha256_file(source)
        if expected_sha256 is not None and actual != expected_sha256.lower():
            raise ChecksumMismatchError(
                f"checksum mismatch for {logical}: expected {expected_sha256}, got {actual}"
            )
        size = source.stat().st_size
        existing = self.catalog.find_object(site, session, logical, actual)
        if existing is not None and Path(existing["stored_path"]).is_file():
            if move:
                source.unlink(missing_ok=True)
            record = _row_to_stored(existing)
            log.info("duplicate %s session=%s path=%s", site, session or "-", logical)
            return StoreResult(status="duplicate", stored=record)

        when = captured_at or utcnow()
        chosen = volume or self.volumes.pick(size)
        target = destination(
            chosen.root,
            site_id=site,
            session_id=session,
            logical_path=logical,
            when=when,
            collection=collection,
        )
        target = self._avoid_collision(target, actual)
        self._publish(source, target, move=move)
        record = StoredObject(
            site_id=site,
            session_id=session,
            logical_path=logical,
            sha256=actual,
            size_bytes=size,
            kind=normalize_kind(kind, logical),
            content_type=content_type,
            volume_id=chosen.id,
            stored_path=str(target),
            source=origin,
            source_locator=source_locator,
            etag=etag,
            batch_id=batch_id,
            captured_at=isoformat(captured_at) if captured_at else None,
            stored_at=isoformat(utcnow()),
        )
        self.catalog.append_journal(record)
        self.catalog.insert_object(record)
        log.info(
            "stored %s session=%s path=%s bytes=%s volume=%s",
            site,
            session or "-",
            logical,
            size,
            chosen.id,
        )
        return StoreResult(status="stored", stored=record)

    def _avoid_collision(self, target: Path, sha256: str) -> Path:
        if not target.exists():
            return target
        current = sha256_file(target)
        if current == sha256:
            return target
        revised = revision_path(target, sha256)
        if revised.exists() and sha256_file(revised) != sha256:
            raise ChecksumMismatchError(f"revision path already holds different bytes: {revised}")
        return revised

    @staticmethod
    def _publish(source: Path, target: Path, *, move: bool) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and source.resolve() == target.resolve():
            return
        if target.exists() and sha256_file(target) == sha256_file(source):
            if move:
                source.unlink(missing_ok=True)
            return
        same_device = source.stat().st_dev == target.parent.stat().st_dev
        if move and same_device:
            os.replace(source, target)
            _fsync_directory(target.parent)
            return
        copy_file(source, target)
        if move:
            source.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _row_to_stored(row) -> StoredObject:
    return StoredObject(
        site_id=row["site_id"],
        session_id=row["session_id"],
        logical_path=row["logical_path"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        kind=row["kind"],
        content_type=row["content_type"],
        volume_id=row["volume_id"],
        stored_path=row["stored_path"],
        source=row["source"],
        source_locator=row["source_locator"],
        etag=row["etag"],
        batch_id=row["batch_id"],
        captured_at=row["captured_at"],
        stored_at=row["stored_at"],
    )
