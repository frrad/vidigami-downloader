from datetime import UTC, datetime

from vidigami_downloader.models import ContainerMembership, MediaRecord, TagMembership
from vidigami_downloader.state import StateStore


def test_memberships_keep_ids_and_observation_intervals():
    store = StateStore()
    at = datetime(2025, 1, 1, tzinfo=UTC)
    store.upsert_media(MediaRecord("m1", filename="photo.jpg"), at)
    store.observe_containers(
        "m1", [ContainerMembership("m1", "page", "p1")],
        authoritative=True, observed_at=at,
    )
    store.observe_tags(
        "m1", [TagMembership("m1", "tag1", "u1")],
        authoritative=True, observed_at=at,
    )
    store.connection.commit()
    snapshot = store.snapshot()
    assert snapshot.containers[0].container_id == "p1"
    assert snapshot.tags[0].tag_id == "tag1"
    assert not hasattr(snapshot.media[0], "page")

    later = datetime(2025, 1, 2, tzinfo=UTC)
    store.observe_containers("m1", [], authoritative=True, observed_at=later)
    store.observe_tags("m1", [], authoritative=True, observed_at=later)
    store.connection.commit()
    assert store.snapshot().containers == ()
    historical = store.snapshot(include_removed=True)
    assert historical.containers[0].container_id == "p1"
    assert historical.containers[0].removed_at is not None
    assert historical.tags[0].user_id == "u1"
    assert historical.tags[0].removed_at is not None

    store.observe_containers(
        "m1", [ContainerMembership("m1", "page", "p1")],
        authoritative=True, observed_at=later,
    )
    store.connection.commit()
    assert store.snapshot().containers[0].container_id == "p1"


def test_download_record_is_separate_from_membership_metadata():
    store = StateStore()
    store.upsert_media(MediaRecord("m1"))
    store.record_download("m1", quality="original", status="complete", local_path="archive/m1.jpg")
    row = store.connection.execute(
        "SELECT quality,status FROM downloads WHERE media_id='m1'"
    ).fetchone()
    assert tuple(row) == ("original", "complete")
