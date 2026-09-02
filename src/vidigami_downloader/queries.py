"""Small, named GraphQL documents used by the downloader.

These documents deliberately contain only fields required by the downloader.  They
are kept in a separate module so that a schema change is easy to review without
mixing it into transport or persistence code.
"""

GET_VIEWER = """
query GetViewer($spaceId: ID) {
  viewer(spaceId: $spaceId) {
    id
    relationshipsConnection(first: 1000) {
      edges { node { id } type }
    }
  }
}
""".strip()

GET_PAGES = """
query GetPages($spaceId: ID!, $first: Int!, $after: String) {
  space(id: $spaceId) {
    pagesConnection(first: $first, after: $after) {
      nodes { id name }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()

GET_PAGE_MEDIA_IDS = """
query GetPageMediaIds($pageId: ID!, $first: Int!, $after: String, $includeCollections: Boolean!) {
  page(id: $pageId) {
    mediaConnection(first: $first, after: $after, includeCollections: $includeCollections) {
      edges { node { id type } cursor }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()

GET_USER_MEDIA = """
query GetUserMedia(
  $after: String
  $before: String
  $first: Int!
  $orderBy: MediaOrder!
  $type: MediaType
  $userId: ID!
  $year: Int
) {
  user(id: $userId) {
    taggedMediaConnection(
      after: $after
      before: $before
      first: $first
      orderBy: $orderBy
      type: $type
      year: $year
    ) {
      edges { node { id type } cursor }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()

GET_LIGHTBOX_MEDIA_CONTAINERS = """
query GetLightboxMediaContainers($mediaIds: [ID!]!) {
  media(ids: $mediaIds) {
    id
    posts { id page { id } }
    events { id pagesConnection(first: 1) { nodes { id } } }
    collections { id legacyAlbum }
  }
}
""".strip()

GET_FACE_TAGS_ON_MEDIA = """
query GetFaceTagsOnMedia($mediaIds: [ID!]!, $includeModerated: Boolean!) {
  media(ids: $mediaIds, includeModerated: $includeModerated) {
    id
    createdAt
    faces(types: [USER_FACE, UNKNOWN_FACE]) {
      id
      hasCluster
      height
      user { id }
      width
      x
      y
    }
    height
    type
    width
  }
}
""".strip()

GET_MEDIA_DOWNLOADS = """
query GetMediaDownloads($mediaIds: [ID!]!) {
  media(ids: $mediaIds) {
    id
    createdAt
    height
    originalFileName
    type
    watermark
    width
    webDownloadUrl: downloadUrl(format: WEB)
    originalDownloadUrl: downloadUrl(format: ORIGINAL)
    printDownloadUrl: downloadUrl(format: PRINT)
  }
}
""".strip()


__all__ = [
    "GET_VIEWER",
    "GET_PAGES",
    "GET_PAGE_MEDIA_IDS",
    "GET_USER_MEDIA",
    "GET_LIGHTBOX_MEDIA_CONTAINERS",
    "GET_FACE_TAGS_ON_MEDIA",
    "GET_MEDIA_DOWNLOADS",
]
