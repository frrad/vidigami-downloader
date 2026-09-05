import sqlite3
from datetime import UTC, datetime

from vidigami_downloader.models import ContainerMembership, MediaRecord, TagMembership
from vidigami_downloader.state import StateStore


def test_schema_migration_marks_legacy_media_as_already_hydrated(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE media (
            media_id TEXT PRIMARY KEY,
            media_type TEXT,
            mime_type TEXT,
            filename TEXT,
            width INTEGER,
            height INTEGER,
            captured_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL
        )"""
    )
    observed_at = "2026-09-05T01:57:29+00:00"
    connection.execute(
        """INSERT INTO media(
            media_id, metadata_json, first_observed_at, last_observed_at
        ) VALUES('m1', '{}', ?, ?)""",
        (observed_at, observed_at),
    )
    connection.commit()
    connection.close()

    store = StateStore(path)
    row = store.connection.execute(
        "SELECT last_hydrated_at FROM media WHERE media_id='m1'"
    ).fetchone()
    assert row["last_hydrated_at"] == observed_at
    assert store.media_needs_hydration("m1") is False
    store.close()


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


def test_page_reconciliation_closes_memberships_nested_under_removed_page():
    store = StateStore()
    store.upsert_media(MediaRecord("m1"))
    store.observe_containers(
        "m1", [ContainerMembership("m1", "post", "post-1", parent_page_id="p1")],
        authoritative=True,
    )
    store.reconcile_page_discovery("p1", [])
    assert store.snapshot().containers == ()
    assert store.snapshot(include_removed=True).containers[0].removed_at is not None
