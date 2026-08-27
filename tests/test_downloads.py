from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from vidigami_downloader.downloads import (
    DownloadError,
    DownloadRequest,
    archive_filename,
    download_media,
)


class FakeResponse(BytesIO):
    def __init__(self, body: bytes, content_length: int | None = None) -> None:
        super().__init__(body)
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_archive_filename_hides_id_and_keeps_safe_extension() -> None:
    result = archive_filename("media|synthetic-123", "Holiday Photo.JPG")
    assert "synthetic" not in result
    assert result.endswith(".jpg")
    assert len(result) == 68


def test_download_is_atomic_and_returns_hash(tmp_path: Path) -> None:
    body = b"synthetic image bytes"

    def opener(request: Request, timeout: float) -> FakeResponse:
        assert request.full_url == "https://cdn.example.invalid/signed"
        assert timeout == 12
        return FakeResponse(body, len(body))

    result = download_media(
        DownloadRequest("media|synthetic-123", "https://cdn.example.invalid/signed", "x.jpeg"),
        tmp_path,
        timeout=12,
        opener=opener,
    )
    assert result.path.read_bytes() == body
    assert result.byte_count == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert list(tmp_path.glob("*.part")) == []


def test_incomplete_download_is_removed(tmp_path: Path) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(b"short", 100)

    with pytest.raises(DownloadError, match="incomplete"):
        download_media(
            DownloadRequest("media|synthetic-456", "https://cdn.example.invalid/signed"),
            tmp_path,
            opener=opener,
        )
    assert list(tmp_path.iterdir()) == []


def test_rejects_non_https_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        download_media(
            DownloadRequest("media|synthetic-789", "http://cdn.example.invalid/file"),
            tmp_path,
        )
