from __future__ import annotations

import json
import threading

import pytest
from typer.testing import CliRunner

from vidigami_downloader import cli_impl, progress, service
from vidigami_downloader.config import AppConfig
from vidigami_downloader.models import MediaRecord, SelectedMedia, SelectionCriteria, SyncResult
from vidigami_downloader.progress import ProgressEvent, ProgressReporter
from vidigami_downloader.state import StateStore
from vidigami_downloader.sync import SyncEngine


def test_discovery_reports_before_slow_api_call_and_dry_run_skips_hydration():
    events = []

    class API:
        def enumerate_page_media(self, page_id):
            assert events[-1] == ProgressEvent("Discovering pages", 0, 1)
            return ["private-media-id"]

        def enumerate_album_media(self, album_id):
            assert events[-1] == ProgressEvent("Discovering albums", 0, 1)
            return ["private-media-id"]

        def enumerate_tagged_media(self, user_id):
            assert events[-1] == ProgressEvent("Discovering tagged media", 0, 1)
            return []

    store = StateStore()
    SyncEngine(API(), store).run(
        SelectionCriteria.from_values(page_ids=["p"], album_ids=["a"], tagged_user_ids=["u"]),
        dry_run=True,
        progress=events.append,
    )
    assert events[-1] == ProgressEvent("Discovering tagged media", 1, 1)
    assert all("private-media-id" not in str(event) for event in events)
    assert not store.snapshot().media
    store.close()


def test_metadata_counts_failure_and_completes():
    events = []

    class API:
        def enumerate_page_media(self, page_id):
            return ["private-id"]

        def get_media(self, media_id):
            assert events[-1] == ProgressEvent("Checking metadata", 0, 1)
            raise ValueError("private error")

    store = StateStore()
    SyncEngine(API(), store).run(
        SelectionCriteria.from_values(page_ids=["p"]),
        progress=events.append,
    )
    assert events[-1] == ProgressEvent("Checking metadata", 1, 1, failed=1)
    assert "private" not in str(events)
    store.close()


def test_download_progress_includes_link_batches_and_missing_url_failures():
    events = []

    class API:
        def get_media_downloads(self, ids):
            assert events[-1].stage == "Fetching download links"
            return {}

    selected = tuple(SelectedMedia(MediaRecord(f"private-{i}"), ()) for i in range(51))
    store = StateStore()
    for item in selected:
        store.upsert_media(item.media)
    result = service._download_selected(
        AppConfig(),
        API(),
        store,
        selected,
        progress=events.append,
    )
    assert result.failed == 51
    assert ProgressEvent("Fetching download links", 50, 51) in events
    assert ProgressEvent("Fetching download links", 51, 51) in events
    assert events[-1] == ProgressEvent("Downloading files", 51, 51, failed=51)
    store.close()


def test_heartbeat_updates_during_blocking_work_and_stops_on_error(monkeypatch):
    heartbeat = threading.Event()
    messages = []

    def echo(message, *, err):
        assert err
        messages.append(message)
        if len(messages) >= 3:
            heartbeat.set()

    monkeypatch.setattr(progress.typer, "echo", echo)
    reporter = ProgressReporter(interval=0.01)
    with pytest.raises(RuntimeError), reporter:
        reporter(ProgressEvent("Discovering pages", 0, 8))
        assert heartbeat.wait(2), "No heartbeat while a source request was blocked"
        raise RuntimeError("failed request")
    assert not reporter._thread.is_alive()
    assert messages[-1].endswith("Discovering pages 0/8")


def test_cli_progress_stays_on_stderr_and_json_stays_on_stdout(monkeypatch):
    def fake_run(config, *, dry_run, progress):
        progress(ProgressEvent("Downloading files", 2, 2, failed=1, reused=1))
        return service.SyncSummary(
            SyncResult("synthetic-run", 2, 0, 2, 0, 2),
            (),
            service.DownloadSummary(2, 1, 1, 1),
        )

    monkeypatch.setattr(cli_impl, "run_sync", fake_run)
    result = CliRunner().invoke(cli_impl.app, ["sync"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["downloads"]["failed"] == 1
    assert "Downloading files 2/2 (1 reused, 1 failed)" in result.stderr
    assert "Starting sync" in result.stderr
