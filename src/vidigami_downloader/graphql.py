"""Minimal Vidigami GraphQL client.

The client intentionally owns no application state.  Callers can persist the
returned IDs and observations in whatever local store they use.  Network tests
can inject ``transport`` and therefore never need a real account.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .queries import (
    GET_FACE_TAGS_ON_MEDIA,
    GET_LIGHTBOX_MEDIA_CONTAINERS,
    GET_MEDIA_DOWNLOADS,
    GET_PAGE_MEDIA_IDS,
    GET_USER_MEDIA,
    GET_VIEWER,
)


class GraphQLError(RuntimeError):
    """A valid HTTP response containing GraphQL errors."""

    def __init__(self, errors: Sequence[Mapping[str, Any]], operation: str):
        self.errors = list(errors)
        self.operation = operation
        messages = "; ".join(str(e.get("message", "GraphQL error")) for e in self.errors)
        super().__init__(f"{operation} failed: {messages}")


class GraphQLHTTPError(RuntimeError):
    """An HTTP or transport failure talking to the GraphQL endpoint."""


@dataclass(frozen=True)
class Viewer:
    id: str
    relationships: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class MediaRef:
    id: str


@dataclass(frozen=True)
class ContainerMembership:
    id: str
    media_id: str | None = None
    container_id: str | None = None
    container_type: str | None = None
    parent_page_id: str | None = None


@dataclass(frozen=True)
class FaceTag:
    id: str
    user_id: str | None = None
    media_id: str | None = None


@dataclass(frozen=True)
class DownloadOption:
    quality: str | None
    # Download URLs are short-lived credentials.  They are intentionally not
    # included if an option is accidentally rendered in a log or exception.
    url: str = field(repr=False)
    mime_type: str | None = None
    expires_at: str | None = None


Transport = Callable[[str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]]


class GraphQLClient:
    """Typed wrapper around the small query set used by the downloader."""

    def __init__(
        self,
        access_token: str | Callable[[], str],
        endpoint: str = "https://api.vidigami.com/graphql-sky-live",
        *,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 0.25,
        organization_id: str | None = None,
        organization_identifier: str | None = None,
        space_id: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._access_token = access_token
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.organization_id = organization_id
        self.organization_identifier = organization_identifier
        self.space_id = space_id
        self._transport = transport or self._http_transport

    def _token(self) -> str:
        token = self._access_token() if callable(self._access_token) else self._access_token
        if not token:
            raise GraphQLHTTPError("No access token is available; authenticate again")
        return token

    def request_headers(self) -> dict[str, str]:
        """Return transient headers required by Vidigami media endpoints.

        The returned mapping is created on demand and is never persisted by
        the client.  In particular, this keeps a refreshed access token out of
        GraphQL results and application state while allowing signed CDN URLs
        that require API authorization to be fetched.
        """

        headers = {"Authorization": f"Bearer {self._token()}"}
        if self.organization_id:
            headers["Organization-Id"] = self.organization_id
        elif self.organization_identifier:
            headers["x-org-identifier"] = self.organization_identifier
        if self.space_id:
            headers["Space-Id"] = self.space_id
        return headers

    # Kept as a descriptive alias for callers that only use the media API.
    def download_headers(self) -> dict[str, str]:
        return self.request_headers()

    def _http_transport(
        self,
        query: str,
        variables: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = Request(self.endpoint, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured HTTPS endpoint
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise GraphQLHTTPError("GraphQL endpoint returned an invalid response")
                return cast(Mapping[str, Any], payload)
        except HTTPError as exc:
            # Do not include response/request contents: they can contain bearer data.
            if exc.code in {401, 403}:
                raise GraphQLHTTPError(
                    "Vidigami rejected authentication; authenticate again"
                ) from exc
            if exc.code == 429 or 500 <= exc.code < 600:
                raise _RetryableHTTPError(f"GraphQL endpoint returned HTTP {exc.code}") from exc
            raise GraphQLHTTPError(f"GraphQL endpoint returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise _RetryableHTTPError("Could not reach the GraphQL endpoint") from exc

    def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation_name: str = "GraphQL operation",
    ) -> Mapping[str, Any]:
        variables = variables or {}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self.request_headers(),
        }
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                payload = self._transport(query, variables, headers, self.timeout)
                if not isinstance(payload, Mapping):
                    raise GraphQLHTTPError("GraphQL endpoint returned an invalid response")
                errors = payload.get("errors")
                if errors:
                    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
                        raise GraphQLError(errors, operation_name)
                    raise GraphQLError([{"message": str(errors)}], operation_name)
                data = payload.get("data")
                if not isinstance(data, Mapping):
                    raise GraphQLHTTPError(f"{operation_name} returned no data")
                return data
            except _RetryableHTTPError as exc:
                last = exc
                if attempt >= self.retries:
                    break
                if self.backoff:
                    time.sleep(self.backoff * (2**attempt))
        raise GraphQLHTTPError(str(last or "GraphQL request failed")) from last

    def get_viewer(self, space_id: str | None = None) -> Viewer:
        raw = self.execute(
            GET_VIEWER, {"spaceId": space_id}, operation_name="GetViewer"
        ).get("viewer")
        if not isinstance(raw, Mapping) or not raw.get("id"):
            raise GraphQLHTTPError("GetViewer returned no viewer")
        connection = raw.get("relationshipsConnection")
        relationships = _items(
            connection.get("edges") if isinstance(connection, Mapping) else None
        )
        return Viewer(
            id=str(raw["id"]),
            relationships=tuple(x for x in relationships if isinstance(x, Mapping)),
        )

    def get_page_media_ids(self, page_id: str, *, first: int = 100) -> list[str]:
        return [
            str(m["id"])
            for m in self._paginate(
                GET_PAGE_MEDIA_IDS,
                "GetPageMediaIds",
                "page",
                "mediaConnection",
                {"pageId": page_id, "includeCollections": True},
                first,
            )
        ]

    def get_user_media(
        self,
        user_id: str,
        *,
        first: int = 100,
        order_by: str = "RECENTLY_ADDED",
    ) -> list[str]:
        return [
            str(m["id"])
            for m in self._paginate(
                GET_USER_MEDIA,
                "GetUserMedia",
                "user",
                "taggedMediaConnection",
                {
                    "userId": user_id,
                    "orderBy": order_by,
                },
                first,
            )
        ]

    def get_lightbox_media_containers(
        self, media_ids: str | Sequence[str]
    ) -> dict[str, list[ContainerMembership]]:
        ids = [media_ids] if isinstance(media_ids, str) else list(media_ids)
        if not ids:
            return {}
        raw = self.execute(
            GET_LIGHTBOX_MEDIA_CONTAINERS,
            {"mediaIds": ids},
            operation_name="GetLightboxMediaContainers",
        ).get("media")
        result: dict[str, list[ContainerMembership]] = {str(i): [] for i in ids}
        for media in raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ():
            if not isinstance(media, Mapping) or not media.get("id"):
                continue
            media_id = str(media["id"])
            memberships: list[ContainerMembership] = []
            for post in _items(media.get("posts")):
                page = post.get("page") if isinstance(post, Mapping) else None
                if isinstance(post, Mapping) and post.get("id"):
                    page_id = (
                        str(page["id"])
                        if isinstance(page, Mapping) and page.get("id")
                        else None
                    )
                    memberships.append(
                        ContainerMembership(
                            id=str(post["id"]),
                            media_id=media_id,
                            container_id=str(post["id"]),
                            container_type="post",
                            parent_page_id=page_id,
                        )
                    )
                    if page_id:
                        memberships.append(
                            ContainerMembership(
                                id=page_id,
                                media_id=media_id,
                                container_id=page_id,
                                container_type="page",
                                parent_page_id=page_id,
                            )
                        )
            for event in _items(media.get("events")):
                if not isinstance(event, Mapping) or not event.get("id"):
                    continue
                pages = event.get("pagesConnection")
                for page in _items(pages.get("nodes") if isinstance(pages, Mapping) else None):
                    if isinstance(page, Mapping) and page.get("id"):
                        memberships.append(
                            ContainerMembership(
                                id=str(page["id"]),
                                media_id=media_id,
                                container_id=str(event["id"]),
                                container_type="event",
                                parent_page_id=str(page["id"]),
                            )
                        )
            for collection in _items(media.get("collections")):
                if isinstance(collection, Mapping) and collection.get("id"):
                    memberships.append(
                        ContainerMembership(
                            id=str(collection["id"]),
                            media_id=media_id,
                            container_id=str(collection["id"]),
                            container_type="collection",
                        )
                    )
            result[media_id] = memberships
        return result

    def get_face_tags_on_media(
        self, media_ids: str | Sequence[str], *, include_moderated: bool = True
    ) -> dict[str, list[FaceTag]]:
        ids = [media_ids] if isinstance(media_ids, str) else list(media_ids)
        if not ids:
            return {}
        raw = self._face_tag_media(ids, include_moderated=include_moderated)
        result: dict[str, list[FaceTag]] = {str(i): [] for i in ids}
        for media in raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ():
            if not isinstance(media, Mapping) or not media.get("id"):
                continue
            media_id = str(media["id"])
            for value in _items(media.get("faces")):
                if not isinstance(value, Mapping) or not value.get("id"):
                    continue
                user = value.get("user")
                user_id = user.get("id") if isinstance(user, Mapping) else None
                result.setdefault(media_id, []).append(
                    FaceTag(id=str(value["id"]), user_id=_string(user_id), media_id=media_id)
                )
        return result

    def get_media_downloads(
        self, media_ids: str | Sequence[str]
    ) -> dict[str, tuple[Mapping[str, Any], list[DownloadOption]]]:
        ids = [media_ids] if isinstance(media_ids, str) else list(media_ids)
        if not ids:
            return {}
        raw = self.execute(
            GET_MEDIA_DOWNLOADS, {"mediaIds": ids}, operation_name="GetMediaDownloads"
        ).get("media")
        result: dict[str, tuple[Mapping[str, Any], list[DownloadOption]]] = {}
        for item in raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ():
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            options: list[DownloadOption] = []
            fields = (
                ("original", "originalDownloadUrl"),
                ("web", "webDownloadUrl"),
                ("print", "printDownloadUrl"),
            )
            for quality, key in fields:
                if item.get(key):
                    options.append(DownloadOption(quality=quality, url=str(item[key])))
            result[str(item["id"])] = (item, options)
        return result

    # The following adapter methods implement the small interface consumed by
    # SyncEngine.  Batch methods above remain available for efficient hydration.
    def enumerate_page_media(self, page_id: str) -> list[str]:
        return self.get_page_media_ids(page_id)

    def enumerate_tagged_media(self, user_id: str) -> list[str]:
        return self.get_user_media(user_id)

    def get_media(self, media_id: str) -> Any:
        from .models import MediaRecord

        batch = self.get_media_downloads([media_id])
        item = batch.get(media_id)
        if item is None:
            raise GraphQLHTTPError(f"Media {media_id} was not returned")
        raw, _ = item
        captured = raw.get("capturedAt")
        try:
            captured_at = (
                datetime.fromisoformat(str(captured).replace("Z", "+00:00")) if captured else None
            )
        except ValueError:
            captured_at = None
        return MediaRecord(
            media_id=media_id,
            media_type=_string(raw.get("type")),
            filename=_string(raw.get("originalFileName")),
            width=_integer(raw.get("width")),
            height=_integer(raw.get("height")),
            captured_at=captured_at,
            metadata=(
                {"upstream_created_at": _string(raw.get("createdAt"))}
                if raw.get("createdAt") is not None
                else {}
            ),
        )

    def _face_tag_media(
        self, ids: Sequence[str], *, include_moderated: bool
    ) -> Any:
        return self.execute(
            GET_FACE_TAGS_ON_MEDIA,
            {"mediaIds": list(ids), "includeModerated": include_moderated},
            operation_name="GetFaceTagsOnMedia",
        ).get("media")

    def get_media_containers(self, media_id: str) -> list[Any]:
        from .models import ContainerMembership as StateContainerMembership

        memberships = self.get_lightbox_media_containers([media_id]).get(media_id, [])
        return [
            StateContainerMembership(
                media_id=media_id,
                container_type=(membership.container_type or "UNKNOWN").upper(),
                container_id=membership.container_id or membership.id,
                parent_page_id=membership.parent_page_id,
            )
            for membership in memberships
        ]

    def get_media_tags(self, media_id: str) -> list[Any]:
        from .models import TagMembership

        return [
            TagMembership(media_id=media_id, tag_id=tag.id, user_id=tag.user_id)
            for tag in self.get_face_tags_on_media([media_id]).get(media_id, [])
        ]

    def _paginate(
        self,
        query: str,
        operation_name: str,
        root_key: str,
        connection_key: str,
        variables: Mapping[str, Any],
        first: int,
    ) -> list[Any]:
        if first < 1:
            raise ValueError("first must be positive")
        cursor: str | None = None
        result: list[Any] = []
        while True:
            values = dict(variables)
            values.update(first=first, after=cursor)
            root = self.execute(query, values, operation_name=operation_name).get(root_key)
            if not isinstance(root, Mapping):
                return result
            connection = root.get(connection_key)
            if not isinstance(connection, Mapping):
                return result
            edges = connection.get("edges")
            if isinstance(edges, Sequence) and not isinstance(edges, (str, bytes)):
                for edge in edges:
                    if isinstance(edge, Mapping):
                        node = edge.get("node", edge)
                        if node is not None:
                            result.append(
                                node if isinstance(node, Mapping) else MediaRef(str(node))
                            )
            elif isinstance(connection.get("nodes"), Sequence):
                result.extend(connection["nodes"])
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
                return result
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise GraphQLHTTPError(f"{operation_name} returned a non-advancing cursor")
            cursor = str(next_cursor)


class _RetryableHTTPError(GraphQLHTTPError):
    pass


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _items(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        value = value.get("nodes") or value.get("edges") or ()
    return (
        list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
    )


__all__ = [
    "ContainerMembership",
    "DownloadOption",
    "FaceTag",
    "GraphQLClient",
    "GraphQLError",
    "GraphQLHTTPError",
    "MediaRef",
    "Viewer",
]
