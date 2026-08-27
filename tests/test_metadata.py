from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from vidigami_downloader.metadata import (
    LocalMediaMetadata,
    _capture_time,
    extract_local_metadata,
)
from vidigami_downloader.models import MediaRecord
from vidigami_downloader.service import backfill_local_metadata
from vidigami_downloader.state import StateStore


def _jpeg(path: Path, *, capture: str | None = None, offset: str | None = None) -> None:
    image = Image.new("RGB", (13, 7), color=(20, 40, 60))
    if capture:
        exif = Image.Exif()
        exif[36867] = capture
        if offset:
            exif[36881] = offset
        image.save(path, format="JPEG", exif=exif)
    else:
        image.save(path, format="JPEG")


def test_extracts_content_mime_dimensions_and_exif_offset(tmp_path: Path) -> None:
    path = tmp_path / "opaque-name.bin"
    _jpeg(path, capture="2024:05:06 07:08:09", offset="-04:30")

    metadata = extract_local_metadata(path)

    assert metadata.mime_type == "image/jpeg"
    assert (metadata.width, metadata.height) == (13, 7)
    assert metadata.captured_at == datetime(
        2024, 5, 6, 7, 8, 9, tzinfo=timezone(timedelta(hours=-4, minutes=-30))
    )


def test_digitized_is_fallback_and_missing_offset_stays_timezone_unknown(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    image = Image.new("RGB", (2, 3))
    exif = Image.Exif()
    exif[36868] = "2020:01:02 03:04:05"
    image.save(path, format="JPEG", exif=exif)

    metadata = extract_local_metadata(path)

    assert metadata.captured_at == datetime(2020, 1, 2, 3, 4, 5)


def test_capture_time_reads_nested_exif_ifd_used_by_mpo() -> None:
    class NestedExif:
        def get(self, tag: int) -> object:
            return {34665: 242}.get(tag)

        def get_ifd(self, tag: int) -> dict[int, str]:
            assert tag == 34665
            return {36867: "2026:08:26 11:01:28", 36881: "-07:00"}

    assert _capture_time(NestedExif()) == datetime(
        2026, 8, 26, 11, 1, 28, tzinfo=timezone(timedelta(hours=-7))
    )


def test_png_dimensions_and_mime_are_available_without_exif(tmp_path: Path) -> None:
    path = tmp_path / "opaque-name.dat"
    Image.new("RGBA", (4, 11)).save(path, format="PNG")

    metadata = extract_local_metadata(path)

    assert metadata.mime_type == "image/png"
    assert (metadata.width, metadata.height, metadata.captured_at) == (4, 11, None)


def test_corrupt_or_unsupported_file_returns_no_guessed_metadata(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"not an image")

    assert extract_local_metadata(path) == LocalMediaMetadata()


def test_backfill_only_completed_downloads_and_preserves_nonnull_values(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    _jpeg(path, capture="2024:05:06 07:08:09")
    store = StateStore()
    store.upsert_media(MediaRecord("media|photo", width=99))
    store.upsert_media(MediaRecord("media|pending"))
    store.record_download(
        "media|photo", status="complete", local_path=str(path), sha256="unused"
    )
    store.record_download(
        "media|pending", status="pending", local_path=str(path), sha256="unused"
    )

    summary = backfill_local_metadata(store)

    assert summary.inspected == 1
    assert summary.updated == 1
    row = store.connection.execute(
        "SELECT mime_type,width,height,captured_at FROM media WHERE media_id='media|photo'"
    ).fetchone()
    assert tuple(row) == ("image/jpeg", 99, 7, "2024-05-06T07:08:09")
    rerun = backfill_local_metadata(store)
    assert rerun.updated == 0
    assert rerun.inspected == 1
    store.close()
