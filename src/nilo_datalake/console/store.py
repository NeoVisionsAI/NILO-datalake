"""Turn a console form into a Settings object, keeping secrets that were left blank."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError

from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError
from nilo_datalake.paths import sanitize_site_id

_SECRET_FIELDS = (
    ("ingest", "api_key"),
    ("pull", "minio", "secret_key"),
    ("pull", "http", "api_key"),
    ("edge", "api_key"),
    ("console", "password"),
)


def view_settings(settings: Settings) -> dict:
    """Settings for the form. Secret strings are empty; ``secrets_set`` says which exist."""

    data = settings.model_dump(mode="json")
    data["pull"]["mongo"]["uri"] = ""
    secrets_set: list[str] = []
    for path in _SECRET_FIELDS:
        cursor = data
        for key in path[:-1]:
            cursor = cursor[key]
        leaf = path[-1]
        if cursor.get(leaf):
            secrets_set.append(".".join(path))
        cursor[leaf] = ""
    return {
        "settings": data,
        "secrets_set": secrets_set,
        "mongo_login": parse_mongo_uri(settings.pull.mongo.uri),
    }


def apply_form(current: Settings, payload: dict) -> Settings:
    """Validate a console submission and restore secrets that arrived blank."""

    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        raise ConfigError("settings object is required")
    incoming = payload["settings"]
    _restore_blanks(current.model_dump(mode="json"), incoming)
    try:
        updated = Settings.model_validate(incoming)
    except ValidationError as exc:
        raise ConfigError(_validation_message(exc)) from exc
    login = payload.get("mongo_login") or {}
    if isinstance(login, dict) and (login.get("host") or login.get("username")):
        password = str(login.get("password") or "")
        if not password:
            password = mongo_password(current.pull.mongo.uri)
        updated.pull.mongo.uri = build_mongo_uri(
            username=str(login.get("username") or ""),
            password=password,
            host=str(login.get("host") or ""),
            auth_source=str(login.get("auth_source") or "admin"),
        )
    elif not updated.pull.mongo.uri:
        updated.pull.mongo.uri = current.pull.mongo.uri
    _check(updated)
    return updated


def parse_mongo_uri(uri: str) -> dict:
    parsed = urlparse(uri or "")
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth_source = "admin"
    for part in (parsed.query or "").split("&"):
        if part.startswith("authSource="):
            auth_source = unquote(part.split("=", 1)[1])
    return {
        "username": unquote(parsed.username or ""),
        "password": "",
        "password_set": bool(parsed.password),
        "host": host,
        "auth_source": auth_source,
    }


def mongo_password(uri: str) -> str:
    parsed = urlparse(uri or "")
    return unquote(parsed.password or "")


def build_mongo_uri(*, username: str, password: str, host: str, auth_source: str) -> str:
    host = host.strip()
    if not host or "://" in host or "@" in host:
        raise ConfigError("MongoDB host must be host or host:port, without a scheme")
    auth = quote((auth_source or "admin").strip(), safe="")
    if username:
        user = quote(username, safe="")
        secret = quote(password, safe="")
        return f"mongodb://{user}:{secret}@{host}/?authSource={auth}"
    return f"mongodb://{host}/"


def _restore_blanks(current: dict, incoming: dict) -> None:
    for path in _SECRET_FIELDS:
        new_value = _dig(incoming, path)
        old_value = _dig(current, path)
        if new_value in ("", None) and old_value:
            _put(incoming, path, old_value)


def _dig(payload: dict, path: tuple[str, ...]):
    cursor = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _put(payload: dict, path: tuple[str, ...], value: str) -> None:
    cursor = payload
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[path[-1]] = value


def _check(settings: Settings) -> None:
    try:
        sanitize_site_id(settings.site_id)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
    if not settings.console.username.strip() or "|" in settings.console.username:
        raise ConfigError("console username is required and cannot contain |")
    if not settings.console.password:
        raise ConfigError("console password is required")
    try:
        CronTrigger.from_crontab(settings.pull.schedule, timezone=ZoneInfo(settings.pull.timezone))
    except Exception as exc:
        raise ConfigError(f"pull schedule is not a valid cron expression: {exc}") from exc
    if settings.inbox.poll_seconds < 0:
        raise ConfigError("inbox poll interval cannot be negative")
    if settings.edge.interval_seconds <= 0:
        raise ConfigError("edge interval must be positive")
    enabled = [item for item in settings.storage.volumes if item.enabled]
    if not enabled:
        raise ConfigError("at least one storage volume must be enabled")
    seen: set[str] = set()
    for item in settings.storage.volumes:
        if not item.id.strip():
            raise ConfigError("every volume needs an id")
        if item.id in seen:
            raise ConfigError(f"duplicate volume id: {item.id}")
        seen.add(item.id)
    if settings.pull.mongo.enabled and not settings.pull.mongo.uri:
        raise ConfigError("MongoDB is enabled but the host is empty")
    if settings.pull.minio.enabled and not settings.pull.minio.endpoint:
        raise ConfigError("MinIO is enabled but the endpoint is empty")


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error["loc"])
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts) or "settings are invalid"
