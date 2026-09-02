"""Typer command-line implementation for the local Vidigami archive."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from . import __version__
from .auth import AuthenticationError, ReauthenticationRequired
from .config import AppConfig, load_config
from .graphql import GraphQLError, GraphQLHTTPError
from .reporting import rows_from_snapshot, write_csv, write_json
from .service import (
    backfill_local_metadata,
    criteria_for,
    graph_client,
    oauth_client,
    open_store,
    run_sync,
    verify_downloads,
)

app = typer.Typer(help="Privacy-first local Vidigami downloader.", invoke_without_command=True)
auth_app = typer.Typer(help="Manage local authentication.")
app.add_typer(auth_app, name="auth")
ConfigOption = Annotated[Path | None, typer.Option("--config", path_type=Path)]


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", help="Show the installed version.", is_eager=True
    ),
) -> None:
    """Run a privacy-first, local Vidigami workflow."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@auth_app.command("login")
def auth_login(
    config_path: ConfigOption = None,
    browser: bool = typer.Option(
        False,
        "--browser",
        help="Use the system-browser loopback flow instead of direct HTTP login.",
    ),
) -> None:
    """Prompt for credentials and save an OAuth session in the OS keychain."""
    config = _load(config_path)
    try:
        client = oauth_client(config)
        if browser:
            client.login()
        else:
            client.direct_login()
    except (AuthenticationError, OSError) as exc:
        _error(_safe_error(exc, "Authentication failed"))
    typer.echo("Authentication succeeded; the token is stored in the OS keychain.")


@auth_app.command("status")
def auth_status(config_path: ConfigOption = None) -> None:
    """Show whether a local OAuth session is present, without printing secrets."""
    config = _load(config_path)
    try:
        tokens = oauth_client(config).status()
    except AuthenticationError as exc:
        _error(_safe_error(exc, "Could not read authentication status"))
    typer.echo(json.dumps({"authenticated": tokens is not None}, sort_keys=True))


@auth_app.command("logout")
def auth_logout(config_path: ConfigOption = None) -> None:
    """Remove the locally stored OAuth session from the OS keychain."""
    config = _load(config_path)
    try:
        oauth_client(config).logout()
    except AuthenticationError as exc:
        _error(_safe_error(exc, "Could not remove authentication"))
    typer.echo("Local authentication removed.")


@app.command()
def relationships(config_path: ConfigOption = None) -> None:
    """List accessible relationship IDs, never relationship names or emails."""
    config = _load(config_path)
    try:
        viewer = graph_client(config).get_viewer(config.vidigami.space_id)
    except Exception as exc:
        _error(_safe_error(exc, "Could not enumerate Vidigami relationships"))
    identifiers = sorted(
        identifier
        for value in viewer.relationships
        if (identifier := _relationship_id(value)) is not None
    )
    typer.echo(
        json.dumps({"viewer_id": viewer.id, "relationship_ids": identifiers}, sort_keys=True)
    )


@app.command()
def pages(config_path: ConfigOption = None) -> None:
    """List accessible page IDs and names."""
    config = _load(config_path)
    try:
        page_results = graph_client(config).get_pages(config.vidigami.space_id or None)
    except Exception as exc:
        _error(_safe_error(exc, "Could not enumerate Vidigami pages"))
    typer.echo(
        json.dumps(
            {
                "pages": [
                    {"id": page.id, "name": page.name}
                    for page in page_results
                ]
            },
            sort_keys=True,
        )
    )


@app.command()
def doctor(config_path: ConfigOption = None) -> None:
    """Check local configuration and dependencies without contacting Vidigami."""
    config = _load(config_path)
    problems: list[str] = []
    organization_is_example = (
        not config.vidigami.organization_id
        or config.vidigami.organization_id.startswith("org|example")
    )
    if organization_is_example and not config.vidigami.organization_identifier:
        problems.append(
            "set vidigami.organization_id or organization_identifier in ignored config.toml"
        )
    if not config.vidigami.space_id or config.vidigami.space_id.startswith("space|example"):
        problems.append("set vidigami.space_id in ignored config.toml")
    if not config.vidigami.page_ids and not config.vidigami.tagged_user_ids:
        problems.append("configure at least one page_ids or tagged_user_ids selector")
    if config.auth.token_auth_method == "client_secret_basic" and not config.auth.client_secret:
        problems.append("set auth.client_secret in ignored config.toml")
    try:
        import keyring  # noqa: F401
    except ImportError:
        problems.append("install project dependencies (keyring is missing)")
    if problems:
        typer.echo(json.dumps({"ok": False, "problems": problems}, sort_keys=True))
        raise typer.Exit(1)
    typer.echo(json.dumps({"ok": True}, sort_keys=True))


@app.command()
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Enumerate without downloading.")
    ] = False,
    config_path: ConfigOption = None,
) -> None:
    """Reconcile membership IDs and download selected media when not dry-running."""
    config = _load(config_path)
    try:
        summary = run_sync(config, dry_run=dry_run)
    except Exception as exc:
        _error(_safe_error(exc, "Sync failed"))
    payload: dict[str, Any] = {
        "run_id": summary.result.run_id,
        "dry_run": summary.result.dry_run,
        "page_media_count": summary.result.page_media_count,
        "tagged_media_count": summary.result.tagged_media_count,
        "candidate_count": summary.result.candidate_count,
        "hydrated_count": summary.result.hydrated_count,
        "selected_count": summary.result.selected_count,
    }
    if not dry_run:
        payload["downloads"] = {
            "attempted": summary.downloads.attempted,
            "completed": summary.downloads.completed,
            "reused": summary.downloads.reused,
            "failed": summary.downloads.failed,
        }
    typer.echo(json.dumps(payload, sort_keys=True))
    if not dry_run and summary.downloads.failed:
        raise typer.Exit(1)


@app.command()
def status(config_path: ConfigOption = None) -> None:
    """Show local sync and download counts without media names or paths."""
    config = _load(config_path)
    store = open_store(config)
    try:
        media = _count(store.connection, "SELECT COUNT(*) FROM media")
        containers = _count(
            store.connection, "SELECT COUNT(*) FROM media_containers WHERE removed_at IS NULL"
        )
        tags = _count(store.connection, "SELECT COUNT(*) FROM media_tags WHERE removed_at IS NULL")
        downloads = _count(
            store.connection, "SELECT COUNT(*) FROM downloads WHERE status='complete'"
        )
        last_run = store.connection.execute(
            "SELECT status FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        store.close()
    typer.echo(
        json.dumps(
            {
                "media_count": media,
                "active_container_memberships": containers,
                "active_tag_memberships": tags,
                "completed_downloads": downloads,
                "last_sync_status": last_run[0] if last_run else None,
            },
            sort_keys=True,
        )
    )


@app.command()
def report(
    config_path: ConfigOption = None,
    format: Annotated[str, typer.Option("--format", help="json, csv, or both.")] = "json",
    output: Annotated[Path | None, typer.Option("--output", path_type=Path)] = None,
) -> None:
    """Write a derived report containing canonical page/container/tag IDs."""
    if format not in {"json", "csv", "both"}:
        _error("format must be json, csv, or both")
    config = _load(config_path)
    store = open_store(config)
    try:
        rows = rows_from_snapshot(store.snapshot(), criteria_for(config))
    finally:
        store.close()
    destination = output or config.storage.reports_directory / (
        f"media.{format if format != 'both' else 'json'}"
    )
    written: list[str] = []
    if format in {"json", "both"}:
        written.append(
            str(
                write_json(
                    destination if format == "json" else destination.with_suffix(".json"), rows
                )
            )
        )
    if format in {"csv", "both"}:
        written.append(
            str(
                write_csv(destination if format == "csv" else destination.with_suffix(".csv"), rows)
            )
        )
    typer.echo(json.dumps({"row_count": len(rows), "files": written}, sort_keys=True))


@app.command()
def verify(config_path: ConfigOption = None) -> None:
    """Verify downloaded files against their recorded SHA-256 checksums."""
    config = _load(config_path)
    store = open_store(config)
    try:
        checked, valid, invalid = verify_downloads(store)
    finally:
        store.close()
    typer.echo(json.dumps({"checked": checked, "valid": valid, "invalid": invalid}, sort_keys=True))
    if invalid:
        raise typer.Exit(1)


@app.command("metadata")
def metadata(config_path: ConfigOption = None) -> None:
    """Backfill technical metadata from completed local originals only."""
    config = _load(config_path)
    store = open_store(config)
    try:
        summary = backfill_local_metadata(store)
    finally:
        store.close()
    typer.echo(
        json.dumps(
            {
                "inspected": summary.inspected,
                "updated": summary.updated,
                "missing": summary.missing,
                "failed": summary.failed,
            },
            sort_keys=True,
        )
    )
    if summary.failed:
        raise typer.Exit(1)


def _load(path: Path | None) -> AppConfig:
    try:
        return load_config(path)
    except (OSError, ValueError, TypeError) as exc:
        _error(_safe_error(exc, "Could not load configuration"))


def _count(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return int(row[0]) if row else 0


def _relationship_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    node = value.get("node")
    if isinstance(node, dict) and node.get("id"):
        return str(node["id"])
    return str(value["id"]) if value.get("id") else None


def _safe_error(exc: Exception, fallback: str) -> str:
    if isinstance(
        exc, (AuthenticationError, ReauthenticationRequired, GraphQLHTTPError, GraphQLError)
    ):
        return str(exc)
    return fallback


def _error(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


__all__ = ["app"]
