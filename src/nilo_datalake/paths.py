"""Path rules for archive keys, site ids, and session ids.

Archive paths are built only from values that pass these checks. Callers cannot
point a logical path at a parent directory.
"""

from __future__ import annotations

import re
from pathlib import Path

from nilo_datalake.errors import InvalidPathError

_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def is_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def sanitize_site_id(value: str) -> str:
    if not _SITE_ID.fullmatch(value or ""):
        raise InvalidPathError(f"invalid site id: {value!r}")
    return value


def sanitize_session_id(value: str) -> str:
    """Validate a session id. An empty string means the object has no session."""

    if value == "":
        return ""
    if not _SESSION_ID.fullmatch(value):
        raise InvalidPathError(f"invalid session id: {value!r}")
    return value


def sanitize_name(value: str, label: str) -> str:
    """Validate a single path segment such as a collection or bucket name."""

    if not _SESSION_ID.fullmatch(value or ""):
        raise InvalidPathError(f"invalid {label}: {value!r}")
    return value


def sanitize_logical_path(value: str) -> str:
    """Return a relative POSIX path with no empty, current, or parent segments."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidPathError("logical path is empty")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise InvalidPathError(f"logical path must be relative: {value!r}")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidPathError(f"logical path escapes its root: {value!r}")
    if any(len(part) > 200 for part in parts) or len(normalized) > 512:
        raise InvalidPathError("logical path is too long")
    return "/".join(parts)


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` and require the result to stay inside ``root``."""

    relative_path = sanitize_logical_path(relative)
    root_resolved = root.resolve()
    candidate = (root_resolved / relative_path).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise InvalidPathError(f"path escapes batch directory: {relative!r}")
    return candidate
