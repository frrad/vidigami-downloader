"""Domain models for the local Vidigami archive state.

The models deliberately keep membership IDs as data.  Whether an item is
selected for a particular run is calculated by :mod:`selectors`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class MediaRecord:
    media_id: str
    media_type: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContainerMembership:
    media_id: str
    container_type: str
    container_id: str
    parent_page_id: str | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TagMembership:
    media_id: str
    tag_id: str | None = None
    user_id: str | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    removed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.tag_id is None and self.user_id is None:
            raise ValueError("a tag membership must have tag_id or user_id")


@dataclass(frozen=True, slots=True)
class SelectionCriteria:
    page_ids: frozenset[str] = frozenset()
    tagged_user_ids: frozenset[str] = frozenset()

    @classmethod
    def from_values(
        cls,
        page_ids: list[str] | tuple[str, ...] | set[str] = (),
        tagged_user_ids: list[str] | tuple[str, ...] | set[str] = (),
    ) -> SelectionCriteria:
        return cls(frozenset(page_ids), frozenset(tagged_user_ids))


@dataclass(frozen=True, slots=True)
class SelectedMedia:
    media: MediaRecord
    matched_page_ids: tuple[str, ...] = ()
    matched_tag_user_ids: tuple[str, ...] = ()

    @property
    def selected(self) -> bool:
        return bool(self.matched_page_ids or self.matched_tag_user_ids)


@dataclass(frozen=True, slots=True)
class SyncResult:
    run_id: str
    page_media_count: int
    tagged_media_count: int
    candidate_count: int
    hydrated_count: int
    selected_count: int
    dry_run: bool = False
    errors: tuple[str, ...] = ()
