"""Pull from the backend's archive API.

The backend owns the watermark. This client asks for one page, stores it, and
only then saves the watermark the backend returned. A page whose watermark does
not move aborts the run so a faulty response cannot loop forever.

Relative ``download_url`` values are resolved against ``base_url`` and are sent
with the API key. Absolute URLs are fetched without that key, so the backend
can return a presigned MinIO link.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from nilo_datalake.models import utcnow
from nilo_datalake.paths import is_sha256
from nilo_datalake.service import Context

log = logging.getLogger(__name__)

_MAX_PAGES = 10000


def pull_http(ctx: Context, client: httpx.Client | None = None) -> dict:
    config = ctx.settings.pull.http
    if not config.enabled:
        return {"source": "http", "skipped": True}
    if not config.base_url:
        return {"source": "http", "skipped": False, "error": "http.base_url is empty"}

    owns_client = client is None
    if owns_client:
        headers = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        client = httpx.Client(
            headers=headers,
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )
    batch_id = uuid4().hex
    ctx.catalog.create_batch(
        batch_id=batch_id,
        site_id=ctx.settings.site_id,
        source="http",
        volume_id=None,
    )
    stored = 0
    duplicates = 0
    errors: list[str] = []
    pages = 0
    try:
        watermark = ctx.catalog.get_watermark("http") or ""
        while pages < _MAX_PAGES:
            response = client.get(
                _changes_url(config.base_url),
                params={"since": watermark, "limit": config.page_limit},
            )
            _reject_redirect(response)
            response.raise_for_status()
            page = response.json()
            items = page.get("items") or []
            pages += 1
            if not items:
                break
            page_stored, page_duplicates = _store_page(
                ctx, client, items, batch_id=batch_id, base_url=config.base_url
            )
            stored += page_stored
            duplicates += page_duplicates
            new_watermark = str(page.get("watermark") or "")
            if not new_watermark or new_watermark == watermark:
                errors.append("backend watermark did not advance")
                break
            watermark = new_watermark
            ctx.catalog.set_watermark("http", watermark)
        ctx.catalog.finish_batch(
            batch_id,
            status="failed" if errors else "committed",
            object_count=stored + duplicates,
            error="; ".join(errors) or None,
        )
    except Exception as exc:
        log.exception("http pull failed")
        ctx.catalog.finish_batch(batch_id, status="failed", error=str(exc))
        errors.append(str(exc))
    finally:
        if owns_client:
            client.close()
    return {
        "source": "http",
        "skipped": False,
        "batch_id": batch_id,
        "stored": stored,
        "duplicates": duplicates,
        "pages": pages,
        "errors": errors,
    }


def _store_page(
    ctx: Context, client: httpx.Client, items: list[dict], *, batch_id: str, base_url: str
) -> tuple[int, int]:
    stored = 0
    duplicates = 0
    metadata: dict[str, list[dict]] = {}
    for item in items:
        item_type = item.get("type")
        if item_type == "metadata":
            collection = str(item.get("collection") or "documents")
            document = item.get("document")
            if not isinstance(document, dict):
                raise ValueError(f"metadata item in {collection} has no document object")
            metadata.setdefault(collection, []).append(document)
            continue
        if item_type != "object":
            raise ValueError(f"unknown item type: {item_type!r}")
        result = _store_remote_object(ctx, client, item, batch_id=batch_id, base_url=base_url)
        if result == "stored":
            stored += 1
        else:
            duplicates += 1
    for collection, documents in metadata.items():
        result = _store_metadata(ctx, collection, documents, batch_id=batch_id)
        if result == "stored":
            stored += 1
        else:
            duplicates += 1
    return stored, duplicates


def _store_metadata(ctx: Context, collection: str, documents: list[dict], *, batch_id: str) -> str:
    import json

    volume = ctx.volumes.pick(1)
    directory = volume.staging() / "_pull"
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="http-", suffix=".ndjson", dir=directory, delete=False)
    path = Path(handle.name)
    try:
        with handle:
            for document in documents:
                handle.write(json.dumps(document).encode("utf-8"))
                handle.write(b"\n")
        logical = f"{batch_id}-{collection}.ndjson"
        result = ctx.writer.store_file(
            path,
            site_id=ctx.settings.site_id,
            session_id="",
            logical_path=logical,
            kind="metadata",
            content_type="application/x-ndjson",
            batch_id=batch_id,
            origin="http",
            collection=collection,
            volume=volume,
            move=True,
        )
        return result.status
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _store_remote_object(
    ctx: Context, client: httpx.Client, item: dict, *, batch_id: str, base_url: str
) -> str:
    download_url = str(item.get("download_url") or "")
    if not download_url:
        raise ValueError(f"object {item.get('logical_path')!r} has no download_url")
    expected = str(item.get("sha256") or "").lower() or None
    if expected is not None and not is_sha256(expected):
        raise ValueError(f"object {item.get('logical_path')!r} has an invalid sha256")
    size = int(item.get("size_bytes") or 1)
    volume = ctx.volumes.pick(size)
    directory = volume.staging() / "_pull"
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="httpobj-", dir=directory, delete=False)
    path = Path(handle.name)
    handle.close()
    absolute = download_url.startswith(("http://", "https://"))
    try:
        if absolute:
            assert_download_url(download_url)
            with httpx.stream(
                "GET",
                download_url,
                timeout=ctx.settings.pull.http.timeout_seconds,
                follow_redirects=False,
            ) as response:
                _reject_redirect(response)
                response.raise_for_status()
                _write_response(response, path)
        else:
            with client.stream("GET", join_download_url(base_url, download_url)) as response:
                _reject_redirect(response)
                response.raise_for_status()
                _write_response(response, path)
        captured = _parse_time(item.get("captured_at"))
        result = ctx.writer.store_file(
            path,
            site_id=ctx.settings.site_id,
            session_id=str(item.get("session_id") or ""),
            logical_path=str(item["logical_path"]),
            kind=item.get("kind"),
            content_type=item.get("content_type"),
            batch_id=batch_id,
            origin="http",
            expected_sha256=expected,
            captured_at=captured,
            source_locator=download_url,
            volume=volume,
            move=True,
        )
        return result.status
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _changes_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/changes"


def assert_download_url(url: str) -> None:
    """Allow an absolute download URL that does not carry credentials or another scheme.

    Redirects are not followed. A presigned link stays on the host it names, and
    the archive API key is never attached to that request.
    """

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"download URL must be http or https: {url}")
    if parsed.username or parsed.password:
        raise ValueError("download URL must not include credentials")


def _reject_redirect(response: httpx.Response) -> None:
    if 300 <= response.status_code < 400:
        raise ValueError(f"refusing to follow a redirect ({response.status_code})")


def join_download_url(base_url: str, download_url: str) -> str:
    """Resolve a download URL. Exposed for tests and for the README contract."""

    if download_url.startswith(("http://", "https://")):
        return download_url
    return urljoin(base_url.rstrip("/") + "/", download_url.lstrip("/"))


def _write_response(response: httpx.Response, path: Path) -> None:
    with path.open("wb") as handle:
        for block in response.iter_bytes():
            handle.write(block)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=utcnow().tzinfo)
