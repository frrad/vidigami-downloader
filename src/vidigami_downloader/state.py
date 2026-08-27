"""SQLite persistence for canonical media, membership, and download state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .metadata import LocalMediaMetadata
from .models import ContainerMembership, MediaRecord, TagMembership, as_datetime, utc_now


def _ts(value: datetime | str | None = None) -> str:
    value = utc_now() if value is None else value
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        # EXIF timestamps often omit timezone information.  Preserve the
        # wall-clock value rather than fabricating UTC or local-machine time.
        return value.isoformat()
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    media: tuple[MediaRecord, ...]
    containers: tuple[ContainerMembership, ...]
    tags: tuple[TagMembership, ...]


class StateStore:
    """A small transactional store with append-like observation intervals."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
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
            );
            CREATE TABLE IF NOT EXISTS media_containers (
                media_id TEXT NOT NULL REFERENCES media(media_id),
                container_type TEXT NOT NULL,
                container_id TEXT NOT NULL,
                parent_page_id TEXT,
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                removed_at TEXT,
                PRIMARY KEY (media_id, container_type, container_id)
            );
            CREATE TABLE IF NOT EXISTS media_tags (
                media_id TEXT NOT NULL REFERENCES media(media_id),
                tag_id TEXT,
                user_id TEXT,
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                removed_at TEXT,
                FOREIGN KEY (media_id) REFERENCES media(media_id),
                CHECK (tag_id IS NOT NULL OR user_id IS NOT NULL)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS media_tags_identity
                ON media_tags(media_id, COALESCE(tag_id, ''), COALESCE(user_id, ''));
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                page_media_count INTEGER NOT NULL DEFAULT 0,
                tagged_media_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                hydrated_count INTEGER NOT NULL DEFAULT 0,
                selected_count INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS downloads (
                media_id TEXT PRIMARY KEY REFERENCES media(media_id),
                quality TEXT,
                local_path TEXT,
                byte_count INTEGER,
                sha256 TEXT,
                status TEXT NOT NULL,
                requested_at TEXT,
                completed_at TEXT,
                last_error TEXT
            );
            """
        )
        self.connection.commit()

    def begin_sync(self, run_id: str | None = None, at: datetime | None = None) -> str:
        run_id = run_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO sync_runs(run_id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, _ts(at)),
        )
        self.connection.commit()
        return run_id

    def finish_sync(
        self,
        run_id: str,
        *,
        status: str = "completed",
        page_media_count: int = 0,
        tagged_media_count: int = 0,
        candidate_count: int = 0,
        hydrated_count: int = 0,
        selected_count: int = 0,
        errors: Sequence[str] = (),
        at: datetime | None = None,
    ) -> None:
        self.connection.execute(
            """UPDATE sync_runs SET completed_at=?, status=?, page_media_count=?,
               tagged_media_count=?, candidate_count=?, hydrated_count=?,
               selected_count=?, errors_json=? WHERE run_id=?""",
            (
                _ts(at),
                status,
                page_media_count,
                tagged_media_count,
                candidate_count,
                hydrated_count,
                selected_count,
                json.dumps(list(errors)),
                run_id,
            ),
        )
        self.connection.commit()

    def upsert_media(self, media: MediaRecord, observed_at: datetime | None = None) -> None:
        now = _ts(observed_at)
        self.connection.execute(
            """INSERT INTO media(media_id,media_type,mime_type,filename,width,height,
               captured_at,metadata_json,first_observed_at,last_observed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_id) DO UPDATE SET
                 media_type=COALESCE(excluded.media_type,media.media_type),
                 mime_type=COALESCE(excluded.mime_type,media.mime_type),
                 filename=COALESCE(excluded.filename,media.filename),
                 width=COALESCE(excluded.width,media.width),
                 height=COALESCE(excluded.height,media.height),
                 captured_at=COALESCE(excluded.captured_at,media.captured_at),
                 metadata_json=CASE WHEN excluded.metadata_json='{}'
                   THEN media.metadata_json ELSE excluded.metadata_json END,
                 last_observed_at=excluded.last_observed_at""",
            (
                media.media_id,
                media.media_type,
                media.mime_type,
                media.filename,
                media.width,
                media.height,
                _ts(media.captured_at) if media.captured_at else None,
                json.dumps(dict(media.metadata), sort_keys=True),
                now,
                now,
            ),
        )

    def observe_containers(
        self,
        media_id: str,
        memberships: Iterable[ContainerMembership],
        *,
        authoritative: bool = False,
        observed_at: datetime | None = None,
    ) -> None:
        now = _ts(observed_at)
        values = [m for m in memberships]
        seen = {(m.container_type, m.container_id) for m in values}
        for membership in values:
            self.connection.execute(
                """INSERT INTO media_containers(media_id,container_type,container_id,
                   parent_page_id,first_observed_at,last_observed_at,removed_at)
                   VALUES(?,?,?,?,?,?,NULL)
                   ON CONFLICT(media_id,container_type,container_id) DO UPDATE SET
                     parent_page_id=excluded.parent_page_id,
                     last_observed_at=excluded.last_observed_at,
                     removed_at=NULL""",
                (
                    media_id,
                    membership.container_type,
                    membership.container_id,
                    membership.parent_page_id,
                    now,
                    now,
                ),
            )
        if authoritative:
            rows = self.connection.execute(
                """SELECT container_type, container_id FROM media_containers
                   WHERE media_id=? AND removed_at IS NULL""",
                (media_id,),
            ).fetchall()
            for row in rows:
                key = (row["container_type"], row["container_id"])
                if key not in seen:
                    self.connection.execute(
                        """UPDATE media_containers SET removed_at=? WHERE media_id=?
                           AND container_type=? AND container_id=?""",
                        (now, media_id, *key),
                    )

    def observe_tags(
        self,
        media_id: str,
        memberships: Iterable[TagMembership],
        *,
        authoritative: bool = False,
        observed_at: datetime | None = None,
    ) -> None:
        now = _ts(observed_at)
        values = list(memberships)
        seen = {(m.tag_id, m.user_id) for m in values}
        for membership in values:
            self.connection.execute(
                """INSERT INTO media_tags(media_id,tag_id,user_id,first_observed_at,
                   last_observed_at,removed_at) VALUES(?,?,?,?,?,NULL)
                   ON CONFLICT DO UPDATE SET last_observed_at=excluded.last_observed_at,
                   removed_at=NULL""",
                (media_id, membership.tag_id, membership.user_id, now, now),
            )
        if authoritative:
            rows = self.connection.execute(
                "SELECT tag_id,user_id FROM media_tags WHERE media_id=? AND removed_at IS NULL",
                (media_id,),
            ).fetchall()
            for row in rows:
                key = (row["tag_id"], row["user_id"])
                if key not in seen:
                    self.connection.execute(
                        """UPDATE media_tags SET removed_at=? WHERE media_id=?
                           AND tag_id IS ? AND user_id IS ?""",
                        (now, media_id, *key),
                    )

    def reconcile_page_discovery(
        self,
        page_id: str,
        media_ids: Iterable[str],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        """Close stale PAGE memberships after a complete page enumeration."""

        now = _ts(observed_at)
        ids = tuple(set(media_ids))
        if not ids:
            self.connection.execute(
                """UPDATE media_containers SET removed_at=?
                   WHERE LOWER(container_type)='page' AND container_id=? AND removed_at IS NULL""",
                (now, page_id),
            )
            return
        placeholders = ",".join("?" for _ in ids)
        self.connection.execute(
            f"""UPDATE media_containers SET removed_at=?
                WHERE LOWER(container_type)='page' AND container_id=?
                  AND removed_at IS NULL AND media_id NOT IN ({placeholders})""",
            (now, page_id, *ids),
        )

    def reconcile_tagged_user_discovery(
        self,
        user_id: str,
        media_ids: Iterable[str],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        """Close stale user-tag memberships after a complete tag enumeration."""

        now = _ts(observed_at)
        ids = tuple(set(media_ids))
        if not ids:
            self.connection.execute(
                "UPDATE media_tags SET removed_at=? WHERE user_id=? AND removed_at IS NULL",
                (now, user_id),
            )
            return
        placeholders = ",".join("?" for _ in ids)
        self.connection.execute(
            f"""UPDATE media_tags SET removed_at=?
                WHERE user_id=? AND removed_at IS NULL AND media_id NOT IN ({placeholders})""",
            (now, user_id, *ids),
        )

    def snapshot(self, *, include_removed: bool = False) -> StateSnapshot:
        removed = "" if include_removed else " AND removed_at IS NULL"
        media_rows = self.connection.execute("SELECT * FROM media ORDER BY media_id").fetchall()
        container_rows = self.connection.execute(
            f"SELECT * FROM media_containers WHERE 1=1{removed} ORDER BY media_id,container_id"
        ).fetchall()
        tag_rows = self.connection.execute(
            f"SELECT * FROM media_tags WHERE 1=1{removed} ORDER BY media_id,tag_id,user_id"
        ).fetchall()
        return StateSnapshot(
            tuple(
                MediaRecord(
                    media_id=row["media_id"],
                    media_type=row["media_type"],
                    mime_type=row["mime_type"],
                    filename=row["filename"],
                    width=row["width"],
                    height=row["height"],
                    captured_at=(
                        datetime.fromisoformat(row["captured_at"])
                        if row["captured_at"]
                        else None
                    ),
                    metadata=json.loads(row["metadata_json"]),
                )
                for row in media_rows
            ),
            tuple(
                ContainerMembership(
                    row["media_id"], row["container_type"], row["container_id"],
                    row["parent_page_id"],
                    as_datetime(row["first_observed_at"]),
                    as_datetime(row["last_observed_at"]),
                    as_datetime(row["removed_at"]),
                )
                for row in container_rows
            ),
            tuple(
                TagMembership(
                    row["media_id"], row["tag_id"], row["user_id"],
                    as_datetime(row["first_observed_at"]),
                    as_datetime(row["last_observed_at"]),
                    as_datetime(row["removed_at"]),
                )
                for row in tag_rows
            ),
        )

    def record_download(self, media_id: str, **values: object) -> None:
        allowed = {
            "quality", "local_path", "byte_count", "sha256", "status",
            "requested_at", "completed_at", "last_error",
        }
        fields = {key: value for key, value in values.items() if key in allowed}
        fields.setdefault("status", "pending")
        fields.setdefault("requested_at", _ts())
        columns = ["media_id", *fields]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{key}=excluded.{key}" for key in fields)
        self.connection.execute(
            f"INSERT INTO downloads({','.join(columns)}) VALUES({placeholders}) "
            f"ON CONFLICT(media_id) DO UPDATE SET {updates}",
            (media_id, *fields.values()),
        )
        self.connection.commit()

    def update_local_metadata(self, media_id: str, metadata: LocalMediaMetadata) -> bool:
        """Persist safe technical facts extracted from a completed local file.

        ``None`` values do not overwrite facts already known from another
        source, including a capture time.  The operation is idempotent and
        commits as one small transaction.
        """

        row = self.connection.execute(
            "SELECT mime_type,width,height,captured_at FROM media WHERE media_id=?",
            (media_id,),
        ).fetchone()
        if row is None:
            return False
        values: dict[str, object] = {
            "mime_type": metadata.mime_type,
            "width": metadata.width,
            "height": metadata.height,
            "captured_at": _ts(metadata.captured_at) if metadata.captured_at else None,
        }
        assignments: list[str] = []
        parameters: list[object] = []
        for field in ("mime_type", "width", "height", "captured_at"):
            if row[field] is None and values[field] is not None:
                assignments.append(f"{field}=?")
                parameters.append(values[field])
        if not assignments:
            return False
        parameters.append(media_id)
        self.connection.execute(
            f"UPDATE media SET {','.join(assignments)} WHERE media_id=?",
            parameters,
        )
        self.connection.commit()
        return True
