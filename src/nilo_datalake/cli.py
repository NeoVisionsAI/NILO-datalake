"""Command line entry point for the NAS service and the MiniPC agent."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import uvicorn

from nilo_datalake.api import create_app, status_payload
from nilo_datalake.config import load_settings, redact
from nilo_datalake.edge import run_forever, run_once
from nilo_datalake.errors import ConfigError
from nilo_datalake.inbox import gc_incomplete, promote_inbox
from nilo_datalake.logsetup import configure_logging
from nilo_datalake.service import build_context
from nilo_datalake.sync import run_sync

app = typer.Typer(help="Archive NILO clinical capture data onto a NAS.", no_args_is_help=True)
edge_app = typer.Typer(help="Run the MiniPC shipping agent.", no_args_is_help=True)
app.add_typer(edge_app, name="edge")


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", "-c", help="YAML settings file."),
) -> None:
    ctx.obj = load_settings(config)


@app.command()
def serve(ctx: typer.Context) -> None:
    """Run the ingest API and the in-process schedule."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    if not settings.ingest.enabled and not settings.pull.enabled and not settings.inbox.enabled:
        raise typer.BadParameter("enable ingest, pull, or inbox before running serve")
    context = build_context(settings)
    application = create_app(context)
    uvicorn.run(application, host=settings.ingest.host, port=settings.ingest.port, workers=1)


@app.command()
def sync(ctx: typer.Context) -> None:
    """Pull enabled backend sources once and promote finished drop folders."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    context = build_context(settings)
    report = run_sync(context)
    typer.echo(_format_report(report))
    if report.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def promote(ctx: typer.Context) -> None:
    """Promote drop folders that already contain a READY marker."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    context = build_context(settings)
    report = promote_inbox(context)
    typer.echo(json.dumps(report, indent=2))
    if report.get("failed"):
        raise typer.Exit(code=1)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show volume free space and the latest batches."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    context = build_context(settings)
    typer.echo(json.dumps(status_payload(context), indent=2))


@app.command("gc")
def gc(
    ctx: typer.Context,
    older_than_hours: int | None = typer.Option(
        None, help="Override inbox.abandon_after_hours for this run."
    ),
) -> None:
    """Delete unfinished uploads older than the configured limit."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    context = build_context(settings)
    hours = settings.inbox.abandon_after_hours if older_than_hours is None else older_than_hours
    report = gc_incomplete(context, older_than_hours=hours)
    typer.echo(json.dumps(report, indent=2))


@app.command("rebuild-catalog")
def rebuild_catalog(ctx: typer.Context) -> None:
    """Rebuild the SQLite object index from the append-only journal."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    context = build_context(settings)
    count = context.catalog.rebuild_from_journal()
    typer.echo(json.dumps({"replayed": count, "objects": context.catalog.object_count()}))


@app.command("check-config")
def check_config(ctx: typer.Context) -> None:
    """Print the resolved configuration with secrets removed."""

    typer.echo(json.dumps(redact(ctx.obj), indent=2))


@edge_app.command("once")
def edge_once(ctx: typer.Context) -> None:
    """Ship every completed session in the spool, then exit."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    try:
        report = run_once(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(report, indent=2))
    if report["errors"]:
        raise typer.Exit(code=1)


@edge_app.command("run")
def edge_run(ctx: typer.Context) -> None:
    """Keep shipping completed sessions on the configured interval."""

    settings = ctx.obj
    configure_logging(settings.log_level)
    try:
        run_forever(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _format_report(report: dict) -> str:
    if report.get("skipped"):
        return f"sync skipped: {report.get('reason')}"
    lines = [f"sync {report['started_at']} -> {report['finished_at']}"]
    for source in report["sources"]:
        name = source.get("source", "unknown")
        if source.get("skipped"):
            lines.append(f"  {name}: skipped")
            continue
        stored = source.get("stored", 0)
        duplicates = source.get("duplicates", 0)
        failed = source.get("failed", 0)
        lines.append(f"  {name}: stored={stored} duplicates={duplicates} failed={failed}")
    if report.get("errors"):
        lines.append("errors:")
        lines.extend(f"  - {item}" for item in report["errors"])
    return "\n".join(lines)
