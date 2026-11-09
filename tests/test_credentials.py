from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values
from typer.testing import CliRunner

from vidigami_downloader import cli_impl, service
from vidigami_downloader.auth import (
    AuthenticationError,
    DotenvTokenStore,
    ReauthenticationRequired,
    TokenSet,
)
from vidigami_downloader.config import AppConfig, load_config
from vidigami_downloader.credentials import login_credentials


@pytest.fixture(autouse=True)
def clean_login_environment(monkeypatch):
    monkeypatch.delenv("VIDIGAMI_USERNAME", raising=False)
    monkeypatch.delenv("VIDIGAMI_PASSWORD", raising=False)


def test_credentials_are_literal_and_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("VIDIGAMI_USERNAME=synthetic\nVIDIGAMI_PASSWORD='${HOME} spaces # !'\n")
    assert login_credentials(path) == ("synthetic", "${HOME} spaces # !")
    assert "VIDIGAMI_USERNAME" not in __import__("os").environ
    monkeypatch.setenv("VIDIGAMI_USERNAME", "override")
    assert login_credentials(path) == ("override", "${HOME} spaces # !")
    monkeypatch.setenv("VIDIGAMI_PASSWORD", "")
    assert login_credentials(path) == ("override", None)


def test_missing_credentials_and_config_relative_path(tmp_path):
    config = load_config(tmp_path / "config.toml")
    assert config.credentials_file == tmp_path / ".env"
    assert login_credentials(config.credentials_file) == (None, None)
    (tmp_path / "config.toml").write_text("")
    assert load_config(tmp_path / "config.toml").credentials_file == config.credentials_file


def test_session_round_trip_refresh_and_logout_preserve_credentials(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep this\nVIDIGAMI_PASSWORD='synthetic'\nUNRELATED=keep\n")
    store = DotenvTokenStore(path)
    assert store.load() is None
    tokens = TokenSet("synthetic-access", "synthetic-refresh", 123.0, scope="offline_access")
    store.save(tokens)
    assert store.load() == tokens
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.save(TokenSet("replacement"))
    assert store.load() == TokenSet("replacement")
    store.clear()
    assert store.load() is None
    assert dotenv_values(path) == {"VIDIGAMI_PASSWORD": "synthetic", "UNRELATED": "keep"}
    assert "# keep this" in path.read_text()


def test_session_creates_private_file_and_rejects_malformed_data(tmp_path):
    path = tmp_path / ".env"
    store = DotenvTokenStore(path)
    store.clear()
    assert not path.exists()
    store.save(TokenSet("synthetic"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for bad in ["broken-private-value", "[]", json.dumps({"access_token": ""})]:
        path.write_text(f"VIDIGAMI_OAUTH_SESSION='{bad}'\n")
        with pytest.raises(AuthenticationError) as error:
            store.load()
        assert bad not in str(error.value)


def test_login_reads_saved_values_and_browser_ignores_them(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("VIDIGAMI_USERNAME=synthetic\nVIDIGAMI_PASSWORD=synthetic\n")
    client = Mock()
    monkeypatch.setattr(cli_impl, "oauth_client", lambda config: client)
    args = ["auth", "login", "--config", str(tmp_path / "config.toml")]
    result = CliRunner().invoke(cli_impl.app, args)
    assert result.exit_code == 0
    client.direct_login.assert_called_once_with(
        username="synthetic", password="synthetic", organization=None
    )
    assert "synthetic" not in result.output
    assert ".env" in result.output
    client.reset_mock()
    result = CliRunner().invoke(cli_impl.app, [*args, "--browser"])
    assert result.exit_code == 0
    client.login.assert_called_once_with()
    client.direct_login.assert_not_called()


@pytest.mark.parametrize("reason", ["no saved session", "refresh failed"])
def test_api_recovers_session_without_prompting(tmp_path, monkeypatch, reason):
    path = tmp_path / ".env"
    path.write_text("VIDIGAMI_USERNAME=synthetic\nVIDIGAMI_PASSWORD=synthetic\n")
    auth = Mock()
    auth.access_token.side_effect = ReauthenticationRequired(reason)
    auth.direct_login.return_value = TokenSet("recovered")
    monkeypatch.setattr(
        service, "GraphQLClient", lambda provider, **kw: SimpleNamespace(get=provider)
    )
    client = service.graph_client(AppConfig(credentials_file=path), auth)
    assert client.get() == "recovered"
    callback = auth.direct_login.call_args.kwargs["input_fn"]
    with pytest.raises(AuthenticationError, match="interactive step"):
        callback("organization")


def test_api_uses_valid_session_and_never_prompts_for_incomplete_credentials(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("VIDIGAMI_USERNAME=synthetic\n")
    auth = Mock()
    auth.access_token.return_value = "current"
    monkeypatch.setattr(
        service, "GraphQLClient", lambda provider, **kw: SimpleNamespace(get=provider)
    )
    client = service.graph_client(AppConfig(credentials_file=path), auth)
    assert client.get() == "current"
    auth.direct_login.assert_not_called()
    auth.access_token.side_effect = ReauthenticationRequired()
    with pytest.raises(ReauthenticationRequired):
        client.get()
    auth.direct_login.assert_not_called()


def test_oauth_client_wires_config_relative_store(tmp_path):
    client = service.oauth_client(AppConfig(credentials_file=tmp_path / ".env"))
    assert isinstance(client.store, DotenvTokenStore)
    assert client.store.path == tmp_path / ".env"
