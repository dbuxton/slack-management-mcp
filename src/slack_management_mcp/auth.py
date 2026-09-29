"""Choose whether Slack API calls act as the bot or as an OAuth user.

``SLACK_BOT_TOKEN`` (``xoxb-``) is the bot. ``SLACK_USER_TOKEN`` or the token
saved by ``slack-management-mcp login`` (``xoxp-``) is the user. Login never
writes the bot token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class SlackConfigError(RuntimeError):
    """Raised when the server is not configured correctly (e.g. missing token)."""


@dataclass(frozen=True)
class SlackAuth:
    """A Slack token and the identity it acts as (``bot`` or ``user``)."""

    token: str
    identity: str


_MISSING = (
    "No Slack credential is configured. Either set SLACK_BOT_TOKEN to a bot "
    "token (xoxb-), or act as yourself by setting SLACK_USER_TOKEN (xoxp-) or "
    "running `slack-management-mcp login`. See the README."
)


def credentials_path() -> Path:
    """Path of the saved user-token file.

    ``SLACK_CREDENTIALS_PATH`` overrides the default
    ``$XDG_CONFIG_HOME/slack-management-mcp/credentials.json`` location.
    """
    override = os.environ.get("SLACK_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "slack-management-mcp" / "credentials.json"


def load_user_credentials() -> dict[str, str] | None:
    """Return the saved user credential record, or None when no file exists."""
    path = credentials_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SlackConfigError(
            f"Saved Slack credentials at {path} are not valid JSON. "
            "Run `slack-management-mcp logout` and log in again."
        ) from exc
    if not isinstance(data, dict):
        raise SlackConfigError(
            f"Saved Slack credentials at {path} are not a JSON object."
        )
    token = str(data.get("token") or "").strip()
    if not token:
        raise SlackConfigError(
            f"Saved Slack credentials at {path} do not contain a user token."
        )
    return {
        "token": token,
        "user_id": str(data.get("user_id") or ""),
        "team_id": str(data.get("team_id") or ""),
        "scope": str(data.get("scope") or ""),
    }


def save_user_credentials(record: dict[str, str]) -> Path:
    """Write the user credential file with mode ``0600`` and return its path."""
    path = credentials_path()
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    payload = json.dumps(
        {
            "token": record["token"],
            "user_id": record.get("user_id") or "",
            "team_id": record.get("team_id") or "",
            "scope": record.get("scope") or "",
        },
        indent=2,
    )
    # Open with 0600 so the token is never created world-readable. umask can
    # only remove bits from that mode; chmod restores 0600 afterward.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def delete_user_credentials() -> bool:
    """Delete the saved user credential file. Return whether it existed."""
    path = credentials_path()
    if not path.is_file():
        return False
    path.unlink()
    return True


def resolve_auth() -> SlackAuth:
    """Pick the bot or user token according to the environment and saved login.

    * ``SLACK_AUTH_MODE=bot`` uses only ``SLACK_BOT_TOKEN``.
    * ``SLACK_AUTH_MODE=user`` uses ``SLACK_USER_TOKEN``, else the saved login.
    * Unset: bot token if present, else ``SLACK_USER_TOKEN``, else the saved login.
    """
    mode = os.environ.get("SLACK_AUTH_MODE", "").strip().lower()
    if mode not in ("", "bot", "user"):
        raise SlackConfigError("SLACK_AUTH_MODE must be 'bot', 'user', or unset.")

    bot = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    user_env = os.environ.get("SLACK_USER_TOKEN", "").strip()

    if mode == "bot":
        if not bot:
            raise SlackConfigError(
                "SLACK_AUTH_MODE=bot but SLACK_BOT_TOKEN is not set. Install the "
                "Slack app and set SLACK_BOT_TOKEN to the Bot User OAuth Token "
                "(xoxb-)."
            )
        _require_prefix(bot, "bot", "SLACK_BOT_TOKEN")
        return SlackAuth(token=bot, identity="bot")

    if mode == "user":
        token, source = _user_token(user_env)
        if not token:
            raise SlackConfigError(
                "SLACK_AUTH_MODE=user but no user token is available. Set "
                "SLACK_USER_TOKEN (xoxp-) or run `slack-management-mcp login`."
            )
        _require_prefix(token, "user", source)
        return SlackAuth(token=token, identity="user")

    if bot:
        _require_prefix(bot, "bot", "SLACK_BOT_TOKEN")
        return SlackAuth(token=bot, identity="bot")
    token, source = _user_token(user_env)
    if token:
        _require_prefix(token, "user", source)
        return SlackAuth(token=token, identity="user")
    raise SlackConfigError(_MISSING)


def _user_token(user_env: str) -> tuple[str, str]:
    if user_env:
        return user_env, "SLACK_USER_TOKEN"
    saved = load_user_credentials()
    if saved is None:
        return "", ""
    return saved["token"], f"saved credentials at {credentials_path()}"


def _require_prefix(token: str, identity: str, source: str) -> None:
    expected = "xoxb-" if identity == "bot" else "xoxp-"
    if token.startswith(expected):
        return
    kind = "bot" if identity == "bot" else "user"
    raise SlackConfigError(
        f"{source} must be a Slack {kind} token starting with '{expected}'."
    )
