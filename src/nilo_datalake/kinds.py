"""Classify a file when the sender does not declare a kind.

A parent directory named after a capture kind wins over the suffix, so a face
recording stored as ``face/camera.mp4`` is archived as ``face``.
"""

from __future__ import annotations

KNOWN_KINDS = frozenset(
    {"video", "audio", "face", "physiological", "postural", "metadata", "other"}
)

_BY_SUFFIX = {
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".avi": "video",
    ".webm": "video",
    ".wav": "audio",
    ".flac": "audio",
    ".mp3": "audio",
    ".ogg": "audio",
    ".m4a": "audio",
    ".json": "metadata",
    ".ndjson": "metadata",
    ".csv": "physiological",
    ".edf": "physiological",
    ".tsv": "physiological",
}


def infer_kind(logical_path: str) -> str:
    parts = logical_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in KNOWN_KINDS and part != "other":
            return part
    suffix = ""
    if "." in parts[-1]:
        suffix = "." + parts[-1].rsplit(".", 1)[-1].lower()
    return _BY_SUFFIX.get(suffix, "other")


def normalize_kind(value: str | None, logical_path: str) -> str:
    if value and value.strip():
        kind = value.strip().lower()
        if kind not in KNOWN_KINDS:
            return "other"
        return kind
    return infer_kind(logical_path)
