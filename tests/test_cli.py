from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from vidigami_downloader import cli_impl
from vidigami_downloader.cli import app
from vidigami_downloader.graphql import Viewer
from vidigami_downloader.models import ContainerMembership, MediaRecord, SyncResult, TagMembership
from vidigami_downloader.service import DownloadSummary, SyncSummary
from vidigami_downloader.state import StateStore

runner = CliRunner()


def _write_config(path: Path, database: Path, *, secret: str = "synthetic-secret") -> Path:
    archive = database.parent / "archive"
    reports = database.parent / "reports"
    path.write_text(
        f'''
[vidigami]
organization_id = "org|synthetic"
space_id = "space|synthetic"
page_ids = ["page|synthetic"]

[storage]
database_path = "{database}"
archive_directory = "{archive}"
reports_directory = "{reports}"

[auth]
client_secret = "{secret}"
''',
        encoding="utf-8",
    )
    return path


def test_doctor_loads_config_without_printing_secret(tmp_path: Path) -> None:
    secret = "synthetic-secret-do-not-print"
    config_path = _write_config(tmp_path / "config.toml", tmp_path / "state.sqlite3", secret=secret)

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": True}
    assert secret not in result.output


def test_relationships_emits_only_opaque_ids(monkeypatch: Any, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path / "state.sqlite3")

    class FakeGraph:
        def get_viewer(self, space_id: str):
            assert space_id == "space|synthetic"
            return Viewer(
                id="viewer|synthetic",
                relationships=(
                    {
                        "node": {
                            "id": "user|one",
                            "name": "Private Person",
                            "email": "person@example.invalid",
                        }
                    },
                    {
                        "id": "user|two",
                        "name": "Another Private Person",
                        "email": "another@example.invalid",
                    },
                    {"node": {"name": "missing-id", "email": "missing@example.invalid"}},
                ),
            )

    monkeypatch.setattr(cli_impl, "graph_client", lambda config: FakeGraph())
    result = runner.invoke(app, ["relationships", "--config", str(config_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "relationship_ids": ["user|one", "user|two"],
        "viewer_id": "viewer|synthetic",
    }
    assert "Private Person" not in result.output
    assert "@example.invalid" not in result.output


def test_sync_dry_run_reports_counts_without_downloads(monkeypatch: Any, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path / "state.sqlite3")
    seen: dict[str, bool] = {}
    summary = SyncSummary(
        result=SyncResult(
            run_id="dry-run",
            page_media_count=2,
            tagged_media_count=1,
            candidate_count=2,
            hydrated_count=0,
            selected_count=2,
            dry_run=True,
        ),
        selected=(),
        downloads=DownloadSummary(0, 0, 0, 0),
    )

    def fake_run_sync(config: Any, *, dry_run: bool = False) -> cli_impl.SyncSummary:
        seen["dry_run"] = dry_run
        return summary

    monkeypatch.setattr(cli_impl, "run_sync", fake_run_sync)
    result = runner.invoke(app, ["sync", "--dry-run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert seen == {"dry_run": True}
    assert json.loads(result.stdout) == {
        "candidate_count": 2,
        "dry_run": True,
        "hydrated_count": 0,
        "page_media_count": 2,
        "run_id": "dry-run",
        "selected_count": 2,
        "tagged_media_count": 1,
    }
    assert "downloads" not in result.stdout


def test_sync_reports_download_failures_and_exits_nonzero(
    monkeypatch: Any, tmp_path: Path
) -> None:
    config_path = _write_config(tmp_path / "config.toml", tmp_path / "state.sqlite3")
    summary = SyncSummary(
        result=SyncResult(
            run_id="sync-with-failure",
            page_media_count=2,
            tagged_media_count=0,
            candidate_count=2,
            hydrated_count=2,
            selected_count=2,
            dry_run=False,
        ),
        selected=(),
        downloads=DownloadSummary(2, 1, 0, 1),
    )

    monkeypatch.setattr(cli_impl, "run_sync", lambda config, *, dry_run=False: summary)
    result = runner.invoke(app, ["sync", "--config", str(config_path)])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["downloads"] == {
        "attempted": 2,
        "completed": 1,
        "failed": 1,
        "reused": 0,
    }


def test_report_writes_id_only_json_and_csv(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    config_path = _write_config(tmp_path / "config.toml", database)
    store = StateStore(database)
    store.upsert_media(MediaRecord("media|one", metadata={"displayName": "Private Name"}))
    store.observe_containers(
        "media|one",
        [ContainerMembership("media|one", "page", "page|synthetic")],
        authoritative=True,
    )
    store.observe_tags(
        "media|one", [TagMembership("media|one", "tag|one", "user|one")], authoritative=True
    )
    store.connection.commit()
    store.close()

    output = tmp_path / "derived.json"
    result = runner.invoke(
        app,
        ["report", "--config", str(config_path), "--format", "both", "--output", str(output)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["row_count"] == 1
    assert {Path(name).suffix for name in payload["files"]} == {".json", ".csv"}
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report[0]["media_id"] == "media|one"
    assert report[0]["page_ids"] == ["page|synthetic"]
    assert report[0]["face_tags"] == [{"tag_id": "tag|one", "tagged_user_id": "user|one"}]
    assert "Private Name" not in output.read_text(encoding="utf-8")


def test_verify_reports_invalid_checksum_and_exits_nonzero(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    config_path = _write_config(tmp_path / "config.toml", database)
    valid_path = tmp_path / "valid.bin"
    invalid_path = tmp_path / "invalid.bin"
    valid_path.write_bytes(b"valid")
    invalid_path.write_bytes(b"changed")
    store = StateStore(database)
    store.upsert_media(MediaRecord("media|valid"))
    store.upsert_media(MediaRecord("media|invalid"))
    store.record_download(
        "media|valid",
        status="complete",
        local_path=str(valid_path),
        sha256=hashlib.sha256(b"valid").hexdigest(),
    )
    store.record_download(
        "media|invalid",
        status="complete",
        local_path=str(invalid_path),
        sha256=hashlib.sha256(b"expected").hexdigest(),
    )
    store.close()

    result = runner.invoke(app, ["verify", "--config", str(config_path)])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"checked": 2, "invalid": 1, "valid": 1}
    assert "valid.bin" not in result.output
