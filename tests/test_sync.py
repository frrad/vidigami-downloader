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
