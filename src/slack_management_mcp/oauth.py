"""Local Slack OAuth login that stores a user token.

The browser flow requests user scopes only, so the saved credential acts as the
authorizing member. It does not install or replace a bot token.
"""

from __future__ import annotations

import errno
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .auth import SlackConfigError, save_user_credentials

# User-token scopes for the methods this server calls. channels:manage and
# channels:join are bot-only; invites use the user invite scopes instead.
USER_SCOPES: tuple[str, ...] = (
    "users:read",
    "users:read.email",
    "channels:read",
    "groups:read",
    "channels:write.invites",
    "groups:write.invites",
    "usergroups:read",
    "usergroups:write",
    "canvases:read",
    "canvases:write",
    "lists:read",
    "lists:write",
)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
LOGIN_TIMEOUT_SECONDS = 180

Exchange = Callable[..., dict[str, str]]


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Slack authorize URL requesting user scopes and no bot scopes."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "user_scope": ",".join(USER_SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://slack.com/oauth/v2/authorize?{query}"


def parse_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    """Return ``(127.0.0.1, port, path)`` for a localhost redirect URI."""
    parsed = urllib.parse.urlparse(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SlackConfigError(
            "SLACK_REDIRECT_URI must look like "
            f"{DEFAULT_REDIRECT_URI} (http, host 127.0.0.1, an explicit port, and a path)."
        )
    return "127.0.0.1", parsed.port, parsed.path or "/"


def interpret_callback(query: dict[str, list[str]], expected_state: str) -> str:
    """Validate the OAuth redirect query and return the authorization code."""
    state = (query.get("state") or [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise SlackConfigError(
            "OAuth state did not match. Try `slack-management-mcp login` again."
        )
    error = (query.get("error") or [""])[0]
    if error:
        raise SlackConfigError(f"Slack authorization was denied ({error}).")
    code = (query.get("code") or [""])[0]
    if not code:
        raise SlackConfigError("Slack did not return an authorization code.")
    return code


def user_credentials_from_oauth(payload: dict) -> dict[str, str]:
    """Pull the user token out of an ``oauth.v2.access`` response.

    A top-level ``access_token`` is a bot token when Slack returns one. This
    keeps only ``authed_user.access_token``.
    """
    authed = payload.get("authed_user") or {}
    if not isinstance(authed, dict):
        authed = {}
    token = str(authed.get("access_token") or "").strip()
    if not token.startswith("xoxp-"):
        raise SlackConfigError(
            "Slack did not return a user token (xoxp-). Confirm the app's user "
            "scopes and redirect URL match the manifest, then try login again."
        )
    team = payload.get("team") or {}
    if not isinstance(team, dict):
        team = {}
    return {
        "token": token,
        "user_id": str(authed.get("id") or ""),
        "team_id": str(team.get("id") or ""),
        "scope": str(authed.get("scope") or ""),
    }


def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, str]:
    """Exchange an authorization code for a user credential record."""
    client = WebClient(token=None)
    try:
        response = client.oauth_v2_access(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except SlackApiError as exc:
        slack_error = ""
        if exc.response is not None:
            slack_error = exc.response.get("error", "") or ""
        raise SlackConfigError(
            "Slack OAuth token exchange failed"
            + (f" ({slack_error})" if slack_error else "")
            + "."
        ) from exc
    payload = response.data if isinstance(response.data, dict) else dict(response.data)
    return user_credentials_from_oauth(payload)


def login(
    *,
    opener: Callable[[str], object] = webbrowser.open,
    exchange: Exchange = exchange_code,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> None:
    """Run the browser OAuth flow and save the resulting user token."""
    client_id, client_secret, redirect_uri = _oauth_env()
    host, port, path = parse_redirect_uri(redirect_uri)
    state = secrets.token_urlsafe(32)
    url = build_authorize_url(client_id=client_id, redirect_uri=redirect_uri, state=state)

    try:
        httpd = _CallbackServer((host, port), path, state)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SlackConfigError(
                f"Port {port} is already in use. Set SLACK_REDIRECT_URI to a free "
                "http://127.0.0.1:<port>/<path> URL that is also listed in the "
                "Slack app redirect URLs."
            ) from exc
        raise

    thread = threading.Thread(
        target=httpd.serve_forever, name="slack-oauth-callback", daemon=True
    )
    thread.start()
    try:
        print("Open this URL to authorize Slack:", file=sys.stderr)
        print(url, file=sys.stderr)
        try:
            opener(url)
        except webbrowser.Error:
            print(
                "Could not open a browser. Open the URL printed above to continue.",
                file=sys.stderr,
            )
        if not httpd.done.wait(timeout):
            raise SlackConfigError(
                "Timed out waiting for Slack to redirect back. "
                "Run `slack-management-mcp login` again."
            )
        if httpd.failure is not None:
            raise httpd.failure
        if not httpd.code:
            raise SlackConfigError("Slack did not return an authorization code.")
        record = exchange(
            client_id=client_id,
            client_secret=client_secret,
            code=httpd.code,
            redirect_uri=redirect_uri,
        )
        saved = save_user_credentials(record)
        who = record["user_id"] or "the authorized user"
        team = f" in team {record['team_id']}" if record["team_id"] else ""
        print(f"Saved Slack user credentials for {who}{team} to {saved}.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class _CallbackServer(HTTPServer):
    """One-shot loopback server that captures the OAuth redirect."""

    def __init__(
        self, server_address: tuple[str, int], callback_path: str, expected_state: str
    ) -> None:
        super().__init__(server_address, _CallbackHandler)
        self.callback_path = callback_path
        self.expected_state = expected_state
        self.done = threading.Event()
        self.code: str | None = None
        self.failure: SlackConfigError | None = None

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        super().serve_forever(poll_interval=poll_interval)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handle the Slack redirect without logging the query string."""

    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler name)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self._send(404, "Not found.")
            return
        if self.server.done.is_set():
            self._send(409, "Authorization already received.")
            return
        params = urllib.parse.parse_qs(parsed.query)
        try:
            self.server.code = interpret_callback(params, self.server.expected_state)
        except SlackConfigError as exc:
            self.server.failure = exc
            self._send(400, str(exc))
        else:
            self._send(200, "Slack authorization received. You can close this window.")
        self.server.done.set()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # The query string contains the authorization code. Do not log it.
        return

    def _send(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _oauth_env() -> tuple[str, str, str]:
    client_id = os.environ.get("SLACK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SLACK_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SLACK_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    missing = [
        name
        for name, value in (
            ("SLACK_CLIENT_ID", client_id),
            ("SLACK_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise SlackConfigError(
            "User OAuth needs "
            + " and ".join(missing)
            + " from the Slack app's Basic Information page."
        )
    return client_id, client_secret, redirect_uri
