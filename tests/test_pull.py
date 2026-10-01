from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from nilo_datalake.config import (
    HttpSourceConfig,
    MinioBucketConfig,
    MinioConfig,
    MongoCollectionConfig,
    MongoConfig,
    SshPullConfig,
    SshSourceConfig,
)
from nilo_datalake.sources.ssh_pull import pull_ssh
from nilo_datalake.sources.http_api import pull_http
from nilo_datalake.sources.minio_source import pull_minio, split_object_key
from nilo_datalake.sources.mongo import mongo_delta_query, pull_mongo


class _Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, _spec):
        return self

    def __iter__(self):
        return iter(self.documents)


class _Collection:
    def __init__(self, documents):
        self.documents = documents
        self.queries = []

    def find(self, query):
        self.queries.append(query)
        return _Cursor(self.documents)


class _Database:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


class _Mongo:
    def __init__(self, database):
        self.database = database

    def __getitem__(self, _name):
        return self.database

    def close(self):
        return None


class _Object:
    def __init__(self, name, body, etag, modified):
        self.object_name = name
        self.size = len(body)
        self.etag = etag
        self.last_modified = modified
        self.is_dir = False
        self.body = body


class _Minio:
    def __init__(self, objects):
        self.objects = objects

    def list_objects(self, _bucket, prefix="", recursive=True):
        return [item for item in self.objects if item.object_name.startswith(prefix)]

    def fget_object(self, _bucket, key, path):
        body = next(item.body for item in self.objects if item.object_name == key)
        with open(path, "wb") as handle:
            handle.write(body)


def test_mongo_query_uses_timestamp_and_id() -> None:
    cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)
    query = mongo_delta_query(
        "updated_at",
        {"ts": "2026-09-01T00:00:00+00:00", "id": "507f1f77bcf86cd799439011"},
        cutoff,
        timestamp_is_string=False,
    )
    assert "$and" in query
    assert query["$and"][0] == {"updated_at": {"$lte": cutoff}}


def test_mongo_export_advances_the_watermark_by_chunk(ctx, settings) -> None:
    settings.pull.mongo = MongoConfig(
        enabled=True,
        collections=[MongoCollectionConfig(name="sessions", chunk_size=2)],
    )
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    documents = [
        {"_id": "a", "updated_at": past, "session_id": "sess-1"},
        {"_id": "b", "updated_at": past, "session_id": "sess-1"},
        {"_id": "c", "updated_at": past, "session_id": "sess-2"},
    ]
    collection = _Collection(documents)
    report = pull_mongo(ctx, client=_Mongo(_Database({"sessions": collection})))
    assert report["stored"] == 2
    assert report["errors"] == []
    watermark = json.loads(ctx.catalog.get_watermark("mongo:sessions"))
    assert watermark["id"] == "c"
    exported = list(settings.storage.volumes[0].root.rglob("*.ndjson"))
    assert len(exported) == 2


def test_minio_copies_settled_session_objects_once(ctx, settings) -> None:
    assert split_object_key("sessions/sess-1/video/cam.mp4", "sessions/") == ("sess-1", "video/cam.mp4")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh = datetime.now(timezone.utc) + timedelta(minutes=5)
    objects = [
        _Object("sessions/sess-1/video/cam.mp4", b"video-bytes", "etag-1", old),
        _Object("sessions/sess-1/video/live.mp4", b"too-new", "etag-2", fresh),
    ]
    settings.pull.minio = MinioConfig(
        enabled=True,
        buckets=[MinioBucketConfig(name="nilo-media")],
    )
    client = _Minio(objects)
    first = pull_minio(ctx, client=client)
    assert first["stored"] == 1
    assert first["deferred"] == 1
    second = pull_minio(ctx, client=client)
    assert second["stored"] == 0
    assert second["unchanged"] == 1
    archived = list(settings.storage.volumes[0].root.rglob("cam.mp4"))
    assert len(archived) == 1


def test_ssh_pull_takes_only_completed_sessions(ctx, settings, tmp_path) -> None:
    remote = tmp_path / "ready"
    done = remote / "sess-1" / "audio"
    done.mkdir(parents=True)
    (done / "mic.wav").write_bytes(b"sound")
    (remote / "sess-1" / "COMPLETE").write_text("\n", encoding="utf-8")
    partial = remote / "sess-2" / "audio"
    partial.mkdir(parents=True)
    (partial / "mic.wav").write_bytes(b"still-open")
    settings.pull.ssh = SshPullConfig(
        enabled=True,
        sources=[SshSourceConfig(name="ward", remote=str(remote), delete_after=True)],
    )
    first = pull_ssh(ctx)
    assert first["errors"] == []
    assert first["sessions"] == 1
    assert first["stored"] == 1
    assert not (remote / "sess-1").exists()
    assert (remote / "sess-2").is_dir()
    second = pull_ssh(ctx)
    assert second["sessions"] == 0
    assert second["stored"] == 0
    archived = list(settings.storage.volumes[0].root.rglob("mic.wav"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"sound"


def test_http_pull_stores_metadata_and_objects(ctx, settings) -> None:
    body = b"waveform"
    digest = hashlib.sha256(body).hexdigest()
    pages = {
        "": {
            "watermark": "page-2",
            "items": [
                {"type": "metadata", "collection": "sessions", "document": {"_id": "sess-1"}},
                {
                    "type": "object",
                    "session_id": "sess-1",
                    "logical_path": "physiological/ecg.bin",
                    "kind": "physiological",
                    "sha256": digest,
                    "size_bytes": len(body),
                    "content_type": "application/octet-stream",
                    "download_url": "objects/ecg",
                },
            ],
        },
        "page-2": {"watermark": "page-2", "items": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/changes"):
            since = request.url.params.get("since", "")
            return httpx.Response(200, json=pages[since])
        if request.url.path.endswith("/objects/ecg"):
            assert request.headers["authorization"] == "Bearer backend-key"
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    settings.pull.http = HttpSourceConfig(
        enabled=True,
        base_url="http://backend.test/archive/v1",
        api_key="backend-key",
    )
    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer backend-key"}) as client:
        report = pull_http(ctx, client=client)
    assert report["errors"] == []
    assert report["stored"] == 2
    assert ctx.catalog.get_watermark("http") == "page-2"
    assert list(settings.storage.volumes[0].root.rglob("ecg.bin"))
    assert list(settings.storage.volumes[0].root.rglob("*.ndjson"))
