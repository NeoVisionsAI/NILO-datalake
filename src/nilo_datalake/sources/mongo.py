"""Incremental MongoDB export.

Each configured collection needs a timestamp field. The cursor is the timestamp
plus ``_id``, so two documents that share a timestamp are not skipped after a
crash. Documents newer than the safety delay stay on the backend until the next
run, which avoids copying a row that is still being written.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nilo_datalake.models import isoformat, utcnow
from nilo_datalake.service import Context

log = logging.getLogger(__name__)


def pull_mongo(ctx: Context, client=None) -> dict:
    config = ctx.settings.pull.mongo
    if not config.enabled:
        return {"source": "mongo", "skipped": True}
    if not config.collections:
        return {"source": "mongo", "skipped": False, "error": "no collections configured"}

    owns_client = client is None
    if owns_client:
        from pymongo import MongoClient

        client = MongoClient(config.uri, serverSelectionTimeoutMS=5000)
    stored = 0
    duplicates = 0
    errors: list[str] = []
    batch_id = _batch_id()
    ctx.catalog.create_batch(
        batch_id=batch_id,
        site_id=ctx.settings.site_id,
        source="mongo",
        volume_id=None,
    )
    try:
        database = client[config.database]
        for collection_config in config.collections:
            try:
                result = _export_collection(
                    ctx,
                    database[collection_config.name],
                    collection_config,
                    batch_id=batch_id,
                )
                stored += result["stored"]
                duplicates += result["duplicates"]
            except Exception as exc:
                log.exception("mongo export failed for %s", collection_config.name)
                errors.append(f"{collection_config.name}: {exc}")
        status = "failed" if errors else "committed"
        ctx.catalog.finish_batch(
            batch_id,
            status=status,
            object_count=stored + duplicates,
            error="; ".join(errors) or None,
        )
    finally:
        if owns_client:
            client.close()
    return {
        "source": "mongo",
        "skipped": False,
        "batch_id": batch_id,
        "stored": stored,
        "duplicates": duplicates,
        "errors": errors,
    }


def mongo_delta_query(
    timestamp_field: str,
    watermark: dict | None,
    cutoff: datetime | str,
    *,
    timestamp_is_string: bool,
) -> dict:
    clauses: list[dict] = [{timestamp_field: {"$lte": cutoff}}]
    if watermark:
        ts: datetime | str
        if timestamp_is_string:
            ts = str(watermark["ts"])
        else:
            ts = datetime.fromisoformat(str(watermark["ts"]))
        identity = coerce_id(str(watermark["id"]))
        clauses.append(
            {
                "$or": [
                    {timestamp_field: {"$gt": ts}},
                    {"$and": [{timestamp_field: ts}, {"_id": {"$gt": identity}}]},
                ]
            }
        )
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def watermark_from_document(document: dict, timestamp_field: str) -> dict:
    raw = document[timestamp_field]
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        ts = isoformat(raw)
    else:
        ts = str(raw)
    return {"ts": ts, "id": str(document["_id"])}


def coerce_id(value: str):
    if len(value) == 24 and all(char in "0123456789abcdefABCDEF" for char in value):
        from bson import ObjectId

        return ObjectId(value)
    return value


def _export_collection(ctx: Context, collection, config, *, batch_id: str) -> dict:
    source_name = f"mongo:{config.name}"
    raw_watermark = ctx.catalog.get_watermark(source_name)
    watermark = json.loads(raw_watermark) if raw_watermark else None
    cutoff_at = utcnow() - timedelta(seconds=ctx.settings.pull.safety_delay_seconds)
    cutoff: datetime | str = isoformat(cutoff_at) if config.timestamp_is_string else cutoff_at
    query = mongo_delta_query(
        config.timestamp_field,
        watermark,
        cutoff,
        timestamp_is_string=config.timestamp_is_string,
    )
    cursor = collection.find(query).sort([(config.timestamp_field, 1), ("_id", 1)])
    stored = 0
    duplicates = 0
    chunk: list[dict] = []
    chunk_index = 0
    last_watermark = watermark

    def flush() -> None:
        nonlocal stored, duplicates, chunk, chunk_index, last_watermark
        if not chunk:
            return
        result = _store_chunk(ctx, config.name, chunk, batch_id=batch_id, chunk_index=chunk_index)
        if result == "stored":
            stored += 1
        else:
            duplicates += 1
        last_watermark = watermark_from_document(chunk[-1], config.timestamp_field)
        ctx.catalog.set_watermark(source_name, json.dumps(last_watermark))
        chunk = []
        chunk_index += 1

    for document in cursor:
        chunk.append(document)
        if len(chunk) >= config.chunk_size:
            flush()
    flush()
    log.info("mongo %s stored_chunks=%s duplicates=%s", config.name, stored, duplicates)
    return {"stored": stored, "duplicates": duplicates}


def _store_chunk(ctx: Context, collection_name: str, documents: list[dict], *, batch_id: str, chunk_index: int) -> str:
    from bson import json_util

    volume = ctx.volumes.pick(1)
    directory = volume.staging() / "_pull"
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="mongo-", suffix=".ndjson", dir=directory, delete=False)
    path = Path(handle.name)
    try:
        with handle:
            for document in documents:
                handle.write(json_util.dumps(document).encode("utf-8"))
                handle.write(b"\n")
        logical = f"{batch_id}-{chunk_index:05d}.ndjson"
        result = ctx.writer.store_file(
            path,
            site_id=ctx.settings.site_id,
            session_id="",
            logical_path=logical,
            kind="metadata",
            content_type="application/x-ndjson",
            batch_id=batch_id,
            origin="mongo",
            collection=collection_name,
            volume=volume,
            move=True,
            source_locator=f"mongo:{collection_name}:{logical}",
        )
        return result.status
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _batch_id() -> str:
    from uuid import uuid4

    return uuid4().hex
