"""Discovery, hydration, and reconciliation orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from .models import (
    ContainerMembership,
    MediaRecord,
    SelectionCriteria,
    SyncResult,
    TagMembership,
)
from .selectors import select_from_state
from .state import StateStore


class VidigamiAPI(Protocol):
    """Minimal synchronous adapter expected from the GraphQL client."""

    def enumerate_page_media(self, page_id: str) -> Iterable[str]: ...

    def enumerate_tagged_media(self, user_id: str) -> Iterable[str]: ...

    def get_media(self, media_id: str) -> MediaRecord: ...

    def get_media_containers(self, media_id: str) -> Sequence[ContainerMembership]: ...

    def get_media_tags(self, media_id: str) -> Sequence[TagMembership]: ...


def _page_media(api: object, page_id: str) -> Iterable[str]:
    method = getattr(api, "enumerate_page_media", None) or getattr(api, "get_page_media_ids", None)
    if not callable(method):
        raise TypeError("API adapter must provide enumerate_page_media or get_page_media_ids")
    return (str(item) for item in method(page_id))


def _tagged_media(api: object, user_id: str) -> Iterable[str]:
    method = getattr(api, "enumerate_tagged_media", None) or getattr(api, "get_user_media", None)
    if not callable(method):
        raise TypeError("API adapter must provide enumerate_tagged_media or get_user_media")
    return (str(item) for item in method(user_id))


def _media_record(api: object, media_id: str) -> MediaRecord:
    method = getattr(api, "get_media", None)
    if callable(method):
        value = method(media_id)
        if isinstance(value, MediaRecord):
            return value
        if isinstance(value, Mapping):
            return _media_from_mapping(media_id, value)
    method = getattr(api, "get_media_downloads", None)
    if callable(method):
        raw = method(media_id)
        if isinstance(raw, tuple) and raw and isinstance(raw[0], Mapping):
            return _media_from_mapping(media_id, raw[0])
        if isinstance(raw, Mapping):
            return _media_from_mapping(media_id, raw)
    return MediaRecord(media_id)


def _media_from_mapping(media_id: str, raw: Mapping[str, Any]) -> MediaRecord:
    # ``createdAt`` is the provider's record/upload timestamp, not a camera
    # capture time.  Only an explicitly named capture field may populate the
    # canonical capture timestamp; local EXIF extraction is the normal source.
    captured = raw.get("capturedAt")
    # Do not persist arbitrary API fields here.  Fallback mappings can come
    # from a download response containing signed URLs or other credentials;
    # the only upstream value intentionally retained is the non-secret
    # creation timestamp under an explicit provenance key.
    metadata: dict[str, Any] = {}
    if raw.get("createdAt") is not None:
        metadata["upstream_created_at"] = raw["createdAt"]
    return MediaRecord(
        media_id=str(raw.get("id") or media_id),
        media_type=_string(raw.get("type")),
        mime_type=_string(raw.get("mimeType")),
        filename=_string(raw.get("filename") or raw.get("originalFileName")),
        width=_integer(raw.get("width")),
        height=_integer(raw.get("height")),
        captured_at=captured,
        metadata=metadata,
    )


def _containers(api: object, media_id: str) -> Sequence[ContainerMembership]:
    method = (
        getattr(api, "get_media_containers", None)
        or getattr(api, "get_lightbox_media_containers", None)
    )
    if not callable(method):
        raise TypeError("API adapter must provide media container hydration")
    result: list[ContainerMembership] = []
    for value in method(media_id):
        if isinstance(value, ContainerMembership):
            result.append(value)
            continue
        raw = value if isinstance(value, Mapping) else vars(value)
        container_id = _string(raw.get("container_id") or raw.get("containerId") or raw.get("id"))
        if not container_id:
            continue
        result.append(ContainerMembership(
            media_id,
            _string(raw.get("container_type") or raw.get("containerType")) or "CONTAINER",
            container_id,
            _string(raw.get("parent_page_id") or raw.get("parentPageId")),
        ))
    return result


def _tags(api: object, media_id: str) -> Sequence[TagMembership]:
    method = getattr(api, "get_media_tags", None) or getattr(api, "get_face_tags_on_media", None)
    if not callable(method):
        raise TypeError("API adapter must provide media tag hydration")
    result: list[TagMembership] = []
    for value in method(media_id):
        if isinstance(value, TagMembership):
            result.append(value)
            continue
        raw = value if isinstance(value, Mapping) else vars(value)
        tag_id = _string(raw.get("tag_id") or raw.get("tagId") or raw.get("id"))
        user_id = _string(raw.get("user_id") or raw.get("userId"))
        user = raw.get("user")
        if user_id is None and isinstance(user, Mapping):
            user_id = _string(user.get("id"))
        if tag_id is not None or user_id is not None:
            result.append(TagMembership(media_id, tag_id, user_id))
    return result


class SyncEngine:
    def __init__(self, api: VidigamiAPI, store: StateStore) -> None:
        self.api = api
        self.store = store

    def run(self, criteria: SelectionCriteria, *, dry_run: bool = False) -> SyncResult:
        run_id = self.store.begin_sync() if not dry_run else "dry-run"
        page_ids: set[str] = set()
        tagged_ids: set[str] = set()
        discovered_page_memberships: dict[str, set[str]] = {}
        page_discoveries: dict[str, list[str]] = {}
        tagged_discoveries: dict[str, list[str]] = {}
        errors: list[str] = []
        try:
            for page_id in criteria.page_ids:
                discovered = list(_page_media(self.api, page_id))
                page_discoveries[page_id] = discovered
                for media_id in discovered:
                    page_ids.add(media_id)
                    # The discovery source is authoritative evidence that this
                    # media belongs to the selected page.  Keep it even when
                    # the lightbox/container detail query has no page field.
                    discovered_page_memberships.setdefault(media_id, set()).add(page_id)
            for user_id in criteria.tagged_user_ids:
                discovered = list(_tagged_media(self.api, user_id))
                tagged_discoveries[user_id] = discovered
                tagged_ids.update(discovered)
            candidates = page_ids | tagged_ids
            hydrated = 0
            if not dry_run:
                # Enumeration is complete for every configured source before
                # any old relationship is closed.  A mid-pagination failure
                # therefore cannot be mistaken for an authoritative empty
                # result.
                for page_id, discovered in page_discoveries.items():
                    self.store.reconcile_page_discovery(page_id, discovered)
                for user_id, discovered in tagged_discoveries.items():
                    self.store.reconcile_tagged_user_discovery(user_id, discovered)
                with self.store.connection:
                    for media_id in sorted(candidates):
                        savepoint = "media_sync"
                        self.store.connection.execute(f"SAVEPOINT {savepoint}")
                        try:
                            needs_hydration = self.store.media_needs_hydration(media_id)
                            page_memberships = _discovered_page_memberships(
                                media_id, discovered_page_memberships.get(media_id, ())
                            )
                            tagged_users = _discovered_tagged_users(
                                media_id, tagged_discoveries
                            )
                            if needs_hydration:
                                media = _media_record(self.api, media_id)
                                containers = list(_containers(self.api, media_id))
                                containers.extend(page_memberships)
                                tags = list(_tags(self.api, media_id))
                                # A complete tagged-media enumeration is also
                                # authoritative evidence.  Keep that evidence
                                # when the detail query returns no matching
                                # face tag, while avoiding a duplicate row when
                                # it does return one.
                                tags.extend(
                                    TagMembership(media_id, user_id=user_id)
                                    for user_id in tagged_users
                                    if not any(tag.user_id == user_id for tag in tags)
                                )
                                self.store.upsert_media(media)
                                self.store.observe_containers(
                                    media_id, containers, authoritative=True
                                )
                                self.store.observe_tags(media_id, tags, authoritative=True)
                                self.store.mark_media_hydrated(media_id)
                                hydrated += 1
                            else:
                                # Enumeration still updates direct source
                                # evidence on the cheap.  This is important
                                # when an item entered through a tagged-user
                                # source but its old deep result was empty.
                                self.store.observe_containers(
                                    media_id, page_memberships, authoritative=False
                                )
                                self.store.observe_tags(
                                    media_id,
                                    [
                                        TagMembership(media_id, user_id=user_id)
                                        for user_id in tagged_users
                                        if not self.store.media_has_tag_user(media_id, user_id)
                                    ],
                                    authoritative=False,
                                )
                        except Exception as exc:  # keep one inaccessible item from losing the run
                            self.store.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                            errors.append(f"{media_id}: {exc}")
                        finally:
                            self.store.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                selected_count = len(select_from_state(self.store, criteria))
            else:
                selected_count = len(candidates)
            if not dry_run:
                self.store.finish_sync(
                    run_id,
                    page_media_count=len(page_ids),
                    tagged_media_count=len(tagged_ids),
                    candidate_count=len(candidates),
                    hydrated_count=hydrated,
                    selected_count=selected_count,
                    errors=errors,
                    status="completed_with_errors" if errors else "completed",
                )
            return SyncResult(
                run_id,
                len(page_ids),
                len(tagged_ids),
                len(candidates),
                hydrated,
                selected_count,
                dry_run,
                tuple(errors),
            )
        except Exception:
            if not dry_run:
                self.store.finish_sync(run_id, status="failed", errors=errors)
            raise


def _discovered_page_memberships(
    media_id: str, page_ids: Iterable[str]
) -> list[ContainerMembership]:
    return [
        ContainerMembership(media_id, "PAGE", page_id, parent_page_id=page_id)
        for page_id in page_ids
    ]


def _discovered_tagged_users(
    media_id: str, discoveries: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    return tuple(
        sorted(user_id for user_id, media_ids in discoveries.items() if media_id in media_ids)
    )


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
