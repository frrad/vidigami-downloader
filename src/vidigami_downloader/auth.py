"""OAuth Authorization Code + PKCE authentication for Vidigami.

The implementation deliberately uses the operating system browser and keychain.
It does not read browser profiles, cookies, or local-storage databases.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)


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
    direct_redirect_uri: str = "https://app.vidigami.com/auth-callback"


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


@dataclass(frozen=True)
class _HTTPResult:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


class _HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _HTTPResult: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _UrllibTransport:
    """Small cookie-aware transport used only for direct login.

    The cookie jar is owned by this object and is never serialized.  Redirects
    are returned to the caller so the OAuth callback can be captured before a
    browser or HTTP client requests it.
    """

    def __init__(self, opener: Any = urlopen) -> None:
        if opener is not urlopen:
            self._open = opener
        else:
            self._open = build_opener(
                HTTPCookieProcessor(), _NoRedirectHandler()
            ).open

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _HTTPResult:
        request = Request(url, data=data, headers=dict(headers or {}), method=method)
        try:
            with self._open(request, timeout=30) as response:
                return _HTTPResult(
                    response.status,
                    response.geturl(),
                    dict(response.headers.items()),
                    response.read(),
                )
        except HTTPError as exc:
            return _HTTPResult(exc.code, exc.geturl(), dict(exc.headers.items()), exc.read())
        except (URLError, TimeoutError, OSError) as exc:
            raise AuthenticationError("Could not reach Vidigami authentication") from exc


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


@dataclass
class _HTMLForm:
    action: str
    method: str
    fields: list[tuple[str, str]]
    organization_options: list[tuple[str, str]]


class _LoginHTMLParser(HTMLParser):
    """Extract only the fields needed from Vidigami's server-rendered forms."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_HTMLForm] = []
        self.links: list[str] = []
        self._form: _HTMLForm | None = None
        self._select_name: str | None = None
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._form = _HTMLForm(
                values.get("action", "/login"),
                values.get("method", "get").lower(),
                [],
                [],
            )
        elif tag == "input" and self._form is not None and values.get("name"):
            input_type = values.get("type", "text").lower()
            if input_type not in {"submit", "button", "image", "reset"}:
                self._form.fields.append((values["name"], values.get("value", "")))
        elif tag == "select" and self._form is not None:
            self._select_name = values.get("name") or None
        elif tag == "option" and self._select_name:
            self._option_value = values.get("value", "")
            self._option_text = []
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_data(self, data: str) -> None:
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option_value is not None and self._form is not None:
            self._form.organization_options.append(
                (self._option_value, " ".join("".join(self._option_text).split()))
            )
            self._option_value = None
            self._option_text = []
        elif tag == "select":
            self._select_name = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def _parse_login_html(body: bytes) -> _LoginHTMLParser:
    parser = _LoginHTMLParser()
    try:
        parser.feed(body.decode("utf-8", "replace"))
    except ValueError:
        # A malformed error page is not useful for progressing authentication.
        pass
    return parser


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

    def direct_login(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        organization: str | None = None,
        input_fn: Any | None = None,
        password_fn: Any | None = None,
        transport: _HTTPTransport | None = None,
    ) -> TokenSet:
        """Complete OAuth login through Vidigami's public HTML login forms.

        This is a fallback for deployments where the system-browser callback
        renders a generic error page.  It does not automate a browser: the
        username and password are entered in this process, while the OAuth
        provider still issues the authorization code.  Credentials and cookies
        are held only for the duration of this call.
        """
        input_fn = input_fn or input
        password_fn = password_fn or getpass.getpass
        if username is None:
            username = str(input_fn("Vidigami username: "))
        if password is None:
            password = str(password_fn("Vidigami password: "))
        if not username or not password:
            raise AuthenticationError("A Vidigami username and password are required")

        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        state = secrets.token_urlsafe(32)
        redirect_uri = self.config.direct_redirect_uri
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorization_url = (
            self.config.authorization_endpoint + "?" + urllib.parse.urlencode(params)
        )
        direct_transport = transport or _UrllibTransport(self._opener)
        result = self._direct_authorize(
            authorization_url,
            username=username,
            password=password,
            organization=organization,
            transport=direct_transport,
            input_fn=input_fn,
        )
        callback_url, callback_state, code, error = result
        del callback_url  # Kept in the tuple to make callback validation explicit.
        if not callback_state or not hmac.compare_digest(callback_state, state):
            raise AuthenticationError("Vidigami authorization callback state did not match")
        if error:
            raise AuthenticationError(f"Vidigami authorization was declined ({error})")
        if not code:
            raise AuthenticationError("Vidigami authorization returned no code")
        tokens = self._exchange_code(code, verifier, redirect_uri, transport=direct_transport)
        self.store.save(tokens)
        return tokens

    def _direct_authorize(
        self,
        authorization_url: str,
        *,
        username: str,
        password: str,
        organization: str | None,
        transport: _HTTPTransport,
        input_fn: Any,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Drive same-site login redirects, returning the unrequested callback."""
        current_url = authorization_url
        method = "GET"
        data: bytes | None = None
        selected_organization = organization
        login_submissions = 0
        for _hop in range(12):
            response = transport.request(
                method,
                current_url,
                data=data,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Content-Type": "application/x-www-form-urlencoded"
                    if data is not None
                    else "",
                },
            )
            location = _header(response.headers, "Location")
            if 300 <= response.status < 400 and location:
                next_url = urllib.parse.urljoin(response.url or current_url, location)
                callback = self._callback_result(next_url)
                if callback is not None:
                    return callback
                _ensure_auth_host(next_url, self.config.authorization_endpoint)
                current_url = next_url
                if response.status in {307, 308}:
                    # Preserve the method/body only for redirects that require it.
                    pass
                else:
                    method, data = "GET", None
                continue
            if response.status >= 400:
                raise AuthenticationError("Vidigami login could not be completed")

            parsed = _parse_login_html(response.body)
            choices = _organization_choices(parsed)
            if len(choices) > 1 and selected_organization is None:
                selected_organization = _choose_organization(choices, input_fn)
            elif len(choices) == 1 and selected_organization is None:
                selected_organization = choices[0][0]

            form = _credential_form(parsed)
            if form is None and selected_organization is not None:
                form = next(
                    (candidate for candidate in parsed.forms if candidate.organization_options),
                    None,
                )
            if form is None:
                form = _synthetic_login_form(
                    response.url or current_url,
                    fallback_redirect=authorization_url,
                )
            if form is None:
                raise AuthenticationError("Vidigami login page did not contain a sign-in form")
            values = list(form.fields)
            names = {name for name, _value in values}
            if "username" in names:
                values = _replace_form_value(values, "username", username)
            if "password" in names:
                values = _replace_form_value(values, "password", password)
            if selected_organization is not None:
                for organization_name in ("organization", "organization_id", "identifier"):
                    if organization_name in names:
                        values = _replace_form_value(
                            values, organization_name, selected_organization
                        )
            if (
                "username" not in names
                and "password" not in names
                and selected_organization is None
            ):
                raise AuthenticationError("Vidigami login form did not accept credentials")
            login_submissions += 1
            if login_submissions > 4:
                raise AuthenticationError("Vidigami login did not complete")
            target = urllib.parse.urljoin(response.url or current_url, form.action)
            if urllib.parse.urlparse(target).hostname != urllib.parse.urlparse(
                self.config.authorization_endpoint
            ).hostname:
                raise AuthenticationError("Vidigami login form used an unexpected host")
            if form.method == "get":
                query = urllib.parse.urlencode(values)
                current_url = target + ("&" if "?" in target else "?") + query
                method, data = "GET", None
            else:
                current_url, method, data = (
                    target,
                    "POST",
                    urllib.parse.urlencode(values).encode("utf-8"),
                )
        raise AuthenticationError("Vidigami authentication did not complete")

    def _callback_result(
        self, url: str
    ) -> tuple[str, str | None, str | None, str | None] | None:
        parsed = urllib.parse.urlparse(url)
        callback = urllib.parse.urlparse(self.config.direct_redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            callback.scheme,
            callback.netloc,
            callback.path,
        ):
            return None
        values = urllib.parse.parse_qs(parsed.query)
        return (
            url,
            values.get("state", [None])[0],
            values.get("code", [None])[0],
            values.get("error", [None])[0],
        )

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

    def _exchange_code(
        self,
        code: str,
        verifier: str,
        redirect_uri: str,
        *,
        transport: _HTTPTransport | None = None,
    ) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            transport=transport,
        )

    def _refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
            }
        )

    def _token_request(
        self, values: Mapping[str, str], *, transport: _HTTPTransport | None = None
    ) -> TokenSet:
        body = urllib.parse.urlencode(values).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self.config.token_auth_method == "client_secret_basic":
            if not self.config.client_secret:
                raise AuthenticationError(
                    "Vidigami requires a client secret for token exchange; "
                    "provide it through ignored local configuration"
                )
            credentials = f"{self.config.client_id}:{self.config.client_secret}".encode()
            headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials).decode("ascii")
            )
        if transport is not None:
            try:
                response = transport.request(
                    "POST", self.config.token_endpoint, data=body, headers=headers
                )
                if response.status >= 400:
                    raise AuthenticationError(
                        "Vidigami token exchange failed; run auth login again"
                    )
                payload = json.loads(response.body.decode("utf-8"))
            except AuthenticationError:
                raise
            except (TimeoutError, OSError, json.JSONDecodeError) as exc:
                raise AuthenticationError("Could not reach Vidigami authentication") from exc
            if not isinstance(payload, Mapping):
                raise AuthenticationError("Vidigami returned an invalid token response")
            if payload.get("error"):
                raise AuthenticationError("Vidigami rejected authentication; run auth login again")
            return TokenSet.from_response(payload)
        request = Request(
            self.config.token_endpoint,
            data=body,
            headers=headers,
            method="POST",
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


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _ensure_auth_host(url: str, authorization_endpoint: str) -> None:
    target_host = urllib.parse.urlparse(url).hostname
    auth_host = urllib.parse.urlparse(authorization_endpoint).hostname
    if not target_host or target_host != auth_host:
        raise AuthenticationError("Vidigami login redirected to an unexpected host")


def _credential_form(parser: _LoginHTMLParser) -> _HTMLForm | None:
    for form in parser.forms:
        names = {name for name, _value in form.fields}
        if "password" in names or "username" in names:
            return form
    return None


def _synthetic_login_form(
    url: str, *, fallback_redirect: str | None = None
) -> _HTMLForm | None:
    """Build the known credential form when the login custom element is unrendered.

    The public login page mounts ``<login-form>`` client-side.  Its current
    production chunk posts to ``/login`` (or ``/login_verify`` for the second
    step) with a hidden ``redirect`` carrying the OAuth authorize URL.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.path not in {"/login", "/login_verify"}:
        return None
    query = urllib.parse.parse_qs(parsed.query)
    redirect = (
        query.get("returnTo", [None])[0]
        or query.get("redirect", [None])[0]
        or fallback_redirect
    )
    if not redirect:
        return None
    return _HTMLForm(
        parsed.path,
        "post",
        [("username", ""), ("password", ""), ("redirect", redirect)],
        [],
    )


def _organization_choices(parser: _LoginHTMLParser) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for form in parser.forms:
        choices.extend(form.organization_options)
    # Some deployments render organizations as links rather than a select.
    for link in parser.links:
        parsed = urllib.parse.urlparse(link)
        values = urllib.parse.parse_qs(parsed.query)
        identifier = (
            values.get("organization", [None])[0]
            or values.get("organization_id", [None])[0]
            or values.get("identifier", [None])[0]
        )
        if identifier:
            choices.append((identifier, identifier))
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, label in choices:
        if value and value not in seen:
            result.append((value, label or value))
            seen.add(value)
    return result


def _choose_organization(
    choices: list[tuple[str, str]], input_fn: Any
) -> str:
    # Organization labels are shown only interactively and are never persisted.
    answer = input_fn(
        "Vidigami returned multiple organizations; enter the number to choose one ("
        + " ".join(f"{index}: {label}" for index, (_value, label) in enumerate(choices, 1))
        + "): "
    ).strip()
    # Keep parsing deliberately strict so an accidental invalid choice cannot
    # submit credentials to a different organization.
    try:
        index = int(answer) - 1
    except ValueError as exc:
        raise AuthenticationError("Invalid Vidigami organization choice") from exc
    if not 0 <= index < len(choices):
        raise AuthenticationError("Invalid Vidigami organization choice")
    return choices[index][0]


def _replace_form_value(
    fields: list[tuple[str, str]], name: str, value: str
) -> list[tuple[str, str]]:
    replaced = False
    result: list[tuple[str, str]] = []
    for field_name, field_value in fields:
        if field_name == name:
            if not replaced:
                result.append((field_name, value))
                replaced = True
        else:
            result.append((field_name, field_value))
    if not replaced:
        result.append((name, value))
    return result


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
