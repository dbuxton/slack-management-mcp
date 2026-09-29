"""MCP server exposing a handful of focused Slack tools.

Run over stdio. Authenticates with ``SLACK_BOT_TOKEN`` (act as the bot) or with
a user token from ``slack-management-mcp login`` / ``SLACK_USER_TOKEN`` (act as
that user). See the README for setup.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .auth import SlackConfigError, delete_user_credentials
from .oauth import login
from .slack import SlackClient, SlackToolError

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
    the acting account is a member, and member count. Use the returned ``id`` with
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

    The acting Slack account must already be a member of the target channel.
    For a bot token, add the bot to private channels manually (it can self-join
    public channels). For user OAuth, join the channel yourself first.
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


def _call(method: str, **payload: Any) -> dict[str, Any]:
    try:
        return get_client().call(method, **payload)
    except (SlackToolError, SlackConfigError) as exc:
        result = {"error": str(exc)}
        if isinstance(exc, SlackToolError) and exc.code:
            result["code"] = exc.code
        return result


@mcp.tool()
def create_canvas(title: str, markdown: str) -> dict[str, Any]:
    """Create a standalone Slack canvas from Markdown; returns its canvas_id.

    The acting Slack account owns the new canvas (the bot, or your user when
    signed in with OAuth). Use set_canvas_access to share it.
    """
    return _call("canvases.create", title=title,
                 document_content={"type": "markdown", "markdown": markdown})


@mcp.tool()
def edit_canvas(
    canvas_id: str,
    operation: Literal["insert_at_start", "insert_at_end", "insert_before",
                       "insert_after", "replace", "delete", "rename"],
    markdown: str | None = None,
    section_id: str | None = None,
) -> dict[str, Any]:
    """Apply one canvas edit. Markdown supplies new content or the renamed title.

    insert_before/insert_after/delete require section_id from lookup_canvas_sections.
    replace without section_id REPLACES THE ENTIRE CANVAS. delete removes only
    the specified section; delete_canvas removes the entire canvas permanently.
    insert_at_start/insert_at_end/rename do not accept section_id.
    """
    if operation not in {"insert_at_start", "insert_at_end", "insert_before",
                         "insert_after", "replace", "delete", "rename"}:
        return {"error": "Unsupported canvas operation."}
    if operation in {"insert_before", "insert_after", "delete"} and not section_id:
        return {"error": f"{operation} requires section_id."}
    if operation in {"insert_at_start", "insert_at_end", "rename"} and section_id is not None:
        return {"error": f"{operation} does not accept section_id."}
    if operation != "delete" and markdown is None:
        return {"error": f"{operation} requires markdown."}
    if operation == "delete" and markdown is not None:
        return {"error": "delete does not accept markdown."}
    change: dict[str, Any] = {"operation": operation}
    if section_id is not None:
        change["section_id"] = section_id
    if markdown is not None:
        key = "title_content" if operation == "rename" else "document_content"
        change[key] = {"type": "markdown", "markdown": markdown}
    return _call("canvases.edit", canvas_id=canvas_id, changes=[change])


@mcp.tool()
def lookup_canvas_sections(
    canvas_id: str, contains_text: str | None = None,
    section_types: list[str] | None = None,
) -> dict[str, Any]:
    """Find canvas section IDs for targeted edits (does not return canvas text).

    Filter by text and/or section types such as any_header, h1, h2, h3, list,
    table, blockquote. Omit section_types to search across types.
    """
    criteria = {k: v for k, v in {"contains_text": contains_text,
                                 "section_types": section_types}.items() if v is not None}
    if not criteria:
        return {"error": "Provide contains_text or section_types."}
    return _call("canvases.sections.lookup", canvas_id=canvas_id, criteria=criteria)


@mcp.tool()
def delete_canvas(canvas_id: str) -> dict[str, Any]:
    """Permanently delete a whole canvas. Slack cannot restore it."""
    return _call("canvases.delete", canvas_id=canvas_id)


def _access(method: str, id_key: str, resource_id: str,
            access_level: str | None, user_ids: list[str] | None,
            channel_ids: list[str] | None) -> dict[str, Any]:
    if bool(user_ids) == bool(channel_ids):
        return {"error": "Provide exactly one non-empty user_ids or channel_ids list."}
    if access_level is not None and access_level not in {"read", "write", "owner"}:
        return {"error": "access_level must be read, write, or owner."}
    if access_level == "owner" and channel_ids:
        return {"error": "Only users can be owners."}
    return _call(method, **{id_key: resource_id}, access_level=access_level,
                 user_ids=user_ids or None, channel_ids=channel_ids or None)


@mcp.tool()
def set_canvas_access(
    canvas_id: str, access_level: Literal["read", "write", "owner"],
    user_ids: list[str] | None = None, channel_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Grant canvas access to users OR channels by ID; owner transfers ownership."""
    return _access("canvases.access.set", "canvas_id", canvas_id,
                   access_level, user_ids, channel_ids)


@mcp.tool()
def remove_canvas_access(
    canvas_id: str, user_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Remove canvas access for users OR channels by ID."""
    return _access("canvases.access.delete", "canvas_id", canvas_id,
                   None, user_ids, channel_ids)


@mcp.tool()
def create_list(
    name: str, schema: list[dict[str, Any]] | None = None,
    description_blocks: list[dict[str, Any]] | None = None,
    todo_mode: bool = False,
) -> dict[str, Any]:
    """Create a Slack List; returns its ID and column schema. Requires paid Slack.

    schema columns use key, name, type (text, user, date, select, etc.) and optional
    is_primary_column/options. Omit schema for a default text column. todo_mode
    adds completed, assignee and due-date fields. description_blocks uses Slack
    rich_text blocks. Use returned column IDs (not schema keys) to populate rows.
    """
    return _call("slackLists.create", name=name, schema=schema,
                 description_blocks=description_blocks, todo_mode=todo_mode)


@mcp.tool()
def update_list(
    list_id: str, name: str | None = None,
    description_blocks: list[dict[str, Any]] | None = None,
    todo_mode: bool | None = None,
) -> dict[str, Any]:
    """Update a List's name, rich-text description or task-tracking mode.

    This does not change column schema. Omitted properties are left unchanged.
    """
    if name is None and description_blocks is None and todo_mode is None:
        return {"error": "Provide name, description_blocks, or todo_mode."}
    return _call("slackLists.update", id=list_id, name=name,
                 description_blocks=description_blocks, todo_mode=todo_mode)


@mcp.tool()
def get_list_items(
    list_id: str, cursor: str | None = None, limit: int = 100,
    archived: bool = False, include_list: bool = True,
) -> dict[str, Any]:
    """Read one page of List rows, including column schema by default.

    Pass response_metadata.next_cursor back as cursor until empty for more rows.
    archived selects archived rows instead of normal rows. Use column IDs from
    the returned list schema when creating/updating items.
    """
    if limit < 1 or limit > 100:
        return {"error": "limit must be between 1 and 100."}
    return _call("slackLists.items.list", list_id=list_id, cursor=cursor,
                 limit=limit, archived=archived, include_list=include_list)


@mcp.tool()
def get_list_item(list_id: str, item_id: str) -> dict[str, Any]:
    """Read a single Slack List row by its Rec... item ID."""
    return _call("slackLists.items.info", list_id=list_id, id=item_id)


@mcp.tool()
def create_list_item(
    list_id: str, initial_fields: list[dict[str, Any]] | None = None,
    parent_item_id: str | None = None,
    duplicated_item_id: str | None = None,
) -> dict[str, Any]:
    """Create a List row, subtask, or copy of an existing row.

    initial_fields entries require column_id and a typed value: rich_text (Block
    Kit blocks, NOT a plain text property), user (user ID array), date (YYYY-MM-DD
    array), select (option ID array), checkbox (boolean), etc. Get column IDs
    from create_list or get_list_items(include_list=True). parent_item_id creates
    a subtask; duplicated_item_id copies a row. Returns the new item's ID.
    """
    return _call("slackLists.items.create", list_id=list_id,
                 initial_fields=initial_fields, parent_item_id=parent_item_id,
                 duplicated_item_id=duplicated_item_id)


@mcp.tool()
def update_list_items(list_id: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Update 1–100 List cells, potentially across multiple rows.

    Each cell needs row_id (the Rec... item ID), column_id, and its typed value
    (rich_text, user, date, select, checkbox, etc.). Text requires Block Kit
    rich_text blocks, not a plain text property. Only supplied cells change.
    """
    if not 1 <= len(cells) <= 100:
        return {"error": "Provide between 1 and 100 cells."}
    if any(not cell.get("row_id") or not cell.get("column_id") for cell in cells):
        return {"error": "Every cell requires row_id and column_id."}
    return _call("slackLists.items.update", list_id=list_id, cells=cells)


@mcp.tool()
def delete_list_item(list_id: str, item_id: str) -> dict[str, Any]:
    """Delete a row from a Slack List by its Rec... item ID."""
    return _call("slackLists.items.delete", list_id=list_id, id=item_id)


@mcp.tool()
def set_list_access(
    list_id: str, access_level: Literal["read", "write", "owner"],
    user_ids: list[str] | None = None, channel_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Grant List access to users OR channels by ID; owner transfers ownership."""
    return _access("slackLists.access.set", "list_id", list_id,
                   access_level, user_ids, channel_ids)


@mcp.tool()
def remove_list_access(
    list_id: str, user_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Remove List access for users OR channels by ID."""
    return _access("slackLists.access.delete", "list_id", list_id,
                   None, user_ids, channel_ids)


def _looks_like_channel_id(value: str) -> bool:
    # Slack channel IDs start with C (public), G (private/group) or D (DM).
    return bool(value) and value[0] in "CGD" and value[1:].isalnum() and value.isupper()


def _looks_like_user_id(value: str) -> bool:
    # Slack user IDs start with U or W; emails always contain '@'.
    return bool(value) and "@" not in value and value[0] in "UW" and value.isupper()


def _looks_like_usergroup_id(value: str) -> bool:
    # Slack user group (subteam) IDs start with S, e.g. 'S0614TZR7'.
    return bool(value) and value[0] == "S" and value[1:].isalnum() and value.isupper()


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point.

    With no arguments, run the MCP server over stdio. ``login`` and ``logout``
    manage the saved user OAuth token and do not start the server.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        mcp.run()
        return
    if args == ["login"]:
        try:
            login()
        except SlackConfigError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        return
    if args == ["logout"]:
        if delete_user_credentials():
            print("Removed saved Slack user credentials.")
        else:
            print("No saved Slack user credentials to remove.")
        return
    print("Usage: slack-management-mcp [login|logout]", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
