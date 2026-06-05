"""MCP server exposing three focused Slack tools.

Run over stdio. Authenticates as a bot using the ``SLACK_BOT_TOKEN`` environment
variable (a Bot User OAuth token, ``xoxb-...``). See the README for setup.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .slack import SlackClient, SlackConfigError, SlackToolError

mcp = FastMCP("slack-management-mcp")

# The Slack client is created lazily on first use so the server can start (and
# advertise its tools) even before a token is configured. Tests inject their own
# client via ``set_client``.
_client: SlackClient | None = None


def get_client() -> SlackClient:
    global _client
    if _client is None:
        _client = SlackClient()
    return _client


def set_client(client: SlackClient | None) -> None:
    """Override the Slack client (used by tests)."""
    global _client
    _client = client


@mcp.tool()
def lookup_user(email: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """Look up a Slack user by email address or by user ID.

    Provide exactly one of ``email`` or ``user_id``. Returns the user's id, name,
    real_name, email, and whether they are a bot. Use the returned ``id`` with
    ``invite_user_to_channel``.
    """
    try:
        return get_client().lookup_user(email=email, user_id=user_id)
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def lookup_channel(
    name: str | None = None, channel_id: str | None = None
) -> dict[str, Any]:
    """Look up a Slack channel by name or by channel ID.

    Provide exactly one of ``name`` (with or without a leading '#') or
    ``channel_id``. Returns the channel's id, name, whether it is private, whether
    the bot is a member, and member count. Use the returned ``id`` with
    ``invite_user_to_channel``.
    """
    try:
        return get_client().find_channel(name=name, channel_id=channel_id)
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def invite_user_to_channel(channel: str, user: str) -> dict[str, Any]:
    """Invite a user to a Slack channel.

    ``channel`` accepts a channel name (with or without '#') or a channel ID.
    ``user`` accepts an email address or a user ID. Both are resolved
    automatically before inviting.

    Note: the bot must be a member of the target channel to invite others. For
    public channels the bot will self-join automatically if it has the
    'channels:join' scope. Private channels cannot be self-joined — the bot must
    be added to them manually before it can invite anyone.
    """
    client = get_client()
    joined = False
    try:
        resolved_channel = client.find_channel(
            channel_id=channel if _looks_like_channel_id(channel) else None,
            name=None if _looks_like_channel_id(channel) else channel,
        )
        resolved_user = client.lookup_user(
            user_id=user if _looks_like_user_id(user) else None,
            email=None if _looks_like_user_id(user) else user,
        )
        # The bot can only invite others to channels it belongs to. For public
        # channels we can self-join first; private channels must be joined
        # manually, so we let the invite surface the not_in_channel error.
        if (
            resolved_channel.get("is_member") is False
            and not resolved_channel.get("is_private")
        ):
            client.join(resolved_channel["id"])
            joined = True
        client.invite(resolved_channel["id"], [resolved_user["id"]])
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}
    return {
        "ok": True,
        "channel": {"id": resolved_channel["id"], "name": resolved_channel["name"]},
        "user": {"id": resolved_user["id"], "name": resolved_user["name"]},
        "bot_joined_channel": joined,
        "message": (
            f"Invited {resolved_user['name']} ({resolved_user['id']}) to "
            f"#{resolved_channel['name']} ({resolved_channel['id']})."
            + (" (Bot self-joined the channel first.)" if joined else "")
        ),
    }


def _looks_like_channel_id(value: str) -> bool:
    # Slack channel IDs start with C (public), G (private/group) or D (DM).
    return bool(value) and value[0] in "CGD" and value[1:].isalnum() and value.isupper()


def _looks_like_user_id(value: str) -> bool:
    # Slack user IDs start with U or W; emails always contain '@'.
    return bool(value) and "@" not in value and value[0] in "UW" and value.isupper()


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
