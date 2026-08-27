"""Configuration helpers for the local Vidigami Downloader.

Configuration is deliberately ID-only: names and email addresses do not belong
in the persisted selection model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VidigamiConfig:
    """Opaque IDs and local settings used by the downloader."""

    organization_id: str
    space_id: str
    page_ids: tuple[str, ...] = ()
    tagged_user_ids: tuple[str, ...] = ()
    include_shared_collections: bool = True
    download_quality: str = "original"
    media_types: tuple[str, ...] = ("IMAGE", "VIDEO")


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Paths for local state and generated output."""

    database_path: Path = Path("state/vidigami.sqlite3")
    archive_directory: Path = Path("archive")
    reports_directory: Path = Path("reports")


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """HTTP behavior defaults."""

    request_timeout_seconds: int = 30
    max_retries: int = 3
    concurrency: int = 4


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete local configuration, with safe synthetic defaults."""

    vidigami: VidigamiConfig = field(
        default_factory=lambda: VidigamiConfig(
            organization_id="org|example-001",
            space_id="space|example-001",
        )
    )
    storage: StorageConfig = field(default_factory=StorageConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)


def load_config(path: Path | None = None) -> AppConfig:
    """Load TOML configuration when available, otherwise return safe defaults.

    Parsing is kept dependency-free for the scaffold. The full API client will
    add strict validation before using credentials or making requests.
    """

    config_path = path or Path("config.toml")
    if not config_path.exists():
        return AppConfig()

    import tomllib

    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    vidigami_raw = raw.get("vidigami", {})
    storage_raw = raw.get("storage", {})
    network_raw = raw.get("network", {})

    return AppConfig(
        vidigami=VidigamiConfig(
            organization_id=str(vidigami_raw.get("organization_id", "")),
            space_id=str(vidigami_raw.get("space_id", "")),
            page_ids=tuple(str(value) for value in vidigami_raw.get("page_ids", [])),
            tagged_user_ids=tuple(
                str(value) for value in vidigami_raw.get("tagged_user_ids", [])
            ),
            include_shared_collections=bool(
                vidigami_raw.get("include_shared_collections", True)
            ),
            download_quality=str(vidigami_raw.get("download_quality", "original")),
            media_types=tuple(
                str(value) for value in vidigami_raw.get("media_types", ["IMAGE", "VIDEO"])
            ),
        ),
        storage=StorageConfig(
            database_path=Path(storage_raw.get("database_path", "state/vidigami.sqlite3")),
            archive_directory=Path(storage_raw.get("archive_directory", "archive")),
            reports_directory=Path(storage_raw.get("reports_directory", "reports")),
        ),
        network=NetworkConfig(
            request_timeout_seconds=int(network_raw.get("request_timeout_seconds", 30)),
            max_retries=int(network_raw.get("max_retries", 3)),
            concurrency=int(network_raw.get("concurrency", 4)),
        ),
    )
