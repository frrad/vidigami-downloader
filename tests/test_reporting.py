import csv
import json

from vidigami_downloader.models import (
    ContainerMembership,
    MediaRecord,
    SelectedMedia,
    SelectionCriteria,
    TagMembership,
)
from vidigami_downloader.reporting import (
    build_rows,
    rows_from_snapshot,
    write_csv,
    write_json,
)
from vidigami_downloader.state import StateStore


def test_report_keeps_canonical_page_container_and_face_tag_ids():
    selected = [
        SelectedMedia(
            MediaRecord("media-1", metadata={"displayName": "private name"}),
            matched_page_ids=("page-1",),
            matched_tag_user_ids=("user-1",),
        )
    ]
    containers = [
        ContainerMembership("media-1", "page", "page-1"),
        ContainerMembership("media-1", "album", "album-2", parent_page_id="page-2"),
        ContainerMembership("media-1", "album", "album-2", parent_page_id="page-2"),
    ]
    tags = [
        TagMembership("media-1", "tag-1", "user-1"),
        TagMembership("media-1", "tag-2", "user-2"),
        TagMembership("media-1", "tag-2", "user-2"),
    ]

    row = build_rows(selected, containers, tags)[0]

    assert row["media_id"] == "media-1"
    assert row["page_ids"] == ["page-1", "page-2"]
    assert row["container_ids"] == ["album-2", "page-1"]
    assert row["containers"] == [
        {"container_type": "album", "container_id": "album-2", "parent_page_id": "page-2"},
        {"container_type": "page", "container_id": "page-1", "parent_page_id": None},
    ]
    assert row["face_tags"] == [
        {"tag_id": "tag-1", "tagged_user_id": "user-1"},
        {"tag_id": "tag-2", "tagged_user_id": "user-2"},
    ]
    assert row["matched_page_ids"] == ["page-1"]
    assert row["matched_tagged_user_ids"] == ["user-1"]
    assert "page" not in row
    assert "tags" not in row
    assert "selected" not in row
    assert "private name" not in json.dumps(row)


def test_rows_from_snapshot_selection_is_derived_from_current_ids():
    store = StateStore()
    store.upsert_media(MediaRecord("media-1"))
    store.upsert_media(MediaRecord("media-2"))
    store.observe_containers(
        "media-1", [ContainerMembership("media-1", "page", "page-1")], authoritative=True
    )
    store.observe_containers(
        "media-2",
        [ContainerMembership("media-2", "album", "album-2", "page-2")],
        authoritative=True,
    )
    store.observe_tags("media-2", [TagMembership("media-2", "tag-2", "user-2")], authoritative=True)
    store.connection.commit()

    first = rows_from_snapshot(
        store.snapshot(),
        SelectionCriteria.from_values(page_ids=["page-1"]),
    )
    second = rows_from_snapshot(
        store.snapshot(),
        SelectionCriteria.from_values(tagged_user_ids=["user-2"]),
    )

    assert [row["media_id"] for row in first] == ["media-1"]
    assert [row["media_id"] for row in second] == ["media-2"]
    assert second[0]["containers"] == [
        {"container_type": "album", "container_id": "album-2", "parent_page_id": "page-2"}
    ]
    assert second[0]["face_tags"] == [{"tag_id": "tag-2", "tagged_user_id": "user-2"}]
    store.close()


def test_json_and_csv_reports_are_lossless_for_id_fields(tmp_path):
    rows = [
        {
            "media_id": "media-1",
            "page_ids": ["page-1"],
            "container_ids": ["page-1", "album-1"],
            "containers": [
                {"container_type": "page", "container_id": "page-1", "parent_page_id": None}
            ],
            "face_tags": [{"tag_id": "tag-1", "tagged_user_id": "user-1"}],
            "matched_page_ids": ["page-1"],
            "matched_tagged_user_ids": [],
        }
    ]
    json_path = write_json(tmp_path / "reports" / "media.json", rows)
    csv_path = write_csv(tmp_path / "reports" / "media.csv", rows)

    assert json.loads(json_path.read_text(encoding="utf-8")) == rows
    with csv_path.open(newline="", encoding="utf-8") as stream:
        csv_row = next(csv.DictReader(stream))
    assert json.loads(csv_row["page_ids"]) == ["page-1"]
    assert json.loads(csv_row["containers"])[0]["container_id"] == "page-1"
    assert json.loads(csv_row["face_tags"])[0] == {
        "tag_id": "tag-1",
        "tagged_user_id": "user-1",
    }
