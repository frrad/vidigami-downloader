"""Metadata extracted from downloaded originals.

The local original is the authority for facts that can be inspected without
contacting Vidigami.  In particular, ``captured_at`` is camera capture time
from EXIF ``DateTimeOriginal`` (or ``DateTimeDigitized`` when the former is
absent), never the provider's upload/creation timestamp.  An EXIF timestamp
without an offset is retained as a naive local-wall-clock value because its
timezone is unknown.  Missing or malformed metadata is represented as ``None``
rather than inferred from filesystem times.

Pillow is used for format detection, image dimensions, and the EXIF formats
used by common JPEG/TIFF originals.  The module intentionally returns only
technical fields; EXIF GPS, camera serial numbers, and other identifying tags
are not persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class LocalMediaMetadata:
    """Safe technical metadata extracted from one local file."""

    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None


def extract_local_metadata(path: Path) -> LocalMediaMetadata:
    """Inspect ``path`` and return MIME, dimensions, and capture time.

    MIME is identified from file content by Pillow, never guessed from a
    filename alone.  No exception is raised for an unrecognised format; an
    unreadable path itself still raises ``OSError`` so callers can report a
    missing download separately from a file with no embedded metadata.
    """

    # ``is_file`` keeps directory paths from being handed to Pillow while
    # retaining a useful OSError-like failure for callers doing a backfill.
    if not path.is_file():
        raise FileNotFoundError(path)

    try:
        with Image.open(path) as image:
            mime_type = Image.MIME.get(image.format) if image.format else None
            exif: Any
            try:
                exif = image.getexif()
            except (AttributeError, OSError, TypeError, ValueError, KeyError, IndexError):
                # A damaged EXIF block must not discard reliable format and
                # dimensions read from the image container.
                exif = {}
            captured_at = _capture_time(exif)
            return LocalMediaMetadata(
                mime_type=mime_type,
                width=_positive_int(image.width),
                height=_positive_int(image.height),
                captured_at=captured_at,
            )
    except (UnidentifiedImageError, OSError):
        # Videos and unusual formats may not be supported by Pillow.  Their
        # extension is still useful for MIME, while dimensions/capture remain
        # unknown instead of being guessed from an unreliable source.
        return LocalMediaMetadata()


def _positive_int(value: Any) -> int | None:
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer > 0 else None


def _capture_time(exif: Any) -> datetime | None:
    """Read EXIF DateTimeOriginal, then DateTimeDigitized, safely."""

    try:
        capture_exif = exif
        original = capture_exif.get(36867)  # DateTimeOriginal
        digitized = capture_exif.get(36868)  # DateTimeDigitized
        # JPEG-family files commonly expose these tags directly, while MPO
        # and some TIFF writers put them in the Exif sub-IFD (34665).
        if not (original or digitized):
            get_ifd = getattr(exif, "get_ifd", None)
            if callable(get_ifd):
                nested = get_ifd(34665)
                if nested is not None:
                    capture_exif = nested
                    original = capture_exif.get(36867)
                    digitized = capture_exif.get(36868)
        value = original or digitized
        if not value:
            return None
        parsed = datetime.strptime(_text(value), "%Y:%m:%d %H:%M:%S")
        offset = _exif_offset(capture_exif, 36881 if original else 36882)
        # Without OffsetTimeOriginal/OffsetTimeDigitized, retain the local
        # wall-clock value.  Assigning UTC would fabricate an instant.
        return parsed.replace(tzinfo=offset) if offset else parsed
    except (AttributeError, TypeError, ValueError, OverflowError, KeyError, IndexError):
        return None


def _exif_offset(exif: Any, tag: int) -> timezone | None:
    """Convert EXIF ``OffsetTime*`` (``+HH:MM``) into a timezone."""

    try:
        value = _text(exif.get(tag) or "")
        if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
            return None
        hours, minutes = int(value[1:3]), int(value[4:6])
        if hours > 23 or minutes > 59:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        if value[0] == "-":
            delta = -delta
        return timezone(delta)
    except (AttributeError, TypeError, ValueError, OverflowError, KeyError, IndexError):
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="strict").strip()
    return str(value).strip()


__all__ = ["LocalMediaMetadata", "extract_local_metadata"]
