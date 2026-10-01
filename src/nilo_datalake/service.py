"""Wire the catalog, the volumes, and the writer for one process."""

from __future__ import annotations

from dataclasses import dataclass

from nilo_datalake.catalog import Catalog
from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError
from nilo_datalake.paths import sanitize_site_id
from nilo_datalake.volumes import VolumeManager
from nilo_datalake.writer import ArchiveWriter


@dataclass
class Context:
    settings: Settings
    catalog: Catalog
    volumes: VolumeManager
    writer: ArchiveWriter


def build_context(settings: Settings) -> Context:
    sanitize_site_id(settings.site_id)
    enabled = [item for item in settings.storage.volumes if item.enabled]
    if not enabled:
        raise ConfigError("at least one storage volume must be enabled")
    seen: set[str] = set()
    for item in settings.storage.volumes:
        if item.id in seen:
            raise ConfigError(f"duplicate volume id: {item.id}")
        seen.add(item.id)
        if item.enabled:
            item.root.mkdir(parents=True, exist_ok=True)
            for name in ("archive", "inbox", "staging", "quarantine"):
                (item.root / name).mkdir(parents=True, exist_ok=True)
    settings.storage.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(settings.storage.catalog_path)
    catalog.migrate()
    volumes = VolumeManager(settings.storage)
    writer = ArchiveWriter(settings, catalog, volumes)
    return Context(settings=settings, catalog=catalog, volumes=volumes, writer=writer)
