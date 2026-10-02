"""HTTP routes for the settings console."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import signal
import threading
import time
from importlib.resources import files
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nilo_datalake.config import Settings, save_settings
from nilo_datalake.console.store import apply_form, view_settings
from nilo_datalake.console.summary import build_dashboard
from nilo_datalake.errors import ConfigError
from nilo_datalake.passwords import verify_password
from nilo_datalake.service import build_context
from nilo_datalake.sources.http_api import assert_download_url
from nilo_datalake.sync import run_sync, start_scheduler

log = logging.getLogger(__name__)

COOKIE = "nilo_console"
_MAX_AGE = 12 * 60 * 60
_LOCK_AFTER = 8
_LOCK_SECONDS = 60.0
_failures: dict[str, list[float]] = {}


class LoginBody(BaseModel):
    username: str
    password: str


def install_console(app: FastAPI, config_path: Path | None) -> None:
    app.state.config_path = config_path
    static_dir = Path(str(files("nilo_datalake.console").joinpath("static")))
    app.mount("/console/static", StaticFiles(directory=static_dir), name="console-static")
    router = APIRouter(prefix="/console")

    @router.get("")
    def page() -> HTMLResponse:
        build = os.environ.get("NILO_BUILD_SHA", "dev")[:12]
        html = (static_dir / "index.html").read_text(encoding="utf-8").replace("{{BUILD}}", build)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @router.get("/api/version")
    def console_version() -> dict:
        return {"build": os.environ.get("NILO_BUILD_SHA", "unknown")}

    @router.post("/api/login")
    def login(body: LoginBody, request: Request):
        settings = _settings(request)
        _ensure_enabled(settings)
        attempt = f"{request.client.host if request.client else '-'}|{body.username}"
        if _locked(attempt):
            raise HTTPException(status_code=429, detail="too many attempts; wait a minute and try again")
        if not _same(body.username, settings.console.username) or not _password_ok(body.password, settings):
            _record_failure(attempt)
            raise HTTPException(status_code=401, detail="unknown username or password")
        _failures.pop(attempt, None)
        response = {"ok": True, "username": settings.console.username}
        from fastapi.responses import JSONResponse

        result = JSONResponse(response)
        result.set_cookie(
            COOKIE,
            _sign(settings.console.username, _cookie_key(settings)),
            max_age=_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return result

    @router.post("/api/logout")
    def logout():
        from fastapi.responses import JSONResponse

        result = JSONResponse({"ok": True})
        result.delete_cookie(COOKIE, path="/")
        return result

    @router.get("/api/settings")
    def get_settings(request: Request) -> dict:
        settings = _require(request)
        return view_settings(settings)

    @router.get("/api/dashboard")
    def get_dashboard(request: Request) -> dict:
        _require(request)
        ctx = request.app.state.ctx
        running = getattr(request.app.state, "sync_thread", None)
        body = build_dashboard(ctx, last_sync=getattr(request.app.state, "last_sync", None))
        body["sync_running"] = running is not None and running.is_alive()
        return body

    @router.put("/api/settings")
    def put_settings(payload: dict, request: Request) -> dict:
        current = _require(request)
        path = request.app.state.config_path
        if path is None:
            raise HTTPException(status_code=400, detail="this process has no runtime settings file")
        try:
            updated = apply_form(current, payload)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            save_settings(path, updated)
            restart_required = reload_runtime(request.app, updated)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"could not apply settings: {exc}") from exc
        log.info("console saved settings path=%s restart_required=%s", path, restart_required)
        response_body = view_settings(request.app.state.ctx.settings)
        response_body["restart_required"] = restart_required
        from fastapi.responses import JSONResponse

        result = JSONResponse(response_body)
        # The cookie is signed with the console password. Reissue it when that password changes.
        fresh = request.app.state.ctx.settings
        result.set_cookie(
            COOKIE,
            _sign(fresh.console.username, _cookie_key(fresh)),
            max_age=_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return result

    @router.post("/api/test/{kind}")
    def test_connection(kind: str, payload: dict, request: Request) -> dict:
        current = _require(request)
        try:
            candidate = apply_form(current, payload)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            detail = _probe(kind, candidate)
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("console connection test failed kind=%s", kind)
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": detail}

    @router.post("/api/sync")
    def sync_now(request: Request) -> dict:
        """Start one pull in the background. The schedule is unchanged."""

        _require(request)
        running = getattr(request.app.state, "sync_thread", None)
        if running is not None and running.is_alive():
            return {"started": False, "reason": "sync already running"}

        def _run() -> None:
            request.app.state.last_sync = run_sync(request.app.state.ctx)

        thread = threading.Thread(target=_run, name="nilo-sync", daemon=True)
        request.app.state.sync_thread = thread
        thread.start()
        return {"started": True}

    @router.get("/api/sync")
    def sync_status(request: Request) -> dict:
        _require(request)
        running = getattr(request.app.state, "sync_thread", None)
        return {
            "running": running is not None and running.is_alive(),
            "last": getattr(request.app.state, "last_sync", None),
        }

    @router.post("/api/restart")
    def restart(request: Request) -> dict:
        _require(request)
        schedule_restart()
        return {"restarting": True}

    app.include_router(router)

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/console")


def reload_runtime(app: FastAPI, settings: Settings) -> bool:
    """Swap the running configuration and rebuild the scheduler. Returns whether a process restart is still required."""

    previous = app.state.ctx.settings
    restart_required = (previous.ingest.host, previous.ingest.port) != (settings.ingest.host, settings.ingest.port)
    app.state.ctx = build_context(settings)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    app.state.scheduler = start_scheduler(app.state.ctx)
    logging.getLogger().setLevel(settings.log_level.upper())
    return restart_required


def schedule_restart(delay: float = 0.4, killer=None) -> None:
    """Ask the process to exit so Docker (or systemd) starts it again with the saved file."""

    killer = killer or os.kill

    def _exit() -> None:
        time.sleep(delay)
        killer(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_exit, daemon=True).start()


def _probe(kind: str, settings: Settings) -> str:
    if kind == "mongo":
        from pymongo import MongoClient

        client = MongoClient(settings.pull.mongo.uri, serverSelectionTimeoutMS=4000)
        try:
            client.admin.command("ping")
        finally:
            client.close()
        return "MongoDB answered"
    if kind == "minio":
        from minio import Minio

        client = Minio(
            settings.pull.minio.endpoint,
            access_key=settings.pull.minio.access_key,
            secret_key=settings.pull.minio.secret_key,
            secure=settings.pull.minio.secure,
            region=settings.pull.minio.region or None,
        )
        names = [bucket.name for bucket in client.list_buckets()]
        return "MinIO answered, buckets: " + (", ".join(names) if names else "(none)")
    if kind == "http":
        if not settings.pull.http.base_url:
            raise ConfigError("backend base URL is empty")
        headers = {}
        if settings.pull.http.api_key:
            headers["Authorization"] = f"Bearer {settings.pull.http.api_key}"
        target = settings.pull.http.base_url.rstrip("/") + "/changes"
        assert_download_url(target)
        response = httpx.get(
            target,
            params={"since": "", "limit": 1},
            headers=headers,
            timeout=10,
            follow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise ConfigError(f"backend redirected to {response.headers.get('location', '')}")
        if response.status_code >= 500:
            raise ConfigError(f"backend returned {response.status_code}")
        return f"backend returned {response.status_code}"
    raise HTTPException(status_code=404, detail="unknown connection test")


def _require(request: Request) -> Settings:
    settings = _settings(request)
    _ensure_enabled(settings)
    token = request.cookies.get(COOKIE, "")
    if _read(token, settings) is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return settings


def _settings(request: Request) -> Settings:
    return request.app.state.ctx.settings


def _ensure_enabled(settings: Settings) -> None:
    if not settings.console.enabled:
        raise HTTPException(status_code=404, detail="console is disabled")


def _password_ok(presented: str, settings: Settings) -> bool:
    console = settings.console
    if console.password:
        return _same(presented, console.password)
    if console.password_hash:
        return verify_password(presented, console.password_hash)
    return False


def _cookie_key(settings: Settings) -> str:
    return settings.console.session_secret or settings.console.password


def _locked(key: str) -> bool:
    now = time.time()
    recent = [stamp for stamp in _failures.get(key, []) if now - stamp < _LOCK_SECONDS]
    if recent:
        _failures[key] = recent
    elif key in _failures:
        _failures.pop(key, None)
    return len(recent) >= _LOCK_AFTER


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


def _sign(username: str, password: str) -> str:
    expires = int(time.time()) + _MAX_AGE
    body = f"{username}|{expires}"
    signature = hmac.new(password.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{signature}"


def _read(token: str, settings: Settings) -> str | None:
    parts = token.split("|")
    if len(parts) != 3:
        return None
    username, expires, signature = parts
    if not _same(username, settings.console.username):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    expected = hmac.new(_cookie_key(settings).encode(), f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return username


def _same(left: str, right: str) -> bool:
    left_bytes = left.encode()
    right_bytes = right.encode()
    if len(left_bytes) != len(right_bytes):
        return False
    return hmac.compare_digest(left_bytes, right_bytes)
