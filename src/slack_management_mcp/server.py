"""MCP server exposing a handful of focused Slack tools.

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

    Note: the bot must already be a member of the target channel to invite others.
    It can self-join public channels but must be added manually to private ones.
    """
    client = get_client()
    try:
        resolved_channel = client.find_channel(
            channel_id=channel if _looks_like_channel_id(channel) else None,
            name=None if _looks_like_channel_id(channel) else channel,
        )
        resolved_user = client.lookup_user(
            user_id=user if _looks_like_user_id(user) else None,
            email=None if _looks_like_user_id(user) else user,
        )
        client.invite(resolved_channel["id"], [resolved_user["id"]])
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}
    return {
        "ok": True,
        "channel": {"id": resolved_channel["id"], "name": resolved_channel["name"]},
        "user": {"id": resolved_user["id"], "name": resolved_user["name"]},
        "message": (
            f"Invited {resolved_user['name']} ({resolved_user['id']}) to "
            f"#{resolved_channel['name']} ({resolved_channel['id']})."
        ),
    }


@mcp.tool()
def lookup_usergroup(
    handle: str | None = None, usergroup_id: str | None = None
) -> dict[str, Any]:
    """Look up a Slack user group (a '@'-mentionable group, NOT a channel).

    A *user group* is a named, @-mentionable collection of people (e.g.
    ``@marketing``) used to ping or reference several people at once. This is
    different from a channel.

    Provide exactly one of ``handle`` (the @-mention name, with or without a
    leading '@') or ``usergroup_id`` (starts with 'S'). Returns the group's id,
    handle, name, description, member count, and current member user IDs. Use
    the returned ``id`` or ``handle`` with ``add_users_to_usergroup``.
    """
    try:
        return get_client().find_usergroup(handle=handle, usergroup_id=usergroup_id)
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def add_users_to_usergroup(usergroup: str, users: list[str]) -> dict[str, Any]:
    """Add one or more users to a Slack user group (a '@'-mentionable group).

    A *user group* is a named, @-mentionable collection of people (e.g.
    ``@marketing``) — this is NOT a channel. Use this to grow a group's
    membership.

    ``usergroup`` accepts a group handle (the @-mention name, with or without a
    leading '@') or a user group ID (starts with 'S'). ``users`` is a list of
    email addresses and/or user IDs to add; each is resolved automatically.

    Existing members are preserved — the given users are added to them. (Slack
    has no append API, so this reads the current membership and rewrites it with
    the additions merged in.) Note: user groups are a paid Slack feature.
    """
    if not users:
        return {"error": "Provide at least one user (email or ID) to add."}
    client = get_client()
    try:
        group = client.find_usergroup(
            usergroup_id=usergroup if _looks_like_usergroup_id(usergroup) else None,
            handle=None if _looks_like_usergroup_id(usergroup) else usergroup,
        )
        resolved_users = [
            client.lookup_user(
                user_id=u if _looks_like_user_id(u) else None,
                email=None if _looks_like_user_id(u) else u,
            )
            for u in users
        ]
        updated = client.add_users_to_usergroup(
            group["id"],
            [ru["id"] for ru in resolved_users],
            existing_users=group["users"],
        )
    except (SlackToolError, SlackConfigError) as exc:
        return {"error": str(exc)}
    added = ", ".join(f"{ru['name']} ({ru['id']})" for ru in resolved_users)
    return {
        "ok": True,
        "usergroup": {
            "id": updated["id"],
            "handle": updated["handle"],
            "name": updated["name"],
        },
        "added": [{"id": ru["id"], "name": ru["name"]} for ru in resolved_users],
        "user_count": updated.get("user_count"),
        "message": (
            f"Added {added} to @{updated['handle']} ({updated['id']}). "
            f"Group now has {updated.get('user_count')} members."
        ),
    }


def _looks_like_channel_id(value: str) -> bool:
    # Slack channel IDs start with C (public), G (private/group) or D (DM).
    return bool(value) and value[0] in "CGD" and value[1:].isalnum() and value.isupper()


def _looks_like_user_id(value: str) -> bool:
    # Slack user IDs start with U or W; emails always contain '@'.
    return bool(value) and "@" not in value and value[0] in "UW" and value.isupper()


def _looks_like_usergroup_id(value: str) -> bool:
    # Slack user group (subteam) IDs start with S, e.g. 'S0614TZR7'.
    return bool(value) and value[0] == "S" and value[1:].isalnum() and value.isupper()


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
