from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from vidigami_downloader.auth import (
    AuthenticationError,
    OAuthClient,
    OAuthConfig,
    ReauthenticationRequired,
    TokenSet,
)


class MemoryStore:
    def __init__(self, tokens: TokenSet | None = None):
        self.tokens = tokens
        self.cleared = False

    def load(self):
        return self.tokens

    def save(self, tokens):
        self.tokens = tokens

    def clear(self):
        self.cleared = True
        self.tokens = None


class FakeReceiver:
    redirect_uri = "http://127.0.0.1:9999/callback"

    def __init__(self, code="synthetic-code", error=None):
        self.code = code
        self.error = error
        self.state = None

    def wait(self, timeout):
        assert timeout > 0
        return self.code, self.error, self.state


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_login_uses_pkce_browser_and_persists_token():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )

    opened = []
    receiver = FakeReceiver()

    def open_browser(url):
        opened.append(url)
        receiver.state = parse_qs(urlparse(url).query)["state"][0]
        return True

    client = OAuthClient(
        OAuthConfig(client_id="synthetic-client", client_secret="synthetic-secret"),
        MemoryStore(),
        opener=opener,
        browser_open=open_browser,
    )
    tokens = client.login(receiver)

    assert tokens.access_token == "access"
    assert len(opened) == 1
    query = parse_qs(urlparse(opened[0]).query)
    assert query["client_id"] == ["synthetic-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"]
    assert query["code_challenge"]
    assert query["redirect_uri"] == [FakeReceiver.redirect_uri]
    body = parse_qs(calls[0][0].data.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["synthetic-code"]
    assert body["code_verifier"]
    assert (
        calls[0][0].get_header("Authorization")
        == "Basic " + base64.b64encode(b"synthetic-client:synthetic-secret").decode()
    )


def test_declined_login_has_clear_error():
    receiver = FakeReceiver(code="", error="access_denied")

    def open_browser(url):
        receiver.state = parse_qs(urlparse(url).query)["state"][0]
        return True

    client = OAuthClient(
        OAuthConfig(client_id="synthetic-client", client_secret="synthetic-secret"),
        MemoryStore(),
        browser_open=open_browser,
    )
    with pytest.raises(AuthenticationError, match="authorization was declined"):
        client.login(receiver)


def test_expired_session_refreshes_and_replaces_tokens():
    store = MemoryStore(TokenSet("old", "refresh", expires_at=0))

    def opener(_request, timeout):
        assert timeout == 30
        return FakeResponse(
            {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
        )

    client = OAuthClient(
        OAuthConfig(client_id="synthetic-client", client_secret="synthetic-secret"),
        store,
        opener=opener,
    )
    assert client.access_token() == "new"
    assert store.tokens is not None and store.tokens.refresh_token == "new-refresh"


def test_refresh_failure_clears_session_and_requires_reauth():
    store = MemoryStore(TokenSet("old", "refresh", expires_at=0))

    def opener(_request, timeout):
        assert timeout == 30
        return FakeResponse({"error": "invalid_grant"})

    client = OAuthClient(
        OAuthConfig(client_id="synthetic-client", client_secret="synthetic-secret"),
        store,
        opener=opener,
    )
    with pytest.raises(ReauthenticationRequired, match="auth login"):
        client.access_token()
    assert store.cleared


def test_no_session_requires_reauthentication():
    client = OAuthClient(OAuthConfig(client_id="synthetic-client"), MemoryStore())
    with pytest.raises(ReauthenticationRequired, match="no saved session"):
        client.access_token()
