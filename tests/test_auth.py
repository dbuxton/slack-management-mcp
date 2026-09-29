"""Auth resolution and the local Slack user-OAuth login. No network."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from slack_sdk.errors import SlackApiError

from slack_management_mcp import server
from slack_management_mcp.auth import (
    SlackConfigError,
    delete_user_credentials,
    load_user_credentials,
    resolve_auth,
    save_user_credentials,
)
from slack_management_mcp.oauth import (
    USER_SCOPES,
    build_authorize_url,
    exchange_code,
    interpret_callback,
    login,
    parse_redirect_uri,
    user_credentials_from_oauth,
)
from slack_management_mcp.slack import SlackClient, SlackToolError, _to_tool_error


@pytest.fixture
def isolated_auth(monkeypatch, tmp_path):
    for name in (
        "SLACK_BOT_TOKEN",
        "SLACK_USER_TOKEN",
        "SLACK_AUTH_MODE",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_REDIRECT_URI",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "slack-management-mcp" / "credentials.json"
    monkeypatch.setenv("SLACK_CREDENTIALS_PATH", str(path))
    return path


def test_unset_mode_prefers_bot_token(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bot")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user")
    save_user_credentials(_record("xoxp-saved"))
    auth = resolve_auth()
    assert auth.token == "xoxb-bot"
    assert auth.identity == "bot"


def test_unset_mode_uses_user_env_then_saved_file(isolated_auth, monkeypatch):
    save_user_credentials(_record("xoxp-saved"))
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-env")
    assert resolve_auth().token == "xoxp-env"
    monkeypatch.delenv("SLACK_USER_TOKEN")
    auth = resolve_auth()
    assert auth.token == "xoxp-saved"
    assert auth.identity == "user"


def test_auth_mode_user_ignores_bot_token(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_AUTH_MODE", "user")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bot")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user")
    auth = resolve_auth()
    assert auth.token == "xoxp-user"
    assert auth.identity == "user"


def test_auth_mode_bot_requires_bot_token(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_AUTH_MODE", "bot")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user")
    with pytest.raises(SlackConfigError, match="SLACK_BOT_TOKEN"):
        resolve_auth()


def test_auth_mode_user_falls_back_to_saved_login(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_AUTH_MODE", "user")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bot")
    save_user_credentials(_record("xoxp-saved", user_id="U7"))
    assert resolve_auth().token == "xoxp-saved"


def test_rejects_wrong_token_prefix(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-not-a-bot")
    with pytest.raises(SlackConfigError, match="xoxb-"):
        resolve_auth()
    monkeypatch.setenv("SLACK_AUTH_MODE", "user")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxb-not-a-user")
    with pytest.raises(SlackConfigError, match="xoxp-"):
        resolve_auth()


def test_invalid_auth_mode(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_AUTH_MODE", "oauth")
    with pytest.raises(SlackConfigError, match="SLACK_AUTH_MODE"):
        resolve_auth()


def test_missing_config_names_both_options(isolated_auth):
    with pytest.raises(SlackConfigError, match="SLACK_BOT_TOKEN") as excinfo:
        resolve_auth()
    assert "login" in str(excinfo.value)
    assert "SLACK_USER_TOKEN" in str(excinfo.value)


def test_saved_credentials_are_private_and_round_trip(isolated_auth):
    path = save_user_credentials(_record("xoxp-saved", user_id="U1", team_id="T1", scope="users:read"))
    assert path == isolated_auth
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    loaded = load_user_credentials()
    assert loaded == {
        "token": "xoxp-saved",
        "user_id": "U1",
        "team_id": "T1",
        "scope": "users:read",
    }
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "xoxb-" not in json.dumps(raw)
    assert delete_user_credentials() is True
    assert load_user_credentials() is None
    assert delete_user_credentials() is False


def test_corrupt_saved_credentials(isolated_auth):
    isolated_auth.parent.mkdir(parents=True)
    isolated_auth.write_text("{", encoding="utf-8")
    with pytest.raises(SlackConfigError, match="not valid JSON"):
        resolve_auth()


def test_client_uses_resolved_token(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user")
    created: dict[str, str | None] = {}

    class RecordingClient:
        def __init__(self, token=None):
            created["token"] = token

    monkeypatch.setattr("slack_management_mcp.slack.WebClient", RecordingClient)
    client = SlackClient()
    assert created["token"] == "xoxp-user"
    assert client.identity == "user"


def test_authorize_url_requests_user_scopes_only():
    url = build_authorize_url(
        client_id="111.222",
        redirect_uri="http://127.0.0.1:8765/callback",
        state="xyz",
    )
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/oauth/v2/authorize"
    assert set(query) == {"client_id", "user_scope", "redirect_uri", "state"}
    assert query["user_scope"][0].split(",") == list(USER_SCOPES)
    assert "channels:write.invites" in USER_SCOPES
    assert "channels:manage" not in USER_SCOPES


def test_redirect_uri_must_be_loopback_with_port():
    host, port, path = parse_redirect_uri("http://127.0.0.1:8765/callback")
    assert (host, port, path) == ("127.0.0.1", 8765, "/callback")
    with pytest.raises(SlackConfigError):
        parse_redirect_uri("http://localhost:8765/callback")
    with pytest.raises(SlackConfigError):
        parse_redirect_uri("https://127.0.0.1:8765/callback")


def test_callback_rejects_state():
    with pytest.raises(SlackConfigError, match="state"):
        interpret_callback({"code": ["abc"], "state": ["nope"]}, "expected")


def test_user_credentials_ignore_bot_access_token():
    record = user_credentials_from_oauth(
        {
            "access_token": "xoxb-bot",
            "team": {"id": "T1", "name": "Acme"},
            "authed_user": {
                "id": "U1",
                "access_token": "xoxp-user",
                "scope": "users:read,channels:read",
            },
        }
    )
    assert record == {
        "token": "xoxp-user",
        "user_id": "U1",
        "team_id": "T1",
        "scope": "users:read,channels:read",
    }
    with pytest.raises(SlackConfigError, match="xoxp-"):
        user_credentials_from_oauth({"access_token": "xoxb-bot", "authed_user": {"id": "U1"}})


def test_exchange_code_reads_user_token(monkeypatch):
    class FakeResponse:
        data = {
            "access_token": "xoxb-bot",
            "team": {"id": "T9"},
            "authed_user": {"id": "U9", "access_token": "xoxp-from-slack", "scope": "users:read"},
        }

    class FakeWeb:
        def __init__(self, token=None):
            assert token is None

        def oauth_v2_access(self, **kwargs):
            assert kwargs["code"] == "auth-code"
            return FakeResponse()

    monkeypatch.setattr("slack_management_mcp.oauth.WebClient", FakeWeb)
    record = exchange_code(
        client_id="111.222",
        client_secret="secret",
        code="auth-code",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    assert record["token"] == "xoxp-from-slack"
    assert record["user_id"] == "U9"


def test_exchange_code_hides_slack_failure_details(monkeypatch):
    class FakeWeb:
        def __init__(self, token=None):
            pass

        def oauth_v2_access(self, **kwargs):
            raise SlackApiError(message="bad", response={"ok": False, "error": "invalid_code"})

    monkeypatch.setattr("slack_management_mcp.oauth.WebClient", FakeWeb)
    with pytest.raises(SlackConfigError, match="invalid_code") as excinfo:
        exchange_code(
            client_id="111.222",
            client_secret="secret",
            code="auth-code",
            redirect_uri="http://127.0.0.1:8765/callback",
        )
    assert "auth-code" not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_login_saves_user_token(isolated_auth, monkeypatch, capsys):
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("SLACK_REDIRECT_URI", redirect)
    seen: dict[str, str] = {}

    def opener(url: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert "scope" not in query
        assert query["client_id"] == ["111.222"]
        state = query["state"][0]
        status = _get(f"{redirect}?code=auth-code&state={urllib.parse.quote(state)}")
        assert status == 200

    def exchange(**kwargs):
        seen["code"] = kwargs["code"]
        assert kwargs["client_secret"] == "secret-value"
        return _record("xoxp-user", user_id="U123", team_id="T123", scope="users:read")

    login(opener=opener, exchange=exchange, timeout=5)
    assert seen["code"] == "auth-code"
    saved = json.loads(isolated_auth.read_text(encoding="utf-8"))
    assert saved["token"] == "xoxp-user"
    assert isolated_auth.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert "xoxp-user" not in captured.out
    assert "xoxp-user" not in captured.err
    assert "auth-code" not in captured.out
    assert "auth-code" not in captured.err
    assert "secret-value" not in captured.out + captured.err
    assert "U123" in captured.out


def test_login_rejects_mismatched_state(isolated_auth, monkeypatch):
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("SLACK_REDIRECT_URI", redirect)

    def opener(url: str) -> None:
        assert _get(f"{redirect}?code=auth-code&state=nope") == 400

    def exchange(**kwargs):
        raise AssertionError("token exchange must not run after a state mismatch")

    with pytest.raises(SlackConfigError, match="state"):
        login(opener=opener, exchange=exchange, timeout=5)
    assert not isolated_auth.exists()


def test_login_port_in_use(isolated_auth, monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-value")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        monkeypatch.setenv("SLACK_REDIRECT_URI", f"http://127.0.0.1:{port}/callback")
        with pytest.raises(SlackConfigError, match="already in use"):
            login(opener=lambda url: None, exchange=lambda **kwargs: {}, timeout=1)


def test_login_times_out(isolated_auth, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("SLACK_REDIRECT_URI", f"http://127.0.0.1:{port}/callback")
    with pytest.raises(SlackConfigError, match="Timed out"):
        login(opener=lambda url: None, exchange=lambda **kwargs: {}, timeout=0.2)
    assert not isolated_auth.exists()


def test_main_commands(isolated_auth, monkeypatch, capsys):
    calls: dict[str, bool] = {}
    monkeypatch.setattr(server.mcp, "run", lambda: calls.setdefault("run", True))
    monkeypatch.setattr(server, "login", lambda: calls.setdefault("login", True))

    server.main([])
    assert calls == {"run": True}

    server.main(["login"])
    assert calls == {"run": True, "login": True}
    assert capsys.readouterr().out == ""

    save_user_credentials(_record("xoxp-saved"))
    server.main(["logout"])
    assert "Removed" in capsys.readouterr().out
    assert not isolated_auth.exists()
    server.main(["logout"])
    assert "No saved" in capsys.readouterr().out

    with pytest.raises(SystemExit) as excinfo:
        server.main(["serve"])
    assert excinfo.value.code == 2


def test_main_login_failure_exits(monkeypatch, capsys):
    def boom() -> None:
        raise SlackConfigError("missing client")

    monkeypatch.setattr(server, "login", boom)
    def fail_if_started() -> None:
        raise AssertionError("server started")

    monkeypatch.setattr(server.mcp, "run", fail_if_started)
    with pytest.raises(SystemExit) as excinfo:
        server.main(["login"])
    assert excinfo.value.code == 1
    assert "missing client" in capsys.readouterr().err


def test_identity_specific_slack_errors():
    bot_missing = _to_tool_error(_api_error("missing_scope"), "bot")
    user_missing = _to_tool_error(_api_error("missing_scope"), "user")
    assert "reinstall" in str(bot_missing)
    assert "login" in str(user_missing)
    for code in (
        "channel_not_found",
        "not_in_channel",
        "permission_denied",
        "cant_invite_self",
        "not_authed",
        "invalid_auth",
    ):
        bot = str(_to_tool_error(_api_error(code), "bot"))
        user = str(_to_tool_error(_api_error(code), "user"))
        assert bot != user
        assert "bot" in bot.lower()
        folded = user.lower()
        assert "slack user" in folded or "user token" in folded


def test_channel_lookup_mentions_acting_account():
    class EmptyChannels:
        def conversations_list(self, **kwargs):
            return {"channels": [], "response_metadata": {}}

    user_client = SlackClient(client=EmptyChannels(), identity="user")
    with pytest.raises(SlackToolError, match="your Slack user"):
        user_client.find_channel(name="missing")
    bot_client = SlackClient(client=EmptyChannels())
    with pytest.raises(SlackToolError, match="the bot"):
        bot_client.find_channel(name="missing")


def _record(
    token: str, user_id: str = "U1", team_id: str = "T1", scope: str = "users:read"
) -> dict[str, str]:
    return {"token": token, "user_id": user_id, "team_id": team_id, "scope": scope}


def _api_error(code: str) -> SlackApiError:
    return SlackApiError(message=code, response={"ok": False, "error": code})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str) -> int:
    last: Exception | None = None
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except urllib.error.URLError as exc:
            last = exc
            time.sleep(0.02)
    raise AssertionError(last)
