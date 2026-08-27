from __future__ import annotations

from vidigami_downloader.graphql import GraphQLClient, GraphQLError


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
        "orderBy": "CAPTURED_AT_DESC",
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


def test_graphql_errors_are_not_silently_accepted():
    transport = FakeTransport([{"errors": [{"message": "synthetic failure"}]}])
    client = GraphQLClient("synthetic-access", transport=transport, backoff=0)
    try:
        client.get_page_media_ids("page-1")
    except GraphQLError as exc:
        assert "synthetic failure" in str(exc)
    else:
        raise AssertionError("expected GraphQLError")
