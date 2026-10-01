"""Records shared by the catalog, the ingest API, and the drop folder."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from nilo_datalake.paths import is_sha256, sanitize_logical_path, sanitize_session_id, sanitize_site_id

SCHEMA_VERSION = 1
BatchStatus = Literal["open", "committed", "failed", "abandoned"]
StoreStatus = Literal["stored", "duplicate"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class StoredObject(BaseModel):
    site_id: str
    session_id: str = ""
    logical_path: str
    sha256: str
    size_bytes: int
    kind: str
    content_type: str | None = None
    volume_id: str
    stored_path: str
    source: str
    source_locator: str | None = None
    etag: str | None = None
    batch_id: str
    captured_at: str | None = None
    stored_at: str


class StoreResult(BaseModel):
    status: StoreStatus
    stored: StoredObject


class ManifestObject(BaseModel):
    logical_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    kind: str = "other"
    session_id: str = ""
    content_type: str | None = None
    captured_at: datetime | None = None
    payload_path: str

    @field_validator("logical_path", "payload_path")
    @classmethod
    def _path(cls, value: str) -> str:
        return sanitize_logical_path(value)

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: str) -> str:
        return sanitize_session_id(value)

    @field_validator("sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        lowered = value.lower()
        if not is_sha256(lowered):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return lowered


class BatchManifest(BaseModel):
    """Description of one rsync/scp drop. Written before the READY marker."""

    schema_version: int = SCHEMA_VERSION
    batch_id: str
    site_id: str
    source: str
    created_at: datetime
    objects: list[ManifestObject]

    @field_validator("site_id")
    @classmethod
    def _site(cls, value: str) -> str:
        return sanitize_site_id(value)

    @field_validator("batch_id")
    @classmethod
    def _batch(cls, value: str) -> str:
        return sanitize_session_id(value)
