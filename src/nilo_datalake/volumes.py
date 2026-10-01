"""Choose which mounted disk receives the next write.

Disks are filled in configuration order. A second 8 TB disk is another volume:
new objects land on it once the first disk cannot fit them, and existing files
stay where they were written.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from nilo_datalake.config import StorageConfig, VolumeConfig
from nilo_datalake.errors import StorageFullError


@dataclass(frozen=True)
class Volume:
    id: str
    root: Path

    def inbox(self) -> Path:
        return self.root / "inbox"

    def staging(self) -> Path:
        return self.root / "staging"

    def quarantine(self) -> Path:
        return self.root / "quarantine"

    def archive(self) -> Path:
        return self.root / "archive"


class VolumeManager:
    def __init__(self, storage: StorageConfig, disk_usage=shutil.disk_usage):
        self.storage = storage
        self._disk_usage = disk_usage
        self._by_id = {item.id: item for item in storage.volumes}

    def enabled(self) -> list[Volume]:
        return [self._wrap(item) for item in self.storage.volumes if item.enabled]

    def get(self, volume_id: str) -> Volume:
        try:
            item = self._by_id[volume_id]
        except KeyError as exc:
            raise StorageFullError(f"unknown volume: {volume_id}") from exc
        return self._wrap(item)

    def pick(self, needed_bytes: int) -> Volume:
        """Return the first enabled volume that can hold ``needed_bytes``."""

        if needed_bytes < 0:
            raise StorageFullError("needed space cannot be negative")
        for item in self.storage.volumes:
            if not item.enabled:
                continue
            free = self.available_bytes(item)
            if free >= needed_bytes:
                return self._wrap(item)
        raise StorageFullError(
            f"no volume has {needed_bytes} bytes free after the configured reserve"
        )

    def pick_largest(self) -> Volume:
        """Return the enabled volume with the most free space.

        Used when the incoming object size is not known yet, such as an SSH
        pull of a session. A reserve is still held back on every disk.
        """

        best: VolumeConfig | None = None
        best_free = -1
        for item in self.storage.volumes:
            if not item.enabled or not item.root.exists():
                continue
            free = self.available_bytes(item)
            if free > best_free:
                best = item
                best_free = free
        if best is None or best_free <= 0:
            raise StorageFullError("no enabled volume has free space above the reserve")
        return self._wrap(best)

    def available_bytes(self, item: VolumeConfig) -> int:
        usage = self._disk_usage(item.root)
        return max(0, int(usage.free) - self.storage.reserve_bytes)

    def describe(self) -> list[dict]:
        rows = []
        for item in self.storage.volumes:
            if not item.root.exists():
                rows.append(
                    {
                        "id": item.id,
                        "root": str(item.root),
                        "enabled": item.enabled,
                        "mounted": False,
                        "total_bytes": 0,
                        "free_bytes": 0,
                        "available_bytes": 0,
                    }
                )
                continue
            usage = self._disk_usage(item.root)
            rows.append(
                {
                    "id": item.id,
                    "root": str(item.root),
                    "enabled": item.enabled,
                    "mounted": True,
                    "total_bytes": int(usage.total),
                    "free_bytes": int(usage.free),
                    "available_bytes": self.available_bytes(item) if item.enabled else 0,
                }
            )
        return rows

    @staticmethod
    def _wrap(item: VolumeConfig) -> Volume:
        return Volume(id=item.id, root=item.root)
