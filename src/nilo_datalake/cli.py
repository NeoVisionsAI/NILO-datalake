"""Command line entry point for the NAS service and the MiniPC agent."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import uvicorn

from nilo_datalake.api import create_app, status_payload
from nilo_datalake.config import current_config_path, open_runtime_settings, redact
from nilo_datalake.edge import run_forever, run_once
from nilo_datalake.errors import ConfigError
from nilo_datalake.inbox import gc_incomplete, promote_inbox
from nilo_datalake.service import build_context
from nilo_datalake.sync import run_sync
from nilo_datalake.tracing import configure_tracing, recent_failures, trace_directory, uvicorn_log_config

app = typer.Typer(help="Archive NILO clinical capture data onto a NAS.", no_args_is_help=True)
edge_app = typer.Typer(help="Run the MiniPC shipping agent.", no_args_is_help=True)
app.add_typer(edge_app, name="edge")


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", "-c", help="YAML settings file."),
) -> None:
    ctx.obj = open_runtime_settings(config)


@app.command()
def serve(ctx: typer.Context) -> None:
    """Run the ingest API and the in-process schedule."""

    settings = ctx.obj
    _activate_tracing(settings)
    if not any((settings.ingest.enabled, settings.pull.enabled, settings.inbox.enabled, settings.console.enabled)):
        raise typer.BadParameter("enable the console, ingest, pull, or inbox before running serve")
    context = build_context(settings)
    application = create_app(context, config_path=current_config_path())
    uvicorn.run(
        application,
        host=settings.ingest.host,
        port=settings.ingest.port,
        workers=1,
        log_config=uvicorn_log_config(),
    )


@app.command()
def sync(ctx: typer.Context) -> None:
    """Pull enabled backend sources once and promote finished drop folders."""

    settings = ctx.obj
    _activate_tracing(settings)
    context = build_context(settings)
    report = run_sync(context)
    typer.echo(_format_report(report))
    if report.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def promote(ctx: typer.Context) -> None:
    """Promote drop folders that already contain a READY marker."""

    settings = ctx.obj
    _activate_tracing(settings)
    context = build_context(settings)
    report = promote_inbox(context)
    typer.echo(json.dumps(report, indent=2))
    if report.get("failed"):
        raise typer.Exit(code=1)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show volume free space and the latest batches."""

    settings = ctx.obj
    _activate_tracing(settings)
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
    _activate_tracing(settings)
    context = build_context(settings)
    hours = settings.inbox.abandon_after_hours if older_than_hours is None else older_than_hours
    report = gc_incomplete(context, older_than_hours=hours)
    typer.echo(json.dumps(report, indent=2))


@app.command("rebuild-catalog")
def rebuild_catalog(ctx: typer.Context) -> None:
    """Rebuild the SQLite object index from the append-only journal."""

    settings = ctx.obj
    _activate_tracing(settings)
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
    _activate_tracing(settings)
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
    _activate_tracing(settings)
    try:
        run_forever(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command("traces")
def traces(
    ctx: typer.Context,
    limit: int = typer.Option(20, help="How many recent failures to print."),
) -> None:
    """Show recent failures with the file, the line, and the stack."""

    settings = ctx.obj
    directory = trace_directory(settings.storage.catalog_path, settings.trace.directory)
    rows = recent_failures(directory, limit=limit)
    if not rows:
        typer.echo(f"no failures in {directory / 'failures.jsonl'}")
        return
    for row in rows:
        typer.echo(
            f"{row.get('ts')} trace={row.get('trace_id')} "
            f"{row.get('component')}.{row.get('operation')} {row.get('message')}"
        )
        typer.echo(f"  {row.get('location')} {row.get('function')}")
        stack = row.get("stack")
        if stack:
            typer.echo(stack)
        typer.echo("")


def _activate_tracing(settings) -> None:
    configure_tracing(
        settings.log_level,
        trace_directory(settings.storage.catalog_path, settings.trace.directory),
    )


def _format_report(report: dict) -> str:
    if report.get("skipped"):
        return f"sync skipped: {report.get('reason')} trace={report.get('trace_id', '-')}"
    lines = [
        f"sync {report['started_at']} -> {report['finished_at']} trace={report.get('trace_id', '-')}"
    ]
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
