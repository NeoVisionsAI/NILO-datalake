from __future__ import annotations

from pathlib import Path

import pytest

from nilo_datalake.config import InboxConfig, IngestConfig, PullConfig, Settings, StorageConfig, VolumeConfig
from nilo_datalake.service import Context, build_context


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        site_id="hospital-a",
        storage=StorageConfig(
            volumes=[
                VolumeConfig(id="disk1", root=tmp_path / "disk1"),
                VolumeConfig(id="disk2", root=tmp_path / "disk2", enabled=False),
            ],
            reserve_bytes=0,
            min_free_bytes=1,
            catalog_path=tmp_path / "catalog" / "catalog.sqlite",
        ),
        ingest=IngestConfig(enabled=True, api_key="secret"),
        pull=PullConfig(enabled=False, safety_delay_seconds=0),
        inbox=InboxConfig(enabled=False, poll_seconds=0),
    )


@pytest.fixture
def ctx(settings: Settings) -> Context:
    return build_context(settings)
