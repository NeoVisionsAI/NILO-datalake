from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from nilo_datalake.api import create_app
from nilo_datalake.client import DatalakeClient
from nilo_datalake.edge import run_once


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_push_commit_and_duplicate(ctx) -> None:
    app = create_app(ctx)
    body = b"face-frame"
    with TestClient(app) as client:
        denied = client.post("/v1/batches", json={"site_id": "hospital-a", "source": "backend"})
        assert denied.status_code == 401
        headers = {"Authorization": "Bearer secret"}
        opened = client.post("/v1/batches", json={"site_id": "hospital-a", "source": "backend"}, headers=headers)
        assert opened.status_code == 200
        batch_id = opened.json()["batch_id"]
        uploaded = client.put(
            f"/v1/batches/{batch_id}/objects",
            params={"logical_path": "face/cam.mp4"},
            content=body,
            headers={
                **headers,
                "X-Checksum-Sha256": _sha(body),
                "X-Session-Id": "sess-1",
                "X-Kind": "face",
                "Content-Type": "video/mp4",
            },
        )
        assert uploaded.status_code == 200
        escaped = client.put(
            f"/v1/batches/{batch_id}/objects",
            params={"logical_path": "../outside.txt"},
            content=b"no",
            headers={**headers, "X-Checksum-Sha256": _sha(b"no")},
        )
        assert escaped.status_code == 400
        committed = client.post(f"/v1/batches/{batch_id}/commit", headers=headers)
        assert committed.status_code == 200
        assert committed.json()["status"] == "committed"
        assert committed.json()["stored"] == 1
        again = client.post(f"/v1/batches/{batch_id}/commit", headers=headers)
        assert again.status_code == 200
        assert again.json()["status"] == "committed"

        second = client.post("/v1/batches", json={"source": "backend"}, headers=headers).json()["batch_id"]
        client.put(
            f"/v1/batches/{second}/objects",
            params={"logical_path": "face/cam.mp4"},
            content=body,
            headers={**headers, "X-Checksum-Sha256": _sha(body), "X-Session-Id": "sess-1", "X-Kind": "face"},
        )
        duplicate = client.post(f"/v1/batches/{second}/commit", headers=headers)
        assert duplicate.json()["duplicates"] == 1
        status = client.get("/v1/status", headers=headers)
        assert status.json()["objects"] == 1
    stored = list(Path(ctx.settings.storage.volumes[0].root).rglob("cam.mp4"))
    assert len(stored) == 1


def test_edge_ships_only_complete_sessions(ctx, settings, tmp_path) -> None:
    settings.edge.spool_dir = tmp_path / "spool"
    ready = settings.edge.spool_dir / "ready"
    complete = ready / "sess-1" / "video"
    complete.mkdir(parents=True)
    (complete / "cam.mp4").write_bytes(b"video")
    (ready / "sess-1" / "COMPLETE").write_text("\n", encoding="utf-8")
    partial = ready / "sess-2" / "video"
    partial.mkdir(parents=True)
    (partial / "cam.mp4").write_bytes(b"still-recording")

    app = create_app(ctx)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("datalake test server did not start")
        time.sleep(0.05)
    try:
        with DatalakeClient(f"http://127.0.0.1:{port}", "secret") as client:
            report = run_once(settings, client=client)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert report["shipped"] == ["sess-1"]
    assert report["errors"] == []
    assert (settings.edge.spool_dir / "sent" / "sess-1" / "video" / "cam.mp4").is_file()
    assert (ready / "sess-2").is_dir()
    assert ctx.catalog.object_count() == 1


def test_edge_rsync_drop_is_promoted(ctx, settings, tmp_path) -> None:
    import shutil

    import pytest

    rsync = shutil.which("rsync")
    if rsync is None:
        pytest.skip("rsync is not installed")
    inbox = settings.storage.volumes[0].root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    settings.edge.spool_dir = tmp_path / "spool"
    settings.edge.transport = "rsync"
    settings.edge.rsync_binary = rsync
    settings.edge.rsync_target = str(inbox)
    session = settings.edge.spool_dir / "ready" / "sess-3" / "postural"
    session.mkdir(parents=True)
    (session / "track.csv").write_bytes(b"t,x\n1,2\n")
    (settings.edge.spool_dir / "ready" / "sess-3" / "COMPLETE").write_text("\n", encoding="utf-8")

    report = run_once(settings)
    assert report["errors"] == []
    from nilo_datalake.inbox import promote_inbox

    promoted = promote_inbox(ctx)
    assert promoted["stored"] == 1
    assert promoted["failed"] == 0
    archived = list((settings.storage.volumes[0].root / "archive").rglob("track.csv"))
    assert len(archived) == 1
