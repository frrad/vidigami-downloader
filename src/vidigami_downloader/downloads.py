"""Atomic downloads of short-lived Vidigami media URLs.

Download URLs are deliberately treated as secrets: this module never includes
one in an exception, result, filename, or log message.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DownloadError(RuntimeError):
    """A media download failed without disclosing its signed URL."""


class Response(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> Response: ...

    def __exit__(self, *args: object) -> None: ...


class Writer(Protocol):
    def write(self, data: bytes) -> object: ...


OpenUrl = Callable[[Request, float], Response]


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Everything needed to download one media object."""

    media_id: str
    url: str
    original_filename: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Stable local facts suitable for recording in SQLite."""

    media_id: str
    path: Path
    byte_count: int
    sha256: str


def archive_filename(media_id: str, original_filename: str | None) -> str:
    """Return a portable, non-identifying filename for an opaque media ID."""

    digest = hashlib.sha256(media_id.encode("utf-8")).hexdigest()
    suffix = _safe_suffix(original_filename)
    return f"{digest}{suffix}"


def download_media(
    item: DownloadRequest,
    archive_directory: Path,
    *,
    timeout: float = 60.0,
    chunk_size: int = 1024 * 1024,
    opener: OpenUrl | None = None,
) -> DownloadResult:
    """Download one object atomically and return its local checksum.

    A same-filesystem temporary file is fsynced and renamed only after the
    response is complete. An existing destination is verified and reused.
    """

    if not item.media_id:
        raise ValueError("media_id must not be empty")
    if not item.url.lower().startswith("https://"):
        raise ValueError("download URLs must use HTTPS")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    archive_directory.mkdir(parents=True, exist_ok=True)
    destination = archive_directory / archive_filename(
        item.media_id, item.original_filename
    )
    if destination.exists():
        checksum, byte_count = hash_file(destination, chunk_size=chunk_size)
        return DownloadResult(item.media_id, destination, byte_count, checksum)

    open_url = opener or _open_url
    temporary_path: Path | None = None
    try:
        request = Request(item.url, headers={"Accept": "*/*"}, method="GET")
        with open_url(request, timeout) as response:
            expected = _content_length(response.headers)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".vidigami-", suffix=".part",
                dir=archive_directory, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                checksum, byte_count = _copy_and_hash(
                    response, temporary, chunk_size=chunk_size
                )
                temporary.flush()
                os.fsync(temporary.fileno())
        if expected is not None and byte_count != expected:
            raise DownloadError(
                f"Media {item.media_id!r} was incomplete "
                f"({byte_count} of {expected} bytes)"
            )
        os.replace(temporary_path, destination)
        temporary_path = None
        return DownloadResult(item.media_id, destination, byte_count, checksum)
    except DownloadError:
        raise
    except HTTPError as exc:
        raise DownloadError(
            f"Media {item.media_id!r} returned HTTP {exc.code}; refresh its download URL"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"Could not download media {item.media_id!r}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Return the SHA-256 digest and byte length of a local file."""

    with path.open("rb") as source:
        return _copy_and_hash(source, None, chunk_size=chunk_size)


def _copy_and_hash(
    source: BinaryIO | Response,
    destination: Writer | None,
    *,
    chunk_size: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    while chunk := source.read(chunk_size):
        digest.update(chunk)
        byte_count += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest(), byte_count


def _open_url(request: Request, timeout: float) -> Response:
    return cast(Response, urlopen(request, timeout=timeout))  # noqa: S310


def _content_length(headers: object) -> int | None:
    getter = getattr(headers, "get", None)
    value = getter("Content-Length") if callable(getter) else None
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_suffix(filename: str | None) -> str:
    if not filename:
        return ""
    suffix = Path(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


__all__ = [
    "DownloadError",
    "DownloadRequest",
    "DownloadResult",
    "archive_filename",
    "download_media",
    "hash_file",
]
