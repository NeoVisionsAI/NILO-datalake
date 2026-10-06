"""Try archive ingest HTTP endpoints from the console (Swagger-style checks)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError

_KNOWN = frozenset({"health", "status", "open_batch"})


def try_archive_action(action: str, settings: Settings, *, app: Any = None) -> tuple[bool, str]:
    action = action.strip().lower()
    if action not in _KNOWN:
        raise ConfigError(f"unknown try action: {action}")

    if app is not None:
        from starlette.testclient import TestClient

        client = TestClient(app)
        try:
            return _run_archive_requests(client, action, settings)
        finally:
            client.close()

    with httpx.Client(base_url=f"http://127.0.0.1:{settings.ingest.port}") as client:
        return _run_archive_requests(client, action, settings)


def _run_archive_requests(client, action: str, settings: Settings) -> tuple[bool, str]:
    if action == "health":
        response = client.get("/v1/health", timeout=8)
        body = response.text[:500]
        if response.status_code != 200:
            return False, f"GET /v1/health → HTTP {response.status_code}: {body}"
        return True, f"GET /v1/health → HTTP 200 {body}"

    if not settings.ingest.enabled:
        raise ConfigError("ingest is disabled; enable it in Data ingress (option B)")

    headers = _auth_headers(settings)

    if action == "status":
        response = client.get("/v1/status", headers=headers, timeout=8)
        if response.status_code == 401:
            return False, "GET /v1/status → 401 (check the ingest API key)"
        if response.status_code >= 400:
            return False, f"GET /v1/status → HTTP {response.status_code}"
        data = response.json()
        return True, (
            f"GET /v1/status → OK. site_id={data.get('site_id')}, "
            f"objects={data.get('objects')}, bytes={data.get('total_bytes')}"
        )

    response = client.post(
        "/v1/batches",
        headers={**headers, "content-type": "application/json"},
        content=json.dumps({"site_id": settings.site_id}),
        timeout=15,
    )
    if response.status_code == 401:
        return False, "POST /v1/batches → 401 (check the ingest API key)"
    if response.status_code >= 400:
        return False, f"POST /v1/batches → HTTP {response.status_code}: {response.text[:300]}"
    data = response.json()
    batch_id = data.get("batch_id", "?")
    return True, f"POST /v1/batches → open batch {batch_id} on volume {data.get('volume_id')}"


def _auth_headers(settings: Settings) -> dict[str, str]:
    if settings.ingest.allow_insecure_no_auth and not settings.ingest.api_key:
        return {}
    if not settings.ingest.api_key:
        raise ConfigError("set an ingest API key before calling authenticated endpoints")
    return {"Authorization": f"Bearer {settings.ingest.api_key}"}
