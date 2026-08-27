from vidigami_downloader.models import (
    ContainerMembership,
    MediaRecord,
    SelectionCriteria,
    TagMembership,
)
from vidigami_downloader.selectors import select_media


def test_selection_is_derived_from_ids_and_can_change_without_metadata_changes():
    media = [MediaRecord("m1"), MediaRecord("m2"), MediaRecord("m3")]
    containers = [
        ContainerMembership("m1", "page", "p1"),
        ContainerMembership("m2", "album", "a1", parent_page_id="p2"),
        ContainerMembership("m3", "page", "p9"),
    ]
    tags = [TagMembership("m2", "t1", "u1"), TagMembership("m3", "t2", "u2")]
    first = select_media(
        media, containers, tags,
        SelectionCriteria.from_values(page_ids=["p1"], tagged_user_ids=["u1"]),
    )
    assert [(x.media.media_id, x.matched_page_ids, x.matched_tag_user_ids) for x in first] == [
        ("m1", ("p1",), ()),
        ("m2", (), ("u1",)),
    ]
    second = select_media(
        media, containers, tags,
        SelectionCriteria.from_values(page_ids=["p9"], tagged_user_ids=["u2"]),
    )
    assert [x.media.media_id for x in second] == ["m3"]
    assert containers[0].container_id == "p1"
