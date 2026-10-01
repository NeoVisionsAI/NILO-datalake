from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nilo_datalake.checksums import sha256_file
from nilo_datalake.errors import ChecksumMismatchError, InvalidPathError, StorageFullError
from nilo_datalake.kinds import infer_kind
from nilo_datalake.paths import sanitize_logical_path
from nilo_datalake.service import build_context


def test_logical_path_rejects_parent_segments() -> None:
    with pytest.raises(InvalidPathError):
        sanitize_logical_path("../secret")
    with pytest.raises(InvalidPathError):
        sanitize_logical_path("/etc/passwd")
    assert sanitize_logical_path("video/cam 1.mp4") == "video/cam 1.mp4"


def test_kind_uses_parent_directory() -> None:
    assert infer_kind("face/camera.mp4") == "face"
    assert infer_kind("notes.csv") == "physiological"


def test_store_is_idempotent_and_keeps_a_changed_revision(ctx, tmp_path) -> None:
    source = tmp_path / "cam.mp4"
    source.write_bytes(b"frame-a")
    first = ctx.writer.store_file(
        source,
        site_id="hospital-a",
        session_id="sess-1",
        logical_path="video/cam.mp4",
        kind="video",
        content_type="video/mp4",
        batch_id="batch-1",
        origin="test",
    )
    again = ctx.writer.store_file(
        source,
        site_id="hospital-a",
        session_id="sess-1",
        logical_path="video/cam.mp4",
        kind="video",
        content_type="video/mp4",
        batch_id="batch-2",
        origin="test",
    )
    assert first.status == "stored"
    assert again.status == "duplicate"
    assert sha256_file(first.stored.stored_path) == sha256_file(source)

    source.write_bytes(b"frame-b")
    revised = ctx.writer.store_file(
        source,
        site_id="hospital-a",
        session_id="sess-1",
        logical_path="video/cam.mp4",
        kind="video",
        content_type="video/mp4",
        batch_id="batch-3",
        origin="test",
    )
    assert revised.status == "stored"
    assert revised.stored.stored_path.endswith("cam__" + revised.stored.sha256[:12] + ".mp4")
    assert ctx.catalog.object_count() == 2

    ctx.catalog.rebuild_from_journal()
    assert ctx.catalog.object_count() == 2


def test_checksum_mismatch_does_not_publish(ctx, tmp_path) -> None:
    source = tmp_path / "bad.bin"
    source.write_bytes(b"nope")
    with pytest.raises(ChecksumMismatchError):
        ctx.writer.store_file(
            source,
            site_id="hospital-a",
            session_id="sess-1",
            logical_path="audio/mic.wav",
            kind="audio",
            content_type=None,
            batch_id="batch-1",
            origin="test",
            expected_sha256="a" * 64,
        )
    assert ctx.catalog.object_count() == 0
    assert list((ctx.settings.storage.volumes[0].root / "archive").rglob("*")) == []


def test_second_disk_receives_the_write_when_the_first_is_full(settings, tmp_path) -> None:
    settings.storage.volumes[1].enabled = True
    context = build_context(settings)
    free = {"disk1": 10, "disk2": 10_000}

    def usage(path):
        return SimpleNamespace(total=20_000, used=0, free=free[path.name])

    context.volumes._disk_usage = usage
    source = tmp_path / "wide.wav"
    source.write_bytes(b"x" * 100)
    result = context.writer.store_file(
        source,
        site_id="hospital-a",
        session_id="sess-9",
        logical_path="audio/wide.wav",
        kind=None,
        content_type=None,
        batch_id="batch-9",
        origin="test",
    )
    assert result.stored.volume_id == "disk2"
    assert str(settings.storage.volumes[1].root) in result.stored.stored_path


def test_pick_largest_uses_the_emptier_disk(settings) -> None:
    settings.storage.volumes[1].enabled = True
    context = build_context(settings)
    free = {"disk1": 100, "disk2": 5_000}

    def usage(path):
        return SimpleNamespace(total=20_000, used=0, free=free[path.name])

    context.volumes._disk_usage = usage
    assert context.volumes.pick_largest().id == "disk2"


def test_pick_fails_when_every_disk_is_reserved(settings) -> None:
    settings.storage.reserve_bytes = 10**15
    context = build_context(settings)
    with pytest.raises(StorageFullError):
        context.volumes.pick(1)


def test_inbox_promotes_a_ready_drop_and_quarantines_a_bad_checksum(ctx, settings) -> None:
    from nilo_datalake.checksums import sha256_file
    from nilo_datalake.inbox import promote_inbox
    from nilo_datalake.models import isoformat

    volume = settings.storage.volumes[0].root
    good = _drop(
        volume,
        "batch-good",
        b"audio-bytes",
        sha=None,
    )
    assert good == "batch-good"
    report = promote_inbox(ctx)
    assert report["promoted"] == 1
    assert report["stored"] == 1
    archived = list((volume / "archive").rglob("mic.wav"))
    assert len(archived) == 1
    assert not (volume / "inbox" / "batch-good").exists()

    _drop(volume, "batch-bad", b"tampered", sha="b" * 64)
    failed = promote_inbox(ctx)
    assert failed["failed"] == 1
    assert (volume / "quarantine" / "batch-bad" / "ERROR.txt").is_file()
    assert isoformat(datetime.now(timezone.utc))
    assert sha256_file(archived[0])


def _drop(volume, batch_id: str, body: bytes, sha: str | None) -> str:
    import json

    batch = volume / "inbox" / batch_id
    payload = batch / "payload"
    payload.mkdir(parents=True)
    target = payload / "mic.wav"
    target.write_bytes(body)
    digest = sha or sha256_file(target)
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "site_id": "hospital-a",
        "source": "rsync",
        "created_at": "2026-10-01T00:00:00+00:00",
        "objects": [
            {
                "logical_path": "audio/mic.wav",
                "sha256": digest,
                "size_bytes": len(body),
                "kind": "audio",
                "session_id": "sess-7",
                "payload_path": "payload/mic.wav",
            }
        ],
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch / "READY").write_text("ok\n", encoding="utf-8")
    return batch_id


def test_gc_removes_a_stale_open_batch_and_an_incomplete_drop(ctx, settings) -> None:
    from nilo_datalake.inbox import gc_incomplete

    ctx.catalog.create_batch(batch_id="old-batch", site_id="hospital-a", source="push", volume_id="disk1")
    staging = settings.storage.volumes[0].root / "staging" / "old-batch"
    staging.mkdir(parents=True)
    (staging / "partial.bin").write_bytes(b"x")
    with ctx.catalog.connect() as conn:
        conn.execute(
            "UPDATE batches SET created_at = ? WHERE batch_id = ?",
            ("2000-01-01T00:00:00+00:00", "old-batch"),
        )
    incomplete = settings.storage.volumes[0].root / "inbox" / "still-copying"
    incomplete.mkdir(parents=True)
    (incomplete / "manifest.json").write_text("{}", encoding="utf-8")
    old = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    import os

    os.utime(incomplete, (old, old))

    report = gc_incomplete(ctx, older_than_hours=24)
    assert report["abandoned_batches"] == 1
    assert report["removed_incomplete_drops"] == 1
    assert not staging.exists()
    assert not incomplete.exists()
    assert ctx.catalog.get_batch("old-batch")["status"] == "abandoned"
