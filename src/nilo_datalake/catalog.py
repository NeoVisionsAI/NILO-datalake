"""SQLite index of archived objects, plus an append-only JSONL journal.

The journal is written before the SQLite row. ``rebuild`` replays the journal,
so the index can be reconstructed if the database file is lost.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from nilo_datalake.models import StoredObject, isoformat, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    volume_id TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    object_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    logical_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_type TEXT,
    volume_id TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    source TEXT NOT NULL,
    source_locator TEXT,
    etag TEXT,
    batch_id TEXT NOT NULL,
    captured_at TEXT,
    stored_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_identity
    ON objects(site_id, session_id, logical_path, sha256);

CREATE TABLE IF NOT EXISTS staging_objects (
    batch_id TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_type TEXT,
    captured_at TEXT,
    staging_path TEXT NOT NULL,
    PRIMARY KEY (batch_id, logical_path)
);

CREATE TABLE IF NOT EXISTS watermarks (
    source_name TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_objects (
    source_name TEXT NOT NULL,
    locator TEXT NOT NULL,
    etag TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    stored_path TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_name, locator)
);
"""


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.journal_path = path.with_suffix(".jsonl")

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    def create_batch(
        self,
        *,
        batch_id: str,
        site_id: str,
        source: str,
        volume_id: str | None,
        status: str = "open",
    ) -> None:
        now = isoformat(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, site_id, source, status, volume_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, site_id, source, status, volume_id, now),
            )

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()

    def list_batches(self, *, status: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM batches WHERE status = ? ORDER BY created_at ASC",
                    (status,),
                ).fetchall()
            return list(rows)

    def finish_batch(
        self,
        batch_id: str,
        *,
        status: str,
        object_count: int | None = None,
        total_bytes: int | None = None,
        error: str | None = None,
    ) -> None:
        finished_at = None if status == "open" else isoformat(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE batches
                SET status = ?,
                    committed_at = COALESCE(?, committed_at),
                    object_count = COALESCE(?, object_count),
                    total_bytes = COALESCE(?, total_bytes),
                    error = ?
                WHERE batch_id = ?
                """,
                (status, finished_at, object_count, total_bytes, error, batch_id),
            )

    def add_staging(self, **row: str | int | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO staging_objects (
                    batch_id, logical_path, session_id, sha256, size_bytes, kind,
                    content_type, captured_at, staging_path
                ) VALUES (
                    :batch_id, :logical_path, :session_id, :sha256, :size_bytes, :kind,
                    :content_type, :captured_at, :staging_path
                )
                ON CONFLICT(batch_id, logical_path) DO UPDATE SET
                    session_id = excluded.session_id,
                    sha256 = excluded.sha256,
                    size_bytes = excluded.size_bytes,
                    kind = excluded.kind,
                    content_type = excluded.content_type,
                    captured_at = excluded.captured_at,
                    staging_path = excluded.staging_path
                """,
                row,
            )

    def staging_objects(self, batch_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM staging_objects WHERE batch_id = ? ORDER BY logical_path",
                    (batch_id,),
                )
            )

    def delete_staging(self, batch_id: str, logical_path: str | None = None) -> None:
        with self.connect() as conn:
            if logical_path is None:
                conn.execute("DELETE FROM staging_objects WHERE batch_id = ?", (batch_id,))
            else:
                conn.execute(
                    "DELETE FROM staging_objects WHERE batch_id = ? AND logical_path = ?",
                    (batch_id, logical_path),
                )

    def find_object(
        self, site_id: str, session_id: str, logical_path: str, sha256: str
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM objects
                WHERE site_id = ? AND session_id = ? AND logical_path = ? AND sha256 = ?
                """,
                (site_id, session_id, logical_path, sha256),
            ).fetchone()

    def find_logical(self, site_id: str, session_id: str, logical_path: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM objects
                    WHERE site_id = ? AND session_id = ? AND logical_path = ?
                    ORDER BY stored_at
                    """,
                    (site_id, session_id, logical_path),
                )
            )

    def append_journal(self, record: StoredObject) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.model_dump(), separators=(",", ":"))
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def insert_object(self, record: StoredObject) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO objects (
                    site_id, session_id, logical_path, sha256, size_bytes, kind,
                    content_type, volume_id, stored_path, source, source_locator,
                    etag, batch_id, captured_at, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.site_id,
                    record.session_id,
                    record.logical_path,
                    record.sha256,
                    record.size_bytes,
                    record.kind,
                    record.content_type,
                    record.volume_id,
                    record.stored_path,
                    record.source,
                    record.source_locator,
                    record.etag,
                    record.batch_id,
                    record.captured_at,
                    record.stored_at,
                ),
            )

    def count_for_batch(self, batch_id: str) -> tuple[int, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total
                FROM objects WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            return int(row["n"]), int(row["total"])

    def object_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM objects").fetchone()
            return int(row["n"])

    def total_bytes(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM objects").fetchone()
            return int(row["n"])

    def get_watermark(self, source_name: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM watermarks WHERE source_name = ?",
                (source_name,),
            ).fetchone()
            return None if row is None else str(row["cursor"])

    def set_watermark(self, source_name: str, cursor: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO watermarks (source_name, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_name) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (source_name, cursor, isoformat(utcnow())),
            )

    def get_remote(self, source_name: str, locator: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM remote_objects WHERE source_name = ? AND locator = ?",
                (source_name, locator),
            ).fetchone()

    def put_remote(
        self,
        *,
        source_name: str,
        locator: str,
        etag: str | None,
        size_bytes: int | None,
        sha256: str | None,
        stored_path: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_objects (
                    source_name, locator, etag, size_bytes, sha256, stored_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_name, locator) DO UPDATE SET
                    etag = excluded.etag,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    stored_path = excluded.stored_path,
                    updated_at = excluded.updated_at
                """,
                (source_name, locator, etag, size_bytes, sha256, stored_path, isoformat(utcnow())),
            )

    def rebuild_from_journal(self) -> int:
        """Replace the object index with the contents of the JSONL journal."""

        if not self.journal_path.is_file():
            with self.connect() as conn:
                conn.execute("DELETE FROM objects")
            return 0
        records: list[StoredObject] = []
        with self.journal_path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                records.append(StoredObject.model_validate_json(text))
        with self.connect() as conn:
            conn.execute("DELETE FROM objects")
            for record in records:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO objects (
                        site_id, session_id, logical_path, sha256, size_bytes, kind,
                        content_type, volume_id, stored_path, source, source_locator,
                        etag, batch_id, captured_at, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.site_id,
                        record.session_id,
                        record.logical_path,
                        record.sha256,
                        record.size_bytes,
                        record.kind,
                        record.content_type,
                        record.volume_id,
                        record.stored_path,
                        record.source,
                        record.source_locator,
                        record.etag,
                        record.batch_id,
                        record.captured_at,
                        record.stored_at,
                    ),
                )
        return len(records)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
