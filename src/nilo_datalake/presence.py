"""Report this device's public IP to a central registry on a schedule."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

import httpx

from nilo_datalake.config import Settings
from nilo_datalake.models import isoformat, utcnow

log = logging.getLogger(__name__)

_PUBLIC_IP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)

_lock = threading.Lock()
_last: dict[str, Any] | None = None


def get_last_report() -> dict[str, Any] | None:
    with _lock:
        return dict(_last) if _last is not None else None


def _store(result: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        global _last
        _last = result
    return result


def fetch_public_ip(*, timeout: float = 10.0) -> str:
    errors: list[str] = []
    for url in _PUBLIC_IP_URLS:
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code // 100 != 2:
                errors.append(f"{url}: HTTP {response.status_code}")
                continue
            text = (response.text or "").strip()
            if text and len(text) <= 64 and " " not in text:
                return text
            errors.append(f"{url}: unexpected body")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("could not resolve public IP: " + "; ".join(errors))


def report_presence(settings: Settings) -> dict[str, Any]:
    """POST a heartbeat to ``presence.registry_url``. Updates :func:`get_last_report`."""

    cfg = settings.presence
    started = isoformat(utcnow())
    if not cfg.enabled:
        return _store({"ok": False, "skipped": True, "detail": "presence reporting is disabled", "at": started})
    url = (cfg.registry_url or "").strip()
    if not url:
        return _store({"ok": False, "detail": "registry URL is empty", "at": started})
    if cfg.interval_seconds <= 0:
        return _store({"ok": False, "detail": "interval must be positive", "at": started})

    try:
        public_ip = fetch_public_ip(timeout=min(cfg.timeout_seconds, 30.0))
    except Exception as exc:
        result = {"ok": False, "detail": str(exc), "at": started}
        log.warning("presence: public IP lookup failed: %s", exc)
        return _store(result)

    hostname = socket.gethostname()
    body = {
        "site_id": settings.site_id,
        "device_kind": (cfg.device_kind or "datalake").strip() or "datalake",
        "public_ip": public_ip,
        "hostname": hostname,
        "reported_at": started,
    }
    headers: dict[str, str] = {"content-type": "application/json", "accept": "application/json"}
    if cfg.api_key.strip():
        headers["authorization"] = f"Bearer {cfg.api_key.strip()}"

    try:
        response = httpx.post(url, json=body, headers=headers, timeout=cfg.timeout_seconds)
        if response.status_code // 100 != 2:
            raise RuntimeError(f"registry returned HTTP {response.status_code}")
    except Exception as exc:
        result = {
            "ok": False,
            "detail": str(exc),
            "public_ip": public_ip,
            "registry_url": url,
            "at": started,
        }
        log.warning("presence: registry POST failed: %s", exc)
        return _store(result)

    detail = response.text.strip()[:500] if response.text else f"HTTP {response.status_code}"
    result = {
        "ok": True,
        "detail": detail or f"HTTP {response.status_code}",
        "public_ip": public_ip,
        "registry_url": url,
        "status_code": response.status_code,
        "at": started,
    }
    log.info("presence: reported %s for site_id=%s to %s", public_ip, settings.site_id, url)
    return _store(result)
