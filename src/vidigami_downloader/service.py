"""Application wiring for the command-line interface.

This module is intentionally small: authentication, API transport, state, and
selection remain independently testable, while the CLI gets one safe place to
compose them.  It never logs bearer tokens or signed media URLs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth import OAuthClient, OAuthConfig
from .config import AppConfig
from .downloads import DownloadError, DownloadRequest, download_media, hash_file
from .graphql import DownloadOption, GraphQLClient
from .models import SelectedMedia, SelectionCriteria, SyncResult
from .selectors import select_from_state
from .state import StateStore
from .sync import SyncEngine


@dataclass(frozen=True, slots=True)
class DownloadSummary:
    attempted: int
    completed: int
    reused: int
    failed: int


@dataclass(frozen=True, slots=True)
class SyncSummary:
    result: SyncResult
    selected: tuple[SelectedMedia, ...]
    downloads: DownloadSummary


def criteria_for(config: AppConfig) -> SelectionCriteria:
    return SelectionCriteria.from_values(
        page_ids=config.vidigami.page_ids,
        tagged_user_ids=config.vidigami.tagged_user_ids,
    )


def oauth_client(config: AppConfig) -> OAuthClient:
    """Build an OAuth client from local config without exposing its secret."""

    auth = config.auth
    return OAuthClient(
        OAuthConfig(
            client_id=auth.client_id,
            client_secret=auth.client_secret,
            authorization_endpoint=auth.authorization_endpoint,
            token_endpoint=auth.token_endpoint,
            redirect_uri=auth.redirect_uri,
            token_auth_method=auth.token_auth_method,
        )
    )


def graph_client(config: AppConfig, auth: OAuthClient | None = None) -> GraphQLClient:
    auth = auth or oauth_client(config)
    return GraphQLClient(
        auth.access_token,
        timeout=config.network.request_timeout_seconds,
        retries=config.network.max_retries,
        organization_id=config.vidigami.organization_id or None,
        organization_identifier=config.vidigami.organization_identifier,
        space_id=config.vidigami.space_id or None,
    )


def open_store(config: AppConfig) -> StateStore:
    config.storage.database_path.parent.mkdir(parents=True, exist_ok=True)
    return StateStore(config.storage.database_path)


def run_sync(
    config: AppConfig,
    *,
    dry_run: bool = False,
    api: GraphQLClient | None = None,
    store: StateStore | None = None,
) -> SyncSummary:
    """Reconcile configured sources and optionally download selected media."""

    owned_store = store is None
    state = store or open_store(config)
    client = api or graph_client(config)
    try:
        result = SyncEngine(client, state).run(criteria_for(config), dry_run=dry_run)
        selected = tuple(select_from_state(state, criteria_for(config))) if not dry_run else ()
        downloads = (
            _download_selected(config, client, state, selected)
            if not dry_run
            else DownloadSummary(0, 0, 0, 0)
        )
        return SyncSummary(result=result, selected=selected, downloads=downloads)
    finally:
        if owned_store:
            state.close()


def _download_selected(
    config: AppConfig,
    api: GraphQLClient,
    store: StateStore,
    selected: tuple[SelectedMedia, ...],
) -> DownloadSummary:
    if not selected:
        return DownloadSummary(0, 0, 0, 0)
    media_ids = [item.media.media_id for item in selected]
    records: dict[str, tuple[Mapping[str, Any], list[DownloadOption]]] = {}
    for start in range(0, len(media_ids), 50):
        records.update(api.get_media_downloads(media_ids[start : start + 50]))
    attempted = completed = reused = failed = 0
    for item in selected:
        attempted += 1
        record = records.get(item.media.media_id)
        option = _choose_download_option(
            record[1] if record else (), config.vidigami.download_quality
        )
        if option is None:
            failed += 1
            store.record_download(
                item.media.media_id,
                quality=config.vidigami.download_quality,
                status="failed",
                last_error="No compatible download URL was returned",
            )
            continue
        filename = record[0].get("originalFileName") if record else item.media.filename
        try:
            destination = config.storage.archive_directory
            was_present = (destination / _archive_name(item.media.media_id, filename)).exists()
            outcome = download_media(
                DownloadRequest(item.media.media_id, option.url, _as_string(filename)),
                destination,
                timeout=config.network.request_timeout_seconds,
            )
            store.record_download(
                item.media.media_id,
                quality=option.quality or config.vidigami.download_quality,
                local_path=str(outcome.path),
                byte_count=outcome.byte_count,
                sha256=outcome.sha256,
                status="complete",
                completed_at=datetime.now(UTC).isoformat(),
                last_error=None,
            )
            completed += 1
            reused += int(was_present)
        except (DownloadError, OSError, ValueError):
            failed += 1
            # Keep error content generic: an exception could contain a remote
            # response or a signed URL supplied by an upstream library.
            store.record_download(
                item.media.media_id,
                quality=option.quality or config.vidigami.download_quality,
                status="failed",
                last_error="Download failed",
            )
    return DownloadSummary(attempted, completed, reused, failed)


def _choose_download_option(
    options: tuple[DownloadOption, ...] | list[DownloadOption], quality: str
) -> DownloadOption | None:
    by_quality = {option.quality: option for option in options if option.url}
    if quality in by_quality:
        return by_quality[quality]
    for fallback in ("original", "print", "web"):
        if fallback in by_quality:
            return by_quality[fallback]
    return None


def verify_downloads(store: StateStore) -> tuple[int, int, int]:
    """Return ``(checked, valid, invalid)`` for completed local downloads."""

    rows = store.connection.execute(
        "SELECT local_path, sha256, status FROM downloads WHERE status='complete'"
    ).fetchall()
    checked = valid = invalid = 0
    for row in rows:
        checked += 1
        path = Path(row["local_path"]) if row["local_path"] else None
        try:
            digest, _ = hash_file(path) if path else (None, None)
            if digest and digest == row["sha256"]:
                valid += 1
            else:
                invalid += 1
        except (OSError, TypeError, ValueError):
            invalid += 1
    return checked, valid, invalid


def _archive_name(media_id: str, filename: object) -> str:
    # Importing the canonical naming helper here avoids duplicating its hash
    # policy while keeping signed URLs out of the application layer.
    from .downloads import archive_filename

    return archive_filename(media_id, _as_string(filename))


def _as_string(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "DownloadSummary",
    "SyncSummary",
    "criteria_for",
    "graph_client",
    "oauth_client",
    "open_store",
    "run_sync",
    "verify_downloads",
]
