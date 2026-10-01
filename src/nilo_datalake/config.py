"""Configuration loaded from a YAML file, a ``.env`` file, and the environment.

``load_settings`` lets environment variables override the YAML file. Nested
fields use a double underscore: ``NILO_INGEST__API_KEY`` overrides
``ingest.api_key``.

The process started by Docker uses ``open_runtime_settings``. The first start
copies the bootstrap file and the environment into ``NILO_CONFIG``. After that
file exists, the web console is the source of truth and the environment is
not applied again.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

_GIB = 1024**3
_runtime_config_path: ContextVar[Path | None] = ContextVar("nilo_runtime_config_path", default=None)


class VolumeConfig(BaseModel):
    id: str
    root: Path
    enabled: bool = True


class StorageConfig(BaseModel):
    """One entry per mounted disk. The first disk that can fit a write is used."""

    volumes: list[VolumeConfig] = Field(
        default_factory=lambda: [VolumeConfig(id="disk1", root=Path("/var/lib/nilo-datalake"))]
    )
    reserve_bytes: int = 50 * _GIB
    min_free_bytes: int = 1 * _GIB
    catalog_path: Path = Path("/var/lib/nilo-datalake/catalog/catalog.sqlite")


class IngestConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8088
    api_key: str = ""
    allow_insecure_no_auth: bool = False


class MongoCollectionConfig(BaseModel):
    name: str
    timestamp_field: str = "updated_at"
    timestamp_is_string: bool = False
    chunk_size: int = 2000


class MongoConfig(BaseModel):
    enabled: bool = False
    uri: str = "mongodb://localhost:27017"
    database: str = "nilo"
    collections: list[MongoCollectionConfig] = Field(default_factory=list)


class MinioBucketConfig(BaseModel):
    name: str
    prefix: str = ""


class MinioConfig(BaseModel):
    """Objects under ``{session_prefix}{session_id}/`` are filed by session."""

    enabled: bool = False
    endpoint: str = "localhost:9000"
    access_key: str = ""
    secret_key: str = ""
    secure: bool = False
    region: str = ""
    session_prefix: str = "sessions/"
    buckets: list[MinioBucketConfig] = Field(default_factory=list)


class HttpSourceConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    page_limit: int = 100
    timeout_seconds: float = 600


class SshSourceConfig(BaseModel):
    """A remote ``ready/`` directory of completed sessions.

    ``remote`` is either a local path (``/srv/export/ready``) or an SSH path
    (``nilo@10.0.0.5:/var/nilo/spool/ready``). rsync runs over SSH, so a
    partial video is resumed on the next run instead of being copied again.
    """

    name: str
    remote: str
    delete_after: bool = False


class SshPullConfig(BaseModel):
    enabled: bool = False
    binary: str = "rsync"
    ssh_command: str = "ssh"
    sources: list[SshSourceConfig] = Field(default_factory=list)


class PullConfig(BaseModel):
    enabled: bool = False
    schedule: str = "0 2 * * *"
    timezone: str = "UTC"
    safety_delay_seconds: int = 120
    mongo: MongoConfig = Field(default_factory=MongoConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    http: HttpSourceConfig = Field(default_factory=HttpSourceConfig)
    ssh: SshPullConfig = Field(default_factory=SshPullConfig)


class InboxConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 60
    abandon_after_hours: int = 72


class TraceConfig(BaseModel):
    """Where failure traces are written. Empty uses ``{catalog_dir}/traces``."""

    directory: Path | None = None


class ConsoleConfig(BaseModel):
    """Login for the web console. The password is stored in the runtime settings file."""

    enabled: bool = True
    username: str = "admin"
    password: str = ""


class EdgeConfig(BaseModel):
    spool_dir: Path = Path("/var/nilo/spool")
    transport: Literal["http", "rsync"] = "http"
    interval_seconds: int = 3600
    datalake_url: str = "http://127.0.0.1:8088"
    api_key: str = ""
    rsync_target: str = ""
    rsync_binary: str = "rsync"
    source_name: str = "edge"
    timeout_seconds: float = 3600


class Settings(BaseSettings):
    site_id: str = "site-local"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    pull: PullConfig = Field(default_factory=PullConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    edge: EdgeConfig = Field(default_factory=EdgeConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="NILO_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    env = os.environ.get("NILO_CONFIG")
    if env:
        return Path(env)
    candidate = Path("config/settings.yaml")
    if candidate.is_file():
        return candidate
    return None


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings. Environment variables win over the YAML file."""

    path = resolve_config_path(config_path)

    class Loaded(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
            if path is not None:
                sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=path))
            sources.append(file_secret_settings)
            return tuple(sources)

    if path is not None and not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    return Loaded()


def load_file_only(path: Path) -> Settings:
    """Load a settings file and ignore environment variables.

    The web console writes this file. Environment values are applied only
    once, when the file is created, so later edits are not overwritten on restart.
    """

    class Loaded(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (init_settings, YamlConfigSettingsSource(settings_cls, yaml_file=path))

    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    return Loaded()


def save_settings(path: Path, settings: Settings) -> None:
    """Write settings atomically. The file is readable only by its owner."""

    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def current_config_path() -> Path | None:
    return _runtime_config_path.get()


def open_runtime_settings(explicit: Path | None = None) -> Settings:
    """Open the console-managed file, creating it from the bootstrap file on first use."""

    runtime = explicit if explicit is not None else resolve_config_path(None)
    if runtime is not None and runtime.is_file():
        _runtime_config_path.set(runtime)
        return load_file_only(runtime)
    bootstrap_env = os.environ.get("NILO_BOOTSTRAP_CONFIG")
    bootstrap = Path(bootstrap_env) if bootstrap_env else None
    if runtime is not None and bootstrap is not None and bootstrap.is_file():
        seeded = load_settings(bootstrap)
        save_settings(runtime, seeded)
        _runtime_config_path.set(runtime)
        return load_file_only(runtime)
    settings = load_settings(explicit)
    _runtime_config_path.set(runtime if runtime is not None and runtime.is_file() else None)
    return settings


def redact(settings: Settings) -> dict:
    """Return a JSON-ready view of the settings with secrets removed."""

    data = settings.model_dump(mode="json")
    data["ingest"]["api_key"] = _mask(data["ingest"]["api_key"])
    data["pull"]["mongo"]["uri"] = _mask_uri(data["pull"]["mongo"]["uri"])
    data["pull"]["minio"]["access_key"] = _mask(data["pull"]["minio"]["access_key"])
    data["pull"]["minio"]["secret_key"] = _mask(data["pull"]["minio"]["secret_key"])
    data["pull"]["http"]["api_key"] = _mask(data["pull"]["http"]["api_key"])
    data["edge"]["api_key"] = _mask(data["edge"]["api_key"])
    data["console"]["password"] = _mask(data["console"]["password"])
    return data


def _mask(value: str) -> str:
    if not value:
        return ""
    return "***"


def _mask_uri(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
