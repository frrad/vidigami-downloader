"""Derived selection over normalized media memberships."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import (
    ContainerMembership,
    MediaRecord,
    SelectedMedia,
    SelectionCriteria,
    TagMembership,
)


def select_media(
    media: Iterable[MediaRecord],
    containers: Iterable[ContainerMembership],
    tags: Iterable[TagMembership],
    criteria: SelectionCriteria,
) -> list[SelectedMedia]:
    """Return media matching the current selectors.

    Membership collections are canonical observations; the result is a view
    and can therefore change when criteria changes without rewriting metadata.
    A page selector matches the page itself and a container whose parent page
    is selected.  Tag selectors match user IDs, irrespective of tag ID.
    """

    by_media_containers: dict[str, list[ContainerMembership]] = {}
    for container_membership in containers:
        by_media_containers.setdefault(container_membership.media_id, []).append(
            container_membership
        )
    by_media_tags: dict[str, list[TagMembership]] = {}
    for tag_membership in tags:
        by_media_tags.setdefault(tag_membership.media_id, []).append(tag_membership)

    result: list[SelectedMedia] = []
    for item in media:
        page_matches: set[str] = set()
        for membership in by_media_containers.get(item.media_id, ()):
            if membership.container_id in criteria.page_ids:
                page_matches.add(membership.container_id)
            if membership.parent_page_id in criteria.page_ids:
                page_matches.add(membership.parent_page_id)
        tag_matches = {
            membership.user_id
            for membership in by_media_tags.get(item.media_id, ())
            if membership.user_id in criteria.tagged_user_ids
        }
        selected = SelectedMedia(
            item,
            tuple(sorted(page_matches)),
            tuple(sorted(tag_matches)),
        )
        if selected.selected:
            result.append(selected)
    return result


def select_from_state(store: object, criteria: SelectionCriteria) -> list[SelectedMedia]:
    """Select from a :class:`~vidigami_downloader.state.StateStore` snapshot."""

    # Keeping this tiny adapter avoids making selectors depend on sqlite.
    snapshot = store.snapshot()  # type: ignore[attr-defined]
    return select_media(
        snapshot.media,
        snapshot.containers,
        snapshot.tags,
        criteria,
    )


def selection_rows(
    selected: Iterable[SelectedMedia],
) -> list[Mapping[str, object]]:
    """Produce stable report rows without persisting derived classifications."""

    return [
        {
            "media_id": item.media.media_id,
            "page_ids": list(item.matched_page_ids),
            "tagged_user_ids": list(item.matched_tag_user_ids),
        }
        for item in selected
    ]
