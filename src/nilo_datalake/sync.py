"""One-shot sync used by the CLI and by the process scheduler."""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from nilo_datalake.inbox import promote_inbox
from nilo_datalake.models import isoformat, utcnow
from nilo_datalake.service import Context
from nilo_datalake.sources.http_api import pull_http
from nilo_datalake.sources.minio_source import pull_minio
from nilo_datalake.sources.mongo import pull_mongo
from nilo_datalake.sources.ssh_pull import pull_ssh

log = logging.getLogger(__name__)

_lock = threading.Lock()


def run_sync(ctx: Context) -> dict:
    """Pull every enabled backend source, then promote finished drop folders.

    A run that starts while another run is still going returns immediately.
    Pulling a multi-terabyte day must not overlap with the next one.
    """

    if not _lock.acquire(blocking=False):
        log.warning("sync already running; skipping this trigger")
        return {"skipped": True, "reason": "sync already running", "sources": [], "errors": []}
    started = isoformat(utcnow())
    sources: list[dict] = []
    errors: list[str] = []
    try:
        for pull in (pull_mongo, pull_minio, pull_http, pull_ssh):
            try:
                result = pull(ctx)
            except Exception as exc:
                log.exception("%s failed", pull.__name__)
                result = {"source": pull.__name__, "error": str(exc)}
                errors.append(f"{pull.__name__}: {exc}")
            sources.append(result)
            for item in result.get("errors") or []:
                errors.append(f"{result.get('source', pull.__name__)}: {item}")
            if result.get("error"):
                errors.append(f"{result.get('source', pull.__name__)}: {result['error']}")
        if ctx.settings.inbox.enabled:
            try:
                inbox_result = promote_inbox(ctx)
            except Exception as exc:
                log.exception("inbox promotion failed")
                inbox_result = {"source": "inbox", "error": str(exc)}
                errors.append(f"inbox: {exc}")
            sources.append(inbox_result)
            errors.extend(f"inbox: {item}" for item in inbox_result.get("errors") or [])
        return {
            "skipped": False,
            "started_at": started,
            "finished_at": isoformat(utcnow()),
            "sources": sources,
            "errors": errors,
        }
    finally:
        _lock.release()


def start_scheduler(ctx: Context) -> BackgroundScheduler | None:
    """Start in-process jobs. Returns ``None`` when nothing is scheduled."""

    scheduler = BackgroundScheduler(timezone=ctx.settings.pull.timezone)
    if ctx.settings.pull.enabled and ctx.settings.pull.schedule:
        scheduler.add_job(
            lambda: run_sync(ctx),
            CronTrigger.from_crontab(ctx.settings.pull.schedule, timezone=ctx.settings.pull.timezone),
            id="pull",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    if ctx.settings.inbox.enabled and ctx.settings.inbox.poll_seconds > 0:
        scheduler.add_job(
            lambda: promote_inbox(ctx),
            "interval",
            seconds=ctx.settings.inbox.poll_seconds,
            id="inbox",
            max_instances=1,
            coalesce=True,
        )
    if not scheduler.get_jobs():
        return None
    scheduler.start()
    log.info("scheduler started with %s job(s)", len(scheduler.get_jobs()))
    return scheduler
