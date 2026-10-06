from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from fastapi.testclient import TestClient

from nilo_datalake.api import create_app
from nilo_datalake.config import (
    ConsoleConfig,
    IngestConfig,
    Settings,
    _runtime_config_path,
    load_file_only,
    open_runtime_settings,
    save_settings,
)
from nilo_datalake.console.routes import schedule_restart
from nilo_datalake.console.store import build_mongo_uri, mongo_password, parse_mongo_uri


def test_login_and_settings_round_trip(ctx, tmp_path) -> None:
    path = tmp_path / "settings.yaml"
    ctx.settings.console = ConsoleConfig(username="admin", password="console-secret")
    save_settings(path, ctx.settings)
    with TestClient(create_app(ctx, path)) as client:
        page = client.get("/console")
        assert page.status_code == 200
        assert "/console/static/app.js" in page.text
        script = client.get("/console/static/app.js")
        assert script.status_code == 200
        assert "Sign in" in script.text
        home = client.get("/")
        assert home.status_code == 200
        assert "NILO archive console" in home.text

        denied = client.get("/console/api/settings")
        assert denied.status_code == 401
        bad = client.post("/console/api/login", json={"username": "admin", "password": "wrong-pass"})
        assert bad.status_code == 401
        ok = client.post("/console/api/login", json={"username": "admin", "password": "console-secret"})
        assert ok.status_code == 200

        view = client.get("/console/api/settings")
        assert view.status_code == 200
        body = view.json()
        assert body["settings"]["ingest"]["api_key"] == ""
        assert "ingest.api_key" in body["secrets_set"]
        assert body["settings"]["pull"]["mongo"]["uri"] == ""

        body["settings"]["pull"]["schedule"] = "not a cron"
        rejected = client.put("/console/api/settings", json=_payload(body))
        assert rejected.status_code == 422
        assert load_file_only(path).pull.schedule == "0 2 * * *"

        body["settings"]["pull"]["schedule"] = "15 3 * * *"
        saved = client.put("/console/api/settings", json=_payload(body))
        assert saved.status_code == 200
        stored = load_file_only(path)
        assert stored.pull.schedule == "15 3 * * *"
        assert stored.ingest.api_key == "secret"

        body["settings"]["ingest"]["api_key"] = "rotated"
        body["settings"]["ingest"]["host"] = "127.0.0.1"
        changed = client.put("/console/api/settings", json=_payload(body))
        assert changed.status_code == 200
        assert changed.json()["restart_required"] is True
        assert load_file_only(path).ingest.api_key == "rotated"
        assert client.get("/console/api/settings").status_code == 200

        unknown = client.post("/console/api/test/nope", json=_payload(body))
        assert unknown.status_code == 404

        dash = client.get("/console/api/dashboard")
        assert dash.status_code == 200
        panel = dash.json()
        assert "disks" in panel
        assert "last_session_backup" in panel
        assert "last_database_backup" in panel

        spec = client.get("/console/api/openapi")
        assert spec.status_code == 200
        assert "/v1/health" in spec.json()["paths"]

        vol_root = str(load_file_only(path).storage.volumes[0].root)
        check = client.get("/console/api/storage/check", params={"path": vol_root})
        assert check.status_code == 200
        assert check.json()["exists"] is True

        browse = client.get("/console/api/storage/browse", params={"path": vol_root})
        assert browse.status_code == 200
        assert browse.json()["path"] == str(Path(vol_root).resolve())

        health_try = client.post("/console/api/archive/try/health", json=_payload(body))
        assert health_try.status_code == 200
        assert health_try.json()["ok"] is True


def test_file_only_ignores_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.yaml"
    save_settings(path, Settings(ingest=IngestConfig(api_key="file-key"), console=ConsoleConfig(password="pw")))
    monkeypatch.setenv("NILO_INGEST__API_KEY", "env-key")
    assert load_file_only(path).ingest.api_key == "file-key"


def test_open_runtime_seeds_once(tmp_path, monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("NILO_"):
            monkeypatch.delenv(key, raising=False)
    bootstrap = tmp_path / "bootstrap.yaml"
    runtime = tmp_path / "runtime" / "settings.yaml"
    save_settings(
        bootstrap,
        Settings(
            site_id="from-file",
            ingest=IngestConfig(api_key="file-key"),
            console=ConsoleConfig(username="admin", password="pw"),
        ),
    )
    monkeypatch.setenv("NILO_BOOTSTRAP_CONFIG", str(bootstrap))
    monkeypatch.setenv("NILO_INGEST__API_KEY", "from-env")
    token = _runtime_config_path.set(None)
    try:
        first = open_runtime_settings(runtime)
        assert first.site_id == "from-file"
        assert first.ingest.api_key == "from-env"
        monkeypatch.setenv("NILO_INGEST__API_KEY", "later")
        second = open_runtime_settings(runtime)
        assert second.ingest.api_key == "from-env"
    finally:
        _runtime_config_path.reset(token)


def test_mongo_login_round_trip() -> None:
    uri = build_mongo_uri(username="nilo", password="p@ss", host="mongo:27017", auth_source="admin")
    assert uri == "mongodb://nilo:p%40ss@mongo:27017/?authSource=admin"
    parsed = parse_mongo_uri(uri)
    assert parsed["username"] == "nilo"
    assert parsed["host"] == "mongo:27017"
    assert parsed["auth_source"] == "admin"
    assert parsed["password"] == ""
    assert parsed["password_set"] is True
    assert mongo_password(uri) == "p@ss"


def test_schedule_restart_does_not_use_the_default_signal() -> None:
    seen: list[tuple[int, int]] = []
    schedule_restart(delay=0, killer=lambda pid, sig: seen.append((pid, sig)))
    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    assert seen == [(os.getpid(), signal.SIGTERM)]


def _payload(body: dict) -> dict:
    return {"settings": body["settings"], "mongo_login": body["mongo_login"]}
