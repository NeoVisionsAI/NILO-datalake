from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from nilo_datalake.api import create_app
from nilo_datalake.config import ConsoleConfig, load_file_only, save_settings
from nilo_datalake.console import routes as console_routes
from nilo_datalake.service import build_context
from nilo_datalake.sources.http_api import assert_download_url
from nilo_datalake.sources.ssh_pull import assert_deletable, validate_remote, validate_ssh_command


def test_docs_are_not_published_and_responses_deny_framing(ctx) -> None:
    with TestClient(create_app(ctx)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        health = client.get("/v1/health")
        assert health.status_code == 200
        assert health.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]


def test_bearer_of_a_different_length_is_rejected(ctx) -> None:
    with TestClient(create_app(ctx)) as client:
        response = client.post("/v1/batches", headers={"Authorization": "Bearer x"})
    assert response.status_code == 401


def test_upload_requires_a_bounded_body(ctx, settings) -> None:
    settings.ingest.max_object_bytes = 4
    with TestClient(create_app(ctx)) as client:
        headers = {"Authorization": "Bearer secret"}
        batch_id = client.post("/v1/batches", json={"source": "backend"}, headers=headers).json()["batch_id"]
        too_big = client.put(
            f"/v1/batches/{batch_id}/objects",
            params={"logical_path": "face/cam.mp4"},
            content=b"12345",
            headers={**headers, "X-Checksum-Sha256": "a" * 64, "Content-Length": "5"},
        )
        assert too_big.status_code == 413

        def chunks():
            yield b"abc"

        missing = client.put(
            f"/v1/batches/{batch_id}/objects",
            params={"logical_path": "face/cam.mp4"},
            content=chunks(),
            headers={**headers, "X-Checksum-Sha256": "ab" * 32},
        )
        assert missing.status_code == 411


def test_console_password_is_hashed_and_still_logs_in(ctx, tmp_path) -> None:
    path = tmp_path / "settings.yaml"
    ctx.settings.console = ConsoleConfig(username="admin", password="console-secret")
    save_settings(path, ctx.settings)
    assert "console-secret" not in path.read_text(encoding="utf-8")
    loaded = load_file_only(path)
    assert loaded.console.password == ""
    assert loaded.console.password_hash.startswith("scrypt$")
    assert loaded.console.session_secret
    with TestClient(create_app(build_context(loaded), path)) as client:
        denied = client.post("/console/api/login", json={"username": "admin", "password": "wrong-pass"})
        assert denied.status_code == 401
        ok = client.post("/console/api/login", json={"username": "admin", "password": "console-secret"})
        assert ok.status_code == 200
        assert client.get("/console/api/settings").status_code == 200


def test_console_login_locks_after_repeated_failures(ctx, tmp_path) -> None:
    path = tmp_path / "settings.yaml"
    ctx.settings.console = ConsoleConfig(username="locked", password="console-secret")
    save_settings(path, ctx.settings)
    console_routes._failures.clear()
    try:
        with TestClient(create_app(ctx, path)) as client:
            for _ in range(8):
                failed = client.post("/console/api/login", json={"username": "locked", "password": "nope-nope"})
                assert failed.status_code == 401
            blocked = client.post("/console/api/login", json={"username": "locked", "password": "console-secret"})
            assert blocked.status_code == 429
    finally:
        console_routes._failures.clear()


def test_console_can_start_a_sync(ctx, tmp_path) -> None:
    path = tmp_path / "settings.yaml"
    ctx.settings.console = ConsoleConfig(username="admin", password="console-secret")
    save_settings(path, ctx.settings)
    with TestClient(create_app(ctx, path)) as client:
        client.post("/console/api/login", json={"username": "admin", "password": "console-secret"})
        started = client.post("/console/api/sync")
        assert started.status_code == 200
        assert started.json()["started"] is True
        last = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get("/console/api/sync")
            body = status.json()
            if not body["running"] and body["last"] is not None:
                last = body["last"]
                break
            time.sleep(0.05)
    assert last is not None
    assert last["skipped"] is False


def test_download_url_rejects_other_schemes_and_credentials() -> None:
    with pytest.raises(ValueError):
        assert_download_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        assert_download_url("http://user:secret@minio:9000/bucket/key")
    assert_download_url("https://minio.example/bucket/key")


def test_ssh_remote_and_delete_are_bounded() -> None:
    host, path = validate_remote("nilo@nas.example:/var/nilo/spool/ready")
    assert host == "nilo@nas.example"
    assert path == "/var/nilo/spool/ready"
    with pytest.raises(Exception):
        validate_remote("-oProxyCommand=id:/tmp/ready")
    with pytest.raises(Exception):
        validate_ssh_command("ssh; rm -rf /")
    with pytest.raises(Exception):
        assert_deletable("/tmp/ready")
    assert_deletable("/var/nilo/spool/ready/sess-1")
