"""Canonical paths inside a volume.

Sessions stay in one directory so a recording can be browsed as a unit.
Metadata exports and objects without a session are partitioned by day.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nilo_datalake.models import isoformat
from nilo_datalake.paths import sanitize_logical_path, sanitize_name, sanitize_session_id, sanitize_site_id


def destination(
    volume_root: Path,
    *,
    site_id: str,
    session_id: str,
    logical_path: str,
    when: datetime,
    collection: str | None = None,
) -> Path:
    site = sanitize_site_id(site_id)
    session = sanitize_session_id(session_id)
    relative = sanitize_logical_path(logical_path)
    day = isoformat(when)[:10].split("-")
    if session:
        return volume_root / "archive" / site / "sessions" / session / relative
    if collection:
        name = sanitize_name(collection, "collection")
        return (
            volume_root
            / "archive"
            / site
            / "metadata"
            / name
            / day[0]
            / day[1]
            / day[2]
            / relative
        )
    return volume_root / "archive" / site / "blobs" / day[0] / day[1] / day[2] / relative


def revision_path(path: Path, sha256: str) -> Path:
    return path.with_name(f"{path.stem}__{sha256[:12]}{path.suffix}")
