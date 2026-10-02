"""Connection checks invoked from the settings console."""

from __future__ import annotations

from pathlib import Path

import httpx

from nilo_datalake.config import Settings
from nilo_datalake.errors import ConfigError
from nilo_datalake.sources.http_api import assert_download_url


def probe_ingest(settings: Settings) -> str:
    if not settings.ingest.enabled:
        raise ConfigError("ingest is disabled in these settings")
    base = f"http://127.0.0.1:{settings.ingest.port}"
    health = httpx.get(f"{base}/v1/health", timeout=8)
    if health.status_code != 200:
        raise ConfigError(f"/v1/health returned {health.status_code}")
    if not settings.ingest.api_key and not settings.ingest.allow_insecure_no_auth:
        return "Health OK. Set an ingest API key to test authenticated /v1/status."
    headers = {}
    if settings.ingest.api_key:
        headers["Authorization"] = f"Bearer {settings.ingest.api_key}"
    status = httpx.get(f"{base}/v1/status", headers=headers, timeout=8)
    if status.status_code == 401:
        raise ConfigError("/v1/status rejected the API key")
    if status.status_code >= 400:
        raise ConfigError(f"/v1/status returned {status.status_code}")
    body = status.json()
    return (
        f"Ingest API is up on port {settings.ingest.port}. "
        f"Catalog has {body.get('objects', '?')} object(s)."
    )


def probe_direct(settings: Settings) -> str:
    lines: list[str] = []
    net = Path("/sys/class/net")
    if net.is_dir():
        for iface in sorted(item for item in net.iterdir() if item.is_dir()):
            if iface.name == "lo":
                continue
            oper_path = iface / "operstate"
            oper = oper_path.read_text(encoding="utf-8").strip() if oper_path.is_file() else "unknown"
            if oper == "up":
                lines.append(f"link {iface.name}: up")
    if not lines:
        lines.append("no active network interfaces detected (except loopback)")

    for volume in settings.storage.volumes:
        if not volume.enabled:
            continue
        root = Path(volume.root)
        inbox = root / "inbox"
        if inbox.is_dir():
            pending = sum(1 for path in inbox.iterdir() if path.is_dir())
            lines.append(f"inbox {inbox}: ready ({pending} drop folder(s))")
        else:
            lines.append(f"inbox {inbox}: missing (created when the first rsync drop arrives)")

    if settings.pull.ssh.sources:
        for source in settings.pull.ssh.sources:
            lines.append(f"rsync/SSH source {source.name or '(unnamed)'}: {source.remote}")
    else:
        lines.append("no rsync/SSH sources configured (add one for a linked MiniPC)")

    lines.append("SSH/rsync pull stays enabled for direct Ethernet links.")
    return "; ".join(lines)


def probe_http_pull(settings: Settings) -> str:
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
    return f"Remote archive API returned {response.status_code}"
