"""Privacy-aware reports of the media selected for a sync.

Reports contain opaque Vidigami identifiers only.  In particular, they do not
copy display names, email addresses, signed download URLs, or arbitrary API
metadata into a report.  Memberships are emitted as identifiers rather than a
derived page/tag boolean so that selection criteria can change on a later run
without losing information.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import ContainerMembership, SelectedMedia, SelectionCriteria, TagMembership
from .selectors import select_media

# Keep the serialized shape deliberately small and stable.  CSV uses the same
# keys as JSON, with nested/list values represented as JSON strings.
REPORT_COLUMNS = (
    "media_id",
    "page_ids",
    "container_ids",
    "containers",
    "face_tags",
    "matched_page_ids",
    "matched_tagged_user_ids",
)


def build_rows(
    selected: Iterable[SelectedMedia],
    containers: Iterable[ContainerMembership] = (),
    tags: Iterable[TagMembership] = (),
) -> list[dict[str, object]]:
    """Build stable report rows for ``selected`` media.

    ``containers`` and ``tags`` should be the canonical observations from the
    state store, not just the memberships that happened to match this run.
    Consequently each row records all known container/page IDs and all known
    face tag IDs for the media item, while the ``matched_*`` fields preserve
    the criteria-derived view used for this particular run.

    No value in a row is a human identity or a boolean page/tag
    classification.  Missing IDs are retained as ``null`` in the face-tag
    objects because an unknown face can have a tag ID without a user ID (and
    vice versa).
    """

    container_index: dict[str, list[ContainerMembership]] = {}
    for container_membership in containers:
        container_index.setdefault(container_membership.media_id, []).append(
            container_membership
        )

    tag_index: dict[str, list[TagMembership]] = {}
    for tag_membership in tags:
        tag_index.setdefault(tag_membership.media_id, []).append(tag_membership)

    rows: list[dict[str, object]] = []
    for item in selected:
        item_containers = _unique_containers(container_index.get(item.media.media_id, ()))
        item_tags = _unique_tags(tag_index.get(item.media.media_id, ()))

        # A selected object may have been built without passing the complete
        # canonical membership collections.  Keep its matched IDs visible in
        # that case, but never invent container/tag records from a boolean.
        page_ids = _page_ids(item_containers)
        matched_page_ids = _sorted_unique(item.matched_page_ids)
        matched_tagged_user_ids = _sorted_unique(item.matched_tag_user_ids)

        rows.append(
            {
                "media_id": item.media.media_id,
                "page_ids": page_ids,
                "container_ids": [entry.container_id for entry in item_containers],
                "containers": [
                    {
                        "container_type": entry.container_type,
                        "container_id": entry.container_id,
                        "parent_page_id": entry.parent_page_id,
                    }
                    for entry in item_containers
                ],
                "face_tags": [
                    {"tag_id": entry.tag_id, "tagged_user_id": entry.user_id}
                    for entry in item_tags
                ],
                "matched_page_ids": matched_page_ids,
                "matched_tagged_user_ids": matched_tagged_user_ids,
            }
        )

    return rows


def rows_from_snapshot(snapshot: Any, criteria: SelectionCriteria) -> list[dict[str, object]]:
    """Select and report media from a :class:`StateSnapshot`-like object."""

    selected = select_media(snapshot.media, snapshot.containers, snapshot.tags, criteria)
    return build_rows(selected, snapshot.containers, snapshot.tags)


def write_json(path: str | Path, rows: Sequence[Mapping[str, object]]) -> Path:
    """Write report rows as UTF-8 JSON and return the destination path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(list(rows), stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    return destination


def write_csv(path: str | Path, rows: Sequence[Mapping[str, object]]) -> Path:
    """Write report rows as UTF-8 CSV and return the destination path.

    List and object columns are JSON-encoded, making the CSV lossless with
    respect to the JSON report and safe to parse without delimiter heuristics.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in REPORT_COLUMNS})
    return destination


def _csv_value(value: object) -> str:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return "" if value is None else str(value)


def _unique_containers(values: Iterable[ContainerMembership]) -> list[ContainerMembership]:
    unique: dict[tuple[str, str], ContainerMembership] = {}
    for value in values:
        unique.setdefault((value.container_type, value.container_id), value)
    return sorted(unique.values(), key=lambda value: (value.container_type, value.container_id))


def _unique_tags(values: Iterable[TagMembership]) -> list[TagMembership]:
    unique: dict[tuple[str | None, str | None], TagMembership] = {}
    for value in values:
        unique.setdefault((value.tag_id, value.user_id), value)
    return sorted(
        unique.values(),
        key=lambda value: (value.tag_id or "", value.user_id or ""),
    )


def _page_ids(values: Iterable[ContainerMembership]) -> list[str]:
    identifiers: set[str] = set()
    for value in values:
        if value.container_type.lower() == "page":
            identifiers.add(value.container_id)
        if value.parent_page_id:
            identifiers.add(value.parent_page_id)
    return sorted(identifiers)


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


__all__ = [
    "REPORT_COLUMNS",
    "build_rows",
    "rows_from_snapshot",
    "write_csv",
    "write_json",
]
