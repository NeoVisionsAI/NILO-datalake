from __future__ import annotations

from nilo_datalake.config import load_settings, redact


def test_environment_overrides_yaml(tmp_path, monkeypatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text(
        "\n".join(
            [
                "site_id: from-yaml",
                "ingest:",
                "  api_key: yaml-key",
                "edge:",
                "  interval_seconds: 10",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NILO_SITE_ID", "from-env")
    monkeypatch.setenv("NILO_INGEST__API_KEY", "env-key")
    settings = load_settings(config)
    assert settings.site_id == "from-env"
    assert settings.ingest.api_key == "env-key"
    assert settings.edge.interval_seconds == 10
    redacted = redact(settings)
    assert redacted["ingest"]["api_key"] == "***"
    assert redacted["site_id"] == "from-env"
