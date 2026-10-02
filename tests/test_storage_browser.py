from __future__ import annotations

from nilo_datalake.config import Settings, StorageConfig, VolumeConfig
from nilo_datalake.console.storage_browser import check_storage_path, resolve_browse_path


def test_resolve_allows_volume_root(tmp_path) -> None:
    settings = Settings(storage=StorageConfig(volumes=[VolumeConfig(id="d1", root=tmp_path, enabled=True)]))
    resolved = resolve_browse_path(settings, str(tmp_path / "nested"))
    assert resolved == (tmp_path / "nested").resolve()


def test_resolve_rejects_outside_roots(tmp_path) -> None:
    settings = Settings(storage=StorageConfig(volumes=[VolumeConfig(id="d1", root=tmp_path, enabled=True)]))
    try:
        resolve_browse_path(settings, "/etc/passwd")
    except Exception as exc:
        assert "outside" in str(exc).lower()
    else:
        raise AssertionError("expected ConfigError")
