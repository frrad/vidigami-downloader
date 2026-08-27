from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from vidigami_downloader import service
from vidigami_downloader.config import AppConfig, StorageConfig, VidigamiConfig
from vidigami_downloader.downloads import DownloadError, DownloadResult
from vidigami_downloader.graphql import DownloadOption
from vidigami_downloader.models import MediaRecord, SelectedMedia
from vidigami_downloader.state import StateStore


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        vidigami=VidigamiConfig(
            organization_id="org|synthetic",
            space_id="space|synthetic",
            page_ids=("page|synthetic",),
        ),
        storage=StorageConfig(
            database_path=tmp_path / "state.sqlite3",
            archive_directory=tmp_path / "archive",
            reports_directory=tmp_path / "reports",
        ),
    )


class FakeDownloadAPI:
    def __init__(self, ids: list[str], missing: set[str] | None = None) -> None:
        self.ids = ids
        self.missing = missing or set()
        self.batches: list[tuple[str, ...]] = []

    def get_media_downloads(
        self, media_ids: list[str]
    ) -> dict[str, tuple[dict[str, str], list[DownloadOption]]]:
        self.batches.append(tuple(media_ids))
        return {
            media_id: (
                {"id": media_id, "originalFileName": f"{media_id}.jpg"},
                [] if media_id in self.missing else [
                    DownloadOption("original", f"https://cdn.invalid/{media_id}")
                ],
            )
            for media_id in media_ids
        }


def test_run_sync_dry_run_does_not_write_state_or_request_downloads(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class DiscoveryOnlyAPI:
        def enumerate_page_media(self, page_id: str) -> list[str]:
            assert page_id == "page|synthetic"
            return ["media|one"]

        def get_media_downloads(self, media_ids: list[str]) -> dict[str, Any]:
            raise AssertionError("dry-run must not request download records")

    store = StateStore()
    summary = service.run_sync(config, dry_run=True, api=DiscoveryOnlyAPI(), store=store)

    assert summary.result.dry_run is True
    assert summary.result.candidate_count == 1
    assert summary.selected == ()
    assert summary.downloads == service.DownloadSummary(0, 0, 0, 0)
    assert store.snapshot().media == ()
    assert store.connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
    store.close()


def test_download_selected_batches_ids_and_records_completed_downloads(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    ids = [f"media|{index:02d}" for index in range(51)]
    selected = tuple(
        SelectedMedia(MediaRecord(media_id, filename=f"{media_id}.jpg"), ("page|synthetic",))
        for media_id in ids
    )
    store = StateStore()
    for item in selected:
        store.upsert_media(item.media)
    api = FakeDownloadAPI(ids)
    calls: list[str] = []

    def fake_download(
        request: Any, destination: Path, *, timeout: float
    ) -> DownloadResult:
        assert timeout == config.network.request_timeout_seconds
        calls.append(request.media_id)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{request.media_id}.jpg"
        body = request.media_id.encode("utf-8")
        path.write_bytes(body)
        return DownloadResult(request.media_id, path, len(body), hashlib.sha256(body).hexdigest())

    monkeypatch.setattr(service, "download_media", fake_download)
    summary = service._download_selected(config, api, store, selected)

    assert [len(batch) for batch in api.batches] == [50, 1]
    assert api.batches[0] == tuple(ids[:50])
    assert api.batches[1] == (ids[50],)
    assert calls == ids
    assert summary == service.DownloadSummary(51, 51, 0, 0)
    rows = store.connection.execute(
        "SELECT media_id, status, byte_count, sha256 FROM downloads ORDER BY media_id"
    ).fetchall()
    assert len(rows) == 51
    assert {row["status"] for row in rows} == {"complete"}
    assert all(row["byte_count"] == len(row["media_id"].encode()) for row in rows)
    store.close()


def test_download_selected_records_generic_failures_without_signed_urls(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    selected = tuple(
        SelectedMedia(MediaRecord(media_id, filename="photo.jpg"), ("page|synthetic",))
        for media_id in ("media|missing", "media|failed")
    )
    store = StateStore()
    for item in selected:
        store.upsert_media(item.media)
    api = FakeDownloadAPI([item.media.media_id for item in selected], missing={"media|missing"})

    def fake_download(*args: Any, **kwargs: Any) -> DownloadResult:
        raise DownloadError("signed URL must not leak")

    monkeypatch.setattr(service, "download_media", fake_download)
    summary = service._download_selected(config, api, store, selected)

    assert summary == service.DownloadSummary(2, 0, 0, 2)
    rows = store.connection.execute(
        "SELECT media_id, status, last_error FROM downloads ORDER BY media_id"
    ).fetchall()
    assert [(row["media_id"], row["status"], row["last_error"]) for row in rows] == [
        ("media|failed", "failed", "Download failed"),
        ("media|missing", "failed", "No compatible download URL was returned"),
    ]
    assert "signed URL" not in " ".join(str(row["last_error"]) for row in rows)
    store.close()


def test_verify_downloads_distinguishes_valid_and_invalid_files(tmp_path: Path) -> None:
    store = StateStore()
    valid_path = tmp_path / "valid.bin"
    valid_path.write_bytes(b"valid")
    store.upsert_media(MediaRecord("media|valid"))
    store.upsert_media(MediaRecord("media|missing"))
    store.record_download(
        "media|valid",
        status="complete",
        local_path=str(valid_path),
        sha256=hashlib.sha256(b"valid").hexdigest(),
    )
    store.record_download(
        "media|missing",
        status="complete",
        local_path=str(tmp_path / "not-there.bin"),
        sha256=hashlib.sha256(b"missing").hexdigest(),
    )

    assert service.verify_downloads(store) == (2, 1, 1)
    store.close()
