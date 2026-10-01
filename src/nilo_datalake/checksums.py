"""Streaming SHA-256 helpers. File bodies are never loaded as a single bytes object."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` and flush the result to disk."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    with source.open("rb") as src, partial.open("wb") as dst:
        for block in iter(lambda: src.read(CHUNK_BYTES), b""):
            dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(partial, destination)
    _fsync_directory(destination.parent)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
