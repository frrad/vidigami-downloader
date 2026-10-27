from datetime import UTC, datetime

import pytest

from vidigami_downloader.models import (
    ContainerMembership,
    MediaRecord,
    SelectionCriteria,
    TagMembership,
)
from vidigami_downloader.state import StateStore
from vidigami_downloader.sync import SyncEngine, _media_from_mapping


class FakeAPI:
    def enumerate_page_media(self, page_id):
        return {"p1": ["m1", "m2"], "p2": ["m2"]}[page_id]

    def enumerate_tagged_media(self, user_id):
        return {"u1": ["m2", "m3"]}[user_id]

    def get_media(self, media_id):
        return MediaRecord(media_id, media_type="IMAGE")

    def get_media_containers(self, media_id):
        return {"m1": [ContainerMembership("m1", "page", "p1")],
                "m2": [
                    ContainerMembership("m2", "page", "p1"),
                    ContainerMembership("m2", "page", "p2"),
                ],
                "m3": []}[media_id]

    def get_media_tags(self, media_id):
        return {
            "m1": [],
            "m2": [TagMembership("m2", "t1", "u1")],
            "m3": [TagMembership("m3", "t2", "u1")],
        }[media_id]


def test_sync_unions_discovery_streams_and_hydrates_all_candidates():
    store = StateStore()
    result = SyncEngine(FakeAPI(), store).run(
        SelectionCriteria.from_values(page_ids=["p1"], tagged_user_ids=["u1"])
    )
    assert result.candidate_count == 3
    assert result.hydrated_count == 3
    assert {item.media_id for item in store.snapshot().media} == {"m1", "m2", "m3"}
    assert {item.media_id for item in store.snapshot().tags} == {"m2", "m3"}
    assert {
        item.container_id for item in store.snapshot().containers
        if item.media_id == "m1"
    } == {"p1"}


def test_sync_watches_specific_album_without_selecting_its_whole_page():
    class AlbumAPI(FakeAPI):
        def enumerate_album_media(self, album_id):
            assert album_id == "event-1"
            return ["m3"]

    store = StateStore()
    result = SyncEngine(AlbumAPI(), store).run(
        SelectionCriteria.from_values(album_ids=["event-1"])
    )

    assert result.album_media_count == 1
    assert result.page_media_count == 0
    assert result.selected_count == 1
    selected = store.snapshot().containers
    assert [(item.media_id, item.container_type, item.container_id) for item in selected] == [
        ("m3", "EVENT", "event-1")
    ]


def test_album_refresh_reconciles_memberships_without_rehydrating_existing_media():
    class AlbumAPI(FakeAPI):
        album_media = ["m1", "m2"]

        def enumerate_album_media(self, album_id):
            return self.album_media

    api = AlbumAPI()
    store = StateStore()
    engine = SyncEngine(api, store)
    criteria = SelectionCriteria.from_values(album_ids=["event-1"])
    assert engine.run(criteria).hydrated_count == 2

    api.album_media = ["m2", "m3"]
    refresh = engine.run(criteria)
    assert refresh.hydrated_count == 1
    assert refresh.selected_count == 2
    assert {
        item.media_id for item in store.snapshot().containers
        if item.container_id == "event-1"
    } == {"m2", "m3"}

    api.album_media = []
    assert engine.run(criteria).selected_count == 0
    assert any(
        item.container_id == "event-1" and item.removed_at is not None
        for item in store.snapshot(include_removed=True).containers
    )
    # Album reconciliation leaves unrelated page memberships intact.
    assert any(item.container_id == "p1" for item in store.snapshot().containers)


def test_failed_album_pagination_preserves_existing_memberships():
    class AlbumAPI(FakeAPI):
        fail = False

        def enumerate_album_media(self, album_id):
            yield "m1"
            if self.fail:
                raise RuntimeError("pagination failed")
            yield "m2"

    api = AlbumAPI()
    store = StateStore()
    engine = SyncEngine(api, store)
    criteria = SelectionCriteria.from_values(album_ids=["event-1"])
    engine.run(criteria)
    before = store.snapshot()
    api.fail = True
    with pytest.raises(RuntimeError, match="pagination failed"):
        engine.run(criteria)
    assert store.snapshot() == before


def test_album_dry_run_unions_sources_without_writing_state():
    class AlbumAPI(FakeAPI):
        def enumerate_album_media(self, album_id):
            return ["m2", "m3"]

    store = StateStore()
    result = SyncEngine(AlbumAPI(), store).run(
        SelectionCriteria.from_values(
            page_ids=["p1"], album_ids=["event-1"], tagged_user_ids=["u1"]
        ),
        dry_run=True,
    )
    assert result.candidate_count == result.selected_count == 3
    assert result.album_media_count == 2
    assert result.hydrated_count == 0
    assert store.snapshot().media == ()
    assert store.connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_repeat_sync_reconciles_ids_without_deep_hydrating_complete_items():
    class CountingAPI(FakeAPI):
        def __init__(self):
            self.calls = {"media": 0, "containers": 0, "tags": 0}

        def get_media(self, media_id):
            self.calls["media"] += 1
            return super().get_media(media_id)

        def get_media_containers(self, media_id):
            self.calls["containers"] += 1
            return super().get_media_containers(media_id)

        def get_media_tags(self, media_id):
            self.calls["tags"] += 1
            return super().get_media_tags(media_id)

    api = CountingAPI()
    store = StateStore()
    criteria = SelectionCriteria.from_values(page_ids=["p1"], tagged_user_ids=["u1"])

    first = SyncEngine(api, store).run(criteria)
    assert first.hydrated_count == 3
    assert api.calls == {"media": 3, "containers": 3, "tags": 3}

    second = SyncEngine(api, store).run(criteria)
    assert second.hydrated_count == 0
    assert second.selected_count == 3
    assert api.calls == {"media": 3, "containers": 3, "tags": 3}


def test_old_hydration_is_not_refreshed_by_default():
    class CountingAPI(FakeAPI):
        def __init__(self):
            self.media_calls = []

        def get_media(self, media_id):
            self.media_calls.append(media_id)
            return super().get_media(media_id)

    api = CountingAPI()
    store = StateStore()
    criteria = SelectionCriteria.from_values(page_ids=["p1"])
    SyncEngine(api, store).run(criteria)
    store.mark_media_hydrated("m1", datetime(2020, 1, 1, tzinfo=UTC))

    result = SyncEngine(api, store).run(criteria)
    assert result.hydrated_count == 0
    assert api.media_calls == ["m1", "m2"]


def test_failed_hydration_remains_incomplete_for_next_sync():
    class FlakyAPI(FakeAPI):
        def __init__(self):
            self.failed = False
            self.media_calls = []

        def get_media(self, media_id):
            self.media_calls.append(media_id)
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary failure")
            return super().get_media(media_id)

    api = FlakyAPI()
    store = StateStore()
    criteria = SelectionCriteria.from_values(page_ids=["p1"])
    first = SyncEngine(api, store).run(criteria)
    assert first.errors and first.hydrated_count == 1
    second = SyncEngine(api, store).run(criteria)
    assert second.hydrated_count == 1
    assert api.media_calls == ["m1", "m2", "m1"]


def test_complete_page_refresh_closes_removed_membership_but_keeps_history():
    api = FakeAPI()
    store = StateStore()
    criteria = SelectionCriteria.from_values(page_ids=["p1"])
    SyncEngine(api, store).run(criteria)
    # Override discovery only for this synthetic refresh.
    api.enumerate_page_media = lambda page_id: []
    SyncEngine(api, store).run(criteria)
    assert [item.container_id for item in store.snapshot().containers] == ["p2"]
    assert store.snapshot(include_removed=True).containers


def test_dry_run_does_not_write_state():
    store = StateStore()
    result = SyncEngine(FakeAPI(), store).run(
        SelectionCriteria.from_values(page_ids=["p1"]), dry_run=True
    )
    assert result.candidate_count == 2
    assert store.snapshot().media == ()
    assert store.connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_mapping_hydration_does_not_mislabel_upstream_creation_as_capture_time():
    media = _media_from_mapping(
        "m1",
        {
            "id": "m1",
            "type": "IMAGE",
            "createdAt": "2024-03-01T10:20:30.000Z",
            "width": 1600,
            "height": 1200,
            "originalDownloadUrl": "https://cdn.invalid/file?token=synthetic-secret",
            "accessToken": "synthetic-access-token",
        },
    )
    assert media.captured_at is None
    assert media.metadata == {"upstream_created_at": "2024-03-01T10:20:30.000Z"}
