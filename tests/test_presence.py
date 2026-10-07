from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from nilo_datalake.config import PresenceConfig, Settings
from nilo_datalake.presence import fetch_public_ip, get_last_report, report_presence


def test_fetch_public_ip_uses_first_working_provider() -> None:
    response = httpx.Response(200, text="203.0.113.10\n")
    with patch("nilo_datalake.presence.httpx.get", return_value=response) as get:
        assert fetch_public_ip(timeout=5) == "203.0.113.10"
    get.assert_called_once()


def test_report_presence_posts_json() -> None:
    settings = Settings(
        site_id="hospital-a",
        presence=PresenceConfig(
            enabled=True,
            registry_url="https://registry.test/v1/presence",
            interval_seconds=60,
            device_kind="datalake",
            api_key="secret",
        ),
    )
    ip_response = httpx.Response(200, text="198.51.100.7")
    post_response = httpx.Response(201, text='{"ok":true}')

    def fake_get(url, **kwargs):
        return ip_response

    def fake_post(url, **kwargs):
        assert url == "https://registry.test/v1/presence"
        assert kwargs["json"]["site_id"] == "hospital-a"
        assert kwargs["json"]["public_ip"] == "198.51.100.7"
        assert kwargs["headers"]["authorization"] == "Bearer secret"
        return post_response

    with patch("nilo_datalake.presence.httpx.get", side_effect=fake_get):
        with patch("nilo_datalake.presence.httpx.post", side_effect=fake_post):
            result = report_presence(settings)
    assert result["ok"] is True
    assert result["public_ip"] == "198.51.100.7"
    last = get_last_report()
    assert last is not None
    assert last["ok"] is True


def test_report_presence_skipped_when_disabled() -> None:
    settings = Settings(presence=PresenceConfig(enabled=False))
    result = report_presence(settings)
    assert result["skipped"] is True
