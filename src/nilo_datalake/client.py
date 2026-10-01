"""Client for the datalake ingest API.

The edge agent uses this. A Python backend can use the same class to push a
finished session instead of waiting for the datalake to pull.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from nilo_datalake.checksums import CHUNK_BYTES, sha256_file
from nilo_datalake.models import isoformat


class DatalakeClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 600,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def open_batch(self, site_id: str, source: str) -> str:
        response = self._check(self._http.post("/v1/batches", json={"site_id": site_id, "source": source}))
        return str(response.json()["batch_id"])

    def upload_file(
        self,
        batch_id: str,
        logical_path: str,
        path: Path,
        *,
        session_id: str = "",
        kind: str | None = None,
        content_type: str | None = None,
        captured_at: datetime | None = None,
    ) -> dict:
        size = path.stat().st_size
        digest = sha256_file(path)
        headers = {
            "X-Checksum-Sha256": digest,
            "X-Session-Id": session_id,
            "Content-Length": str(size),
        }
        if kind:
            headers["X-Kind"] = kind
        if content_type:
            headers["Content-Type"] = content_type
        if captured_at is not None:
            headers["X-Captured-At"] = isoformat(captured_at)
        response = self._check(
            self._http.put(
                f"/v1/batches/{batch_id}/objects",
                params={"logical_path": logical_path},
                content=_chunks(path),
                headers=headers,
            )
        )
        return response.json()

    def commit(self, batch_id: str) -> dict:
        response = self._check(self._http.post(f"/v1/batches/{batch_id}/commit"))
        return response.json()

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        if not response.is_error:
            return response
        detail = response.text.strip().replace("\n", " ")[:300]
        raise httpx.HTTPStatusError(
            f"{response.status_code} {detail}",
            request=response.request,
            response=response,
        )

    def __enter__(self) -> DatalakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _chunks(path: Path):
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK_BYTES)
            if not block:
                break
            yield block
