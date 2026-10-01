from __future__ import annotations

import logging

import pytest

from nilo_datalake.tracing import begin_trace, configure_tracing, end_trace, recent_failures, span


@pytest.fixture
def traced(tmp_path):
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    configure_tracing("INFO", tmp_path)
    try:
        yield tmp_path
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)


def test_failure_trace_records_where_it_broke(traced) -> None:
    trace_id, token = begin_trace()
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            with span("mongo", "export", collection="sessions"):
                raise RuntimeError("disk full")
    finally:
        end_trace(token)

    rows = recent_failures(traced, limit=5)
    assert rows
    failure = rows[-1]
    assert failure["component"] == "mongo"
    assert failure["operation"] == "export"
    assert failure["trace_id"] == trace_id
    assert "disk full" in failure["exception"]
    assert "RuntimeError" in failure["stack"]
    assert "test_tracing.py" in failure["stack"]
    assert "collection=sessions" in failure["message"]


def test_span_does_not_record_secrets(traced) -> None:
    with span("api", "upload", session_id="sess-1", api_key="super-secret"):
        pass
    text = (traced / "datalake.jsonl").read_text(encoding="utf-8")
    assert "sess-1" in text
    assert "super-secret" not in text
    assert recent_failures(traced) == []
