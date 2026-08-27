"""OAuth Authorization Code + PKCE authentication for Vidigami.

The implementation deliberately uses the operating system browser and keychain.
It does not read browser profiles, cookies, or local-storage databases.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AuthenticationError(RuntimeError):
    """Authentication could not be completed."""


class ReauthenticationRequired(AuthenticationError):
    """The user must perform an interactive login again."""

    def __init__(self, reason: str = "the session expired or cannot be refreshed") -> None:
        super().__init__(
            "Vidigami authentication is no longer valid; run `vidigami auth login` again "
            f"({reason})"
        )


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str = "vidigami_production"
    client_secret: str | None = None
    authorization_endpoint: str = "https://accounts.vidigami.com/oauth2/authorize"
    token_endpoint: str = "https://accounts.vidigami.com/oauth2/token"
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: tuple[str, ...] = (
        "openid",
        "email",
        "profile",
        "organization.read",
        "content.read",
        "offline_access",
    )
    token_auth_method: str = "client_secret_basic"
    callback_timeout: float = 300.0


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    token_type: str = "Bearer"
    scope: str | None = None

    @classmethod
    def from_response(cls, payload: Mapping[str, Any]) -> TokenSet:
        access = payload.get("access_token")
        if not access:
            raise AuthenticationError("Vidigami did not return an access token")
        expires_in = payload.get("expires_in")
        try:
            expires_at = time.time() + float(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_at = None
        return cls(
            access_token=str(access),
            refresh_token=_optional_string(payload.get("refresh_token")),
            expires_at=expires_at,
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=_optional_string(payload.get("scope")),
        )

    def expired(self, leeway: float = 60.0) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at - leeway


class TokenStore(Protocol):
    def load(self) -> TokenSet | None: ...
    def save(self, tokens: TokenSet) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    """Token store backed by the platform keychain through ``keyring``."""

    def __init__(self, service: str = "vidigami-downloader", account: str = "default") -> None:
        self.service = service
        self.account = account

    def _keyring(self) -> Any:
        try:
            import keyring
        except ImportError as exc:
            raise AuthenticationError(
                "The keyring package is required for authentication; "
                "install the project dependencies"
            ) from exc
        return keyring

    def load(self) -> TokenSet | None:
        encoded = self._keyring().get_password(self.service, self.account)
        if not encoded:
            return None
        try:
            payload = json.loads(encoded)
            if not isinstance(payload, Mapping):
                raise ValueError
            return TokenSet(
                access_token=str(payload["access_token"]),
                refresh_token=_optional_string(payload.get("refresh_token")),
                expires_at=float(payload["expires_at"])
                if payload.get("expires_at") is not None
                else None,
                token_type=str(payload.get("token_type") or "Bearer"),
                scope=_optional_string(payload.get("scope")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "The saved Vidigami token is invalid; run auth login again"
            ) from exc

    def save(self, tokens: TokenSet) -> None:
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
            "scope": tokens.scope,
        }
        self._keyring().set_password(self.service, self.account, json.dumps(payload))

    def clear(self) -> None:
        try:
            self._keyring().delete_password(self.service, self.account)
        except Exception as exc:  # keyring backends differ on missing entries
            if exc.__class__.__name__ not in {"PasswordDeleteError", "KeyringError"}:
                raise


class CallbackReceiver(Protocol):
    @property
    def redirect_uri(self) -> str: ...
    def wait(self, timeout: float) -> tuple[str, str | None, str | None]: ...


class LocalCallbackReceiver:
    """One-shot loopback HTTP callback receiver for an OAuth login."""

    def __init__(self, redirect_uri: str) -> None:
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise AuthenticationError("OAuth redirect URI must be a local HTTP callback")
        if not parsed.path:
            raise AuthenticationError("OAuth redirect URI must include a callback path")
        self._path = parsed.path
        self._server = _CallbackServer((parsed.hostname, parsed.port or 80), self._path)

    @property
    def redirect_uri(self) -> str:
        address = self._server.server_address
        host, port = str(address[0]), int(address[1])
        return f"http://{host}:{port}{self._path}"

    def wait(self, timeout: float) -> tuple[str, str | None, str | None]:
        self._server.timeout = min(1.0, max(0.05, timeout))
        deadline = time.monotonic() + timeout
        while not self._server.result and time.monotonic() < deadline:
            self._server.handle_request()
        if not self._server.result:
            raise AuthenticationError("Timed out waiting for the OAuth callback")
        code, error, state = self._server.result
        return code, error, state


class _CallbackServer(HTTPServer):
    def __init__(self, address: tuple[str, int], path: str) -> None:
        self.callback_path = path
        self.result: tuple[str, str | None, str | None] | None = None
        super().__init__(address, _CallbackHandler)


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:  # type: ignore[attr-defined]
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query)
        code = values.get("code", [None])[0]
        error = values.get("error", [None])[0]
        callback_state = values.get("state", [None])[0]
        self.server.result = (code or "", error, callback_state)  # type: ignore[attr-defined]
        body = b"Authentication complete. You may close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


class OAuthClient:
    def __init__(
        self,
        config: OAuthConfig,
        store: TokenStore | None = None,
        *,
        opener: Any = urlopen,
        browser_open: Any = webbrowser.open,
    ) -> None:
        self.config = config
        self.store = store or KeyringTokenStore()
        self._opener = opener
        self._browser_open = browser_open

    def login(self, receiver: CallbackReceiver | None = None) -> TokenSet:
        receiver = receiver or LocalCallbackReceiver(self.config.redirect_uri)
        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        state = secrets.token_urlsafe(32)
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": receiver.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorization_url = (
            self.config.authorization_endpoint + "?" + urllib.parse.urlencode(params)
        )
        try:
            opened = self._browser_open(authorization_url)
        except Exception as exc:
            raise AuthenticationError(
                "Could not open the system browser for Vidigami login"
            ) from exc
        if opened is False:
            raise AuthenticationError("Could not open the system browser for Vidigami login")
        code, error, callback_state = receiver.wait(self.config.callback_timeout)
        if not callback_state or not hmac.compare_digest(callback_state, state):
            raise AuthenticationError("Vidigami authorization callback state did not match")
        if error:
            raise AuthenticationError(f"Vidigami authorization was declined ({error})")
        if not code:
            raise AuthenticationError("Vidigami authorization returned no code")
        tokens = self._exchange_code(code, verifier, receiver.redirect_uri)
        self.store.save(tokens)
        return tokens

    def access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise ReauthenticationRequired("no saved session")
        if not tokens.expired():
            return tokens.access_token
        if not tokens.refresh_token:
            raise ReauthenticationRequired("no refresh token was issued")
        try:
            refreshed = self._refresh(tokens.refresh_token)
        except AuthenticationError as exc:
            self.store.clear()
            raise ReauthenticationRequired("refresh failed") from exc
        self.store.save(refreshed)
        return refreshed.access_token

    def status(self) -> TokenSet | None:
        return self.store.load()

    def logout(self) -> None:
        self.store.clear()

    def _exchange_code(self, code: str, verifier: str, redirect_uri: str) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    def _refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
            }
        )

    def _token_request(self, values: Mapping[str, str]) -> TokenSet:
        body = urllib.parse.urlencode(values).encode("utf-8")
        request = Request(
            self.config.token_endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        if self.config.token_auth_method == "client_secret_basic":
            if not self.config.client_secret:
                raise AuthenticationError(
                    "Vidigami requires a client secret for token exchange; "
                    "provide it through ignored local configuration"
                )
            credentials = f"{self.config.client_id}:{self.config.client_secret}".encode()
            request.add_header(
                "Authorization",
                "Basic " + base64.b64encode(credentials).decode("ascii"),
            )
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise AuthenticationError(
                "Vidigami token exchange failed; run auth login again"
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AuthenticationError("Could not reach Vidigami authentication") from exc
        if not isinstance(payload, Mapping):
            raise AuthenticationError("Vidigami returned an invalid token response")
        if payload.get("error"):
            raise AuthenticationError("Vidigami rejected authentication; run auth login again")
        return TokenSet.from_response(payload)


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _code_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


__all__ = [
    "AuthenticationError",
    "CallbackReceiver",
    "KeyringTokenStore",
    "LocalCallbackReceiver",
    "OAuthClient",
    "OAuthConfig",
    "ReauthenticationRequired",
    "TokenSet",
    "TokenStore",
]
