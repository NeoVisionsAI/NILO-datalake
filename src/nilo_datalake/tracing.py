"""Traces for every sync, upload, and edge shipment.

A trace id ties the steps of one run together. Each step is a span. When a
step fails, the stack, the module, and the line are appended to
``failures.jsonl``. Console logs and ``datalake.jsonl`` carry the same id, so
a line in the failure file can be followed through the rest of the run.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path

_trace_id: ContextVar[str] = ContextVar("nilo_trace_id", default="-")
_component: ContextVar[str] = ContextVar("nilo_component", default="-")
_operation: ContextVar[str] = ContextVar("nilo_operation", default="-")

_SENSITIVE = ("key", "secret", "password", "token", "uri", "authorization")

log = logging.getLogger("nilo_datalake.trace")


class TraceFilter(logging.Filter):
    """Copy the current trace id and span onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        record.component = _component.get()
        record.operation = _operation.get()
        return True


class JsonLineHandler(logging.Handler):
    """Append one JSON object per log record. Failures include the stack."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        self._lock = threading.Lock()
        self.nilo_trace = True

    def emit(self, record: logging.LogRecord) -> None:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", "-"),
            "component": getattr(record, "component", "-"),
            "operation": getattr(record, "operation", "-"),
            "logger": record.name,
            "message": record.getMessage(),
            "location": f"{record.pathname}:{record.lineno}",
            "function": record.funcName,
        }
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc, _tb = record.exc_info
            payload["exception"] = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            payload["stack"] = "".join(traceback.format_exception(exc_type, exc, record.exc_info[2])).rstrip()
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            self.handleError(record)


def current_trace_id() -> str:
    return _trace_id.get()


def begin_trace(trace_id: str | None = None) -> tuple[str, Token]:
    """Start a trace and return ``(trace_id, token)``. Reset with ``end_trace``."""

    chosen = trace_id or uuid.uuid4().hex[:16]
    token = _trace_id.set(chosen)
    return chosen, token


def end_trace(token: Token) -> None:
    _trace_id.reset(token)


def trace_directory(catalog_path: Path, configured: Path | None) -> Path:
    if configured is not None:
        return configured
    return catalog_path.parent / "traces"


def configure_tracing(level: str, directory: Path | None = None) -> None:
    """Install the console log and, when a directory is set, the JSON traces."""

    root = logging.getLogger()
    root.handlers = [handler for handler in root.handlers if not getattr(handler, "nilo_trace", False)]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler()
    console.nilo_trace = True
    console.addFilter(TraceFilter())
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s trace=%(trace_id)s %(component)s.%(operation)s "
            "%(name)s %(message)s"
        )
    )
    root.addHandler(console)

    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    everything = JsonLineHandler(directory / "datalake.jsonl")
    everything.addFilter(TraceFilter())
    root.addHandler(everything)
    failures = JsonLineHandler(directory / "failures.jsonl")
    failures.setLevel(logging.ERROR)
    failures.addFilter(TraceFilter())
    root.addHandler(failures)


def configure_logging(level: str) -> None:
    """Console tracing without a file. Commands that have a catalog use ``configure_tracing``."""

    configure_tracing(level, None)


@contextmanager
def bound(component: str, operation: str) -> Iterator[None]:
    """Set the component and operation for every log line in this block."""

    component_token = _component.set(component)
    operation_token = _operation.set(operation)
    try:
        yield
    finally:
        _component.reset(component_token)
        _operation.reset(operation_token)


@contextmanager
def span(component: str, operation: str, **fields: object) -> Iterator[None]:
    """Time one step. A failure is logged with the stack and then re-raised.

    ``HTTPException`` responses below 500 are client errors. They are logged
    as a rejection and are not written to the failure file.
    """

    started = time.perf_counter()
    detail = _format_fields(fields)
    with bound(component, operation):
        log.info("start%s", detail)
        try:
            yield
        except Exception as exc:
            elapsed = _elapsed_ms(started)
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and status < 500:
                log.info("rejected status=%s duration_ms=%s%s", status, elapsed, detail)
                raise
            log.exception("failed duration_ms=%s%s", elapsed, detail)
            raise
        else:
            log.info("ok duration_ms=%s%s", _elapsed_ms(started), detail)


def recent_failures(directory: Path, limit: int = 20) -> list[dict]:
    """Return the last failure records. The file is read from the end."""

    path = directory / "failures.jsonl"
    if not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - 1_000_000))
        raw = handle.read().decode("utf-8", errors="replace")
    rows: list[dict] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            rows.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def uvicorn_log_config() -> dict:
    """Keep uvicorn from replacing the handlers installed by ``configure_tracing``."""

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"level": "INFO", "propagate": True},
            "uvicorn.error": {"level": "INFO", "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": True},
        },
    }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _format_fields(fields: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        lowered = key.lower()
        if any(word in lowered for word in _SENSITIVE):
            continue
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    if not parts:
        return ""
    return " " + " ".join(parts)
