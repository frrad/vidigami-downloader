from __future__ import annotations

import pytest

from vidigami_downloader.graphql import (
    DownloadOption,
    GraphQLClient,
    GraphQLError,
    GraphQLHTTPError,
    Page,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, query, variables, headers, timeout):
        self.calls.append((query, dict(variables), dict(headers), timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_page_media_paginates_and_sends_collection_flag():
    transport = FakeTransport(
        [
            {
                "data": {
                    "page": {
                        "mediaConnection": {
                            "edges": [{"node": {"id": "media-1", "type": "IMAGE"}}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            },
            {
                "data": {
                    "page": {
                        "mediaConnection": {
                            "edges": [{"node": {"id": "media-2", "type": "IMAGE"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                        }
                    }
                }
            },
        ]
    )
    client = GraphQLClient(
        "synthetic-access",
        transport=transport,
        backoff=0,
        organization_id="org-1",
        space_id="space-1",
    )

    assert client.get_page_media_ids("page-1", first=1) == ["media-1", "media-2"]
    assert transport.calls[0][1] == {
        "pageId": "page-1",
        "includeCollections": True,
        "first": 1,
        "after": None,
    }
    assert transport.calls[1][1]["after"] == "cursor-1"
    assert transport.calls[0][2]["Authorization"] == "Bearer synthetic-access"
    assert transport.calls[0][2]["Organization-Id"] == "org-1"
    assert transport.calls[0][2]["Space-Id"] == "space-1"


def test_pages_paginates_nodes_and_passes_space_id():
    transport = FakeTransport(
        [
            {
                "data": {
                    "space": {
                        "pagesConnection": {
                            "nodes": [{"id": "page-1", "name": "First Page"}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            },
            {
                "data": {
                    "space": {
                        "pagesConnection": {
                            "nodes": [{"id": "page-2", "name": "Second Page"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                        }
                    }
                }
            },
        ]
    )
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)

    assert client.get_pages("space-1", first=1) == [
        Page(id="page-1", name="First Page"),
        Page(id="page-2", name="Second Page"),
    ]
    assert transport.calls[0][1] == {"spaceId": "space-1", "first": 1, "after": None}
    assert transport.calls[1][1] == {"spaceId": "space-1", "first": 1, "after": "cursor-1"}
    assert "space(id: $spaceId)" in transport.calls[0][0]
    assert "pagesConnection(first: $first, after: $after)" in transport.calls[0][0]
    assert "nodes { id name }" in transport.calls[0][0]


def test_pages_uses_client_space_id_and_requires_one():
    transport = FakeTransport(
        [
            {
                "data": {
                    "space": {
                        "pagesConnection": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        ]
    )
    client = GraphQLClient(
        "synthetic-access", transport=transport, backoff=0, space_id="configured-space"
    )

    assert client.get_pages() == []
    assert transport.calls[0][1]["spaceId"] == "configured-space"

    with pytest.raises(GraphQLHTTPError, match="space_id is required"):
        GraphQLClient("synthetic-access", transport=FakeTransport([]), backoff=0).get_pages()


@pytest.mark.parametrize(
    "space_value, error_text",
    [(None, "no space"), ({}, "no pagesConnection")],
)
def test_pages_rejects_missing_space_connection_root(space_value, error_text):
    transport = FakeTransport([{"data": {"space": space_value}}])
    client = GraphQLClient(
        "synthetic-access", transport=transport, backoff=0, space_id="space-1"
    )

    with pytest.raises(GraphQLHTTPError, match=error_text):
        client.get_pages()


@pytest.mark.parametrize("page_info", [None, {}, {"hasNextPage": "false"}])
def test_pages_rejects_malformed_or_missing_page_info(page_info):
    transport = FakeTransport(
        [
            {
                "data": {
                    "space": {
                        "pagesConnection": {
                            "nodes": [{"id": "page-1", "name": "Page One"}],
                            "pageInfo": page_info,
                        }
                    }
                }
            }
        ]
    )
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0, space_id="space-1")

    with pytest.raises(GraphQLHTTPError, match="pageInfo"):
        client.get_pages()


def test_pages_rejects_space_header_conflict_and_allows_explicit_without_header():
    conflict_client = GraphQLClient(
        "synthetic-access", transport=FakeTransport([]), backoff=0, space_id="space-1"
    )
    with pytest.raises(GraphQLHTTPError, match="conflicts"):
        conflict_client.get_pages("space-2")

    transport = FakeTransport(
        [
            {
                "data": {
                    "space": {
                        "pagesConnection": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        ]
    )
    no_header_client = GraphQLClient("synthetic-access", transport=transport, backoff=0)
    assert no_header_client.get_pages("space-2") == []
    assert transport.calls[0][1]["spaceId"] == "space-2"
    assert "Space-Id" not in transport.calls[0][2]


def test_org_identifier_is_used_until_canonical_org_id_is_known():
    transport = FakeTransport(
        [{"data": {"viewer": {"id": "viewer-1", "relationshipsConnection": {"edges": []}}}}]
    )
    client = GraphQLClient(
        "synthetic-access",
        transport=transport,
        organization_identifier="example-school",
    )
    assert client.get_viewer().id == "viewer-1"
    assert transport.calls[0][2]["x-org-identifier"] == "example-school"
    assert "Organization-Id" not in transport.calls[0][2]


def test_download_headers_include_auth_and_context_without_option_repr_url():
    client = GraphQLClient(
        "synthetic-access",
        organization_id="org-1",
        space_id="space-1",
    )
    assert client.request_headers() == {
        "Authorization": "Bearer synthetic-access",
        "Organization-Id": "org-1",
        "Space-Id": "space-1",
    }

    value = DownloadOption("original", "https://cdn.invalid/signed?token=secret")
    assert "token=secret" not in repr(value)


def test_tagged_media_sets_current_connection_arguments():
    transport = FakeTransport(
        [
            {
                "data": {
                    "user": {
                        "taggedMediaConnection": {
                            "edges": [{"node": {"id": "media-1", "type": "IMAGE"}}],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        ]
    )
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)

    assert client.enumerate_tagged_media("user-1") == ["media-1"]
    variables = transport.calls[0][1]
    assert variables == {
        "userId": "user-1",
        "orderBy": "RECENTLY_ADDED",
        "first": 100,
        "after": None,
    }


def test_batch_hydration_preserves_ids_and_download_urls():
    transport = FakeTransport(
        [
            {
                "data": {
                    "media": [
                        {
                            "id": "media-1",
                            "posts": [{"id": "post-1", "page": {"id": "page-1"}}],
                            "events": [],
                            "collections": [],
                        },
                        {
                            "id": "media-2",
                            "posts": [],
                            "events": [],
                            "collections": [],
                        },
                    ]
                }
            },
            {
                "data": {
                    "media": [
                        {
                            "id": "media-1",
                            "faces": [{"id": "face-1", "user": {"id": "user-1"}}],
                        }
                    ]
                }
            },
            {
                "data": {
                    "media": [
                        {
                            "id": "media-1",
                            "originalFileName": "photo.jpg",
                            "type": "IMAGE",
                            "originalDownloadUrl": "https://download.invalid/original",
                            "webDownloadUrl": "https://download.invalid/web",
                        }
                    ]
                }
            },
        ]
    )
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)

    containers = client.get_lightbox_media_containers(["media-1", "media-2"])
    tags = client.get_face_tags_on_media(["media-1"])
    downloads = client.get_media_downloads(["media-1"])
    assert {(item.container_type, item.container_id, item.parent_page_id)
            for item in containers["media-1"]} == {
        ("post", "post-1", "page-1"),
        ("page", "page-1", "page-1"),
    }
    assert tags["media-1"][0].user_id == "user-1"
    assert [option.quality for option in downloads["media-1"][1]] == ["original", "web"]


def test_media_hydration_preserves_dimensions_and_distinguishes_upstream_created_at():
    transport = FakeTransport(
        [
            {
                "data": {
                    "media": [
                        {
                            "id": "media-1",
                            "createdAt": "2024-03-01T10:20:30.000Z",
                            "height": 1200,
                            "originalFileName": "photo.jpg",
                            "type": "IMAGE",
                            "originalDownloadUrl": "https://download.invalid/original",
                            "width": 1600,
                        }
                    ]
                }
            },
        ]
    )
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)

    media = client.get_media("media-1")

    assert media.width == 1600
    assert media.height == 1200
    assert media.metadata == {"upstream_created_at": "2024-03-01T10:20:30.000Z"}
    assert media.captured_at is None
    assert len(transport.calls) == 1
    assert all(field in transport.calls[0][0] for field in ("createdAt", "height", "width"))


def test_graphql_errors_are_not_silently_accepted():
    transport = FakeTransport([{"errors": [{"message": "synthetic failure"}]}])
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)
    try:
        client.get_page_media_ids("page-1")
    except GraphQLError as exc:
        assert "synthetic failure" in str(exc)
    else:
        raise AssertionError("expected GraphQLError")
