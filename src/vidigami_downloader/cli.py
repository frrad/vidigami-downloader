"""Typer command-line interface scaffold."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import load_config

app = typer.Typer(help="Privacy-first local Vidigami downloader.")
auth_app = typer.Typer(help="Manage local authentication.")
app.add_typer(auth_app, name="auth")


def _not_implemented(command: str) -> None:
    typer.echo(f"{command}: not implemented yet; this scaffold performs no network access.")


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="Show the installed version."),
) -> None:
    """Run a privacy-first, local Vidigami workflow."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@auth_app.command("login")
def auth_login() -> None:
    """Start the interactive OAuth flow (future implementation)."""
    _not_implemented("auth login")


@auth_app.command("status")
def auth_status() -> None:
    """Show local authentication status (future implementation)."""
    _not_implemented("auth status")


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove local authentication (future implementation)."""
    _not_implemented("auth logout")


@app.command()
def relationships() -> None:
    """List accessible relationships without persisting names (future implementation)."""
    _not_implemented("relationships")


@app.command()
def doctor() -> None:
    """Check local configuration and dependencies (future implementation)."""
    _not_implemented("doctor")


@app.command()
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Enumerate without downloading.")
    ] = False,
    config: Annotated[Path | None, typer.Option("--config", path_type=Path)] = None,
) -> None:
    """Reconcile membership IDs and download selected media (future implementation)."""
    load_config(config)
    suffix = " (dry run)" if dry_run else ""
    _not_implemented(f"sync{suffix}")


@app.command()
def status() -> None:
    """Show local sync and download status (future implementation)."""
    _not_implemented("status")


@app.command()
def report() -> None:
    """Render a derived metadata report (future implementation)."""
    _not_implemented("report")


@app.command()
def verify() -> None:
    """Verify downloaded files against recorded checksums (future implementation)."""
    _not_implemented("verify")
