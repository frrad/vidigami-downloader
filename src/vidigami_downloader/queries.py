"""Small, named GraphQL documents used by the downloader.

These documents deliberately contain only fields required by the downloader.  They
are kept in a separate module so that a schema change is easy to review without
mixing it into transport or persistence code.
"""

GET_VIEWER = """
query GetViewer {
  viewer {
    id
    relationships {
      id
      user { id }
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
  $userId: ID!, $first: Int!, $after: String, $format: MediaFormat!, $orderBy: MediaOrder!,
  $savedMedia: Boolean!, $taggedMedia: Boolean!, $uploadsMedia: Boolean!, $workMedia: Boolean!
) {
  user(id: $userId) {
    taggedMediaConnection(
      first: $first, after: $after, format: $format, orderBy: $orderBy,
      savedMedia: $savedMedia, taggedMedia: $taggedMedia,
      uploadsMedia: $uploadsMedia, workMedia: $workMedia
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
    posts { id createdAt page { id name space { id } } }
    events { id name pagesConnection(first: 1) { nodes { id space { id } } } }
    collections { id name legacyAlbum }
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
    originalFileName
    type
    watermark: downloadUrl(format: WEB)
    webDownloadUrl: downloadUrl(format: WEB)
    originalDownloadUrl: downloadUrl(format: ORIGINAL)
    printDownloadUrl: downloadUrl(format: PRINT)
  }
}
""".strip()


__all__ = [
    "GET_VIEWER",
    "GET_PAGE_MEDIA_IDS",
    "GET_USER_MEDIA",
    "GET_LIGHTBOX_MEDIA_CONTAINERS",
    "GET_FACE_TAGS_ON_MEDIA",
    "GET_MEDIA_DOWNLOADS",
]
