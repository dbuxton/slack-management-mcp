"""Thin wrapper around the Slack Web API for the three operations this MCP exposes.

The wrapper centralises construction from ``SLACK_BOT_TOKEN`` and turns Slack's
``SlackApiError`` responses into readable messages so the MCP tools can surface
actionable feedback to the calling model/user.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


class SlackConfigError(RuntimeError):
    """Raised when the server is not configured correctly (e.g. missing token)."""


class SlackToolError(RuntimeError):
    """Raised when a Slack API call fails in a way worth reporting to the caller.

    ``code`` carries Slack's machine-readable error (e.g. ``channel_not_found``)
    when available so tools can branch on it; ``str(self)`` is human readable.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _require_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise SlackConfigError(
            "SLACK_BOT_TOKEN is not set. Create a Slack app, install it to your "
            "workspace, and set SLACK_BOT_TOKEN to the Bot User OAuth Token "
            "(starts with 'xoxb-'). See the README for setup instructions."
        )
    return token


class SlackClient:
    """Small helper around :class:`slack_sdk.WebClient`.

    A pre-built ``client`` can be injected (used by the tests); otherwise one is
    constructed from the ``SLACK_BOT_TOKEN`` environment variable.
    """

    def __init__(self, client: WebClient | None = None) -> None:
        self._client = client or WebClient(token=_require_token())

    # -- users -----------------------------------------------------------------

    def lookup_user(
        self, email: str | None = None, user_id: str | None = None
    ) -> dict[str, Any]:
        if not email and not user_id:
            raise SlackToolError("Provide either 'email' or 'user_id' to look up a user.")
        try:
            if user_id:
                resp = self._client.users_info(user=user_id)
            else:
                resp = self._client.users_lookupByEmail(email=email)
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return _format_user(resp["user"])

    # -- channels --------------------------------------------------------------

    def find_channel(
        self, name: str | None = None, channel_id: str | None = None
    ) -> dict[str, Any]:
        if not name and not channel_id:
            raise SlackToolError(
                "Provide either 'name' or 'channel_id' to look up a channel."
            )
        if channel_id:
            try:
                resp = self._client.conversations_info(channel=channel_id)
            except SlackApiError as exc:
                raise _to_tool_error(exc) from exc
            return _format_channel(resp["channel"])

        target = name.lstrip("#").strip().lower()
        for channel in self._iter_channels():
            if channel.get("name", "").lower() == target:
                return _format_channel(channel)
        raise SlackToolError(
            f"No channel named '{name}' found. Note the bot can only see channels "
            "it is a member of plus public channels in the workspace.",
            code="channel_not_found",
        )

    def _iter_channels(self) -> Iterable[dict[str, Any]]:
        cursor: str | None = None
        while True:
            try:
                resp = self._client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
            except SlackApiError as exc:
                raise _to_tool_error(exc) from exc
            yield from resp.get("channels", [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

    # -- invites ---------------------------------------------------------------

    def invite(self, channel_id: str, user_ids: list[str]) -> dict[str, Any]:
        try:
            resp = self._client.conversations_invite(
                channel=channel_id, users=",".join(user_ids)
            )
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return _format_channel(resp["channel"])

    # -- user groups -----------------------------------------------------------

    def find_usergroup(
        self, handle: str | None = None, usergroup_id: str | None = None
    ) -> dict[str, Any]:
        """Find a user group (the '@'-mentionable kind) by handle or by ID.

        Slack has no ``usergroups.info`` endpoint, so we list all groups and
        match locally. The returned dict includes the current ``users`` so
        callers can merge new members into them.
        """
        if not handle and not usergroup_id:
            raise SlackToolError(
                "Provide either 'handle' or 'usergroup_id' to look up a user group."
            )
        target_handle = handle.lstrip("@").strip().lower() if handle else None
        for group in self._list_usergroups():
            if usergroup_id and group.get("id") == usergroup_id:
                return _format_usergroup(group)
            if target_handle and (
                group.get("handle", "").lower() == target_handle
                or group.get("name", "").lower() == target_handle
            ):
                return _format_usergroup(group)
        ident = usergroup_id or f"@{target_handle}"
        raise SlackToolError(
            f"No user group matching '{ident}' found. User groups are a paid "
            "Slack feature; check the handle (the @-mention name) or the group "
            "ID (starts with 'S').",
            code="usergroup_not_found",
        )

    def _list_usergroups(self) -> list[dict[str, Any]]:
        try:
            resp = self._client.usergroups_list(
                include_users=True, include_disabled=True
            )
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return resp.get("usergroups", [])

    def add_users_to_usergroup(
        self, usergroup_id: str, user_ids: list[str], existing_users: list[str]
    ) -> dict[str, Any]:
        """Add ``user_ids`` to a user group, preserving its current members.

        Slack's ``usergroups.users.update`` *replaces* the whole membership
        list, so we merge the new users with the existing ones (de-duplicated,
        order preserved) to behave like an append.
        """
        merged = list(dict.fromkeys([*existing_users, *user_ids]))
        if not merged:
            raise SlackToolError(
                "Provide at least one user to add to the group.",
                code="no_users_provided",
            )
        try:
            resp = self._client.usergroups_users_update(
                usergroup=usergroup_id, users=",".join(merged)
            )
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return _format_usergroup(resp["usergroup"])


def _format_user(user: dict[str, Any]) -> dict[str, Any]:
    profile = user.get("profile") or {}
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "real_name": user.get("real_name") or profile.get("real_name"),
        "email": profile.get("email"),
        "is_bot": user.get("is_bot", False),
    }


def _format_channel(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.get("id"),
        "name": channel.get("name"),
        "is_private": channel.get("is_private", False),
        "is_member": channel.get("is_member"),
        "num_members": channel.get("num_members"),
    }


def _format_usergroup(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": group.get("id"),
        "handle": group.get("handle"),
        "name": group.get("name"),
        "description": group.get("description"),
        "user_count": group.get("user_count"),
        "is_disabled": bool(group.get("date_delete")),
        "users": list(group.get("users") or []),
    }


# Slack error codes mapped to friendlier guidance. Anything not listed falls back
# to the raw Slack error string.
_FRIENDLY_ERRORS = {
    "users_not_found": "No Slack user matches that email or ID.",
    "user_not_found": "No Slack user matches that ID.",
    "channel_not_found": "No channel matches that name or ID (the bot may not be able to see it).",
    "already_in_channel": "That user is already a member of the channel.",
    "not_in_channel": (
        "The bot is not a member of that channel, so it cannot invite others. "
        "Add the bot to the channel first (it can self-join public channels but "
        "must be added manually to private channels)."
    ),
    "cant_invite_self": "The bot cannot invite itself.",
    "no_users_provided": "Provide at least one user to add to the group.",
    "subteam_not_found": "No user group matches that handle or ID.",
    "permission_denied": (
        "The bot is not allowed to manage user groups. User groups are a paid "
        "Slack feature and require the 'usergroups:write' scope."
    ),
    "missing_scope": (
        "The bot token is missing a required OAuth scope. Check the scopes listed "
        "in the README and reinstall the app."
    ),
    "not_authed": "No valid SLACK_BOT_TOKEN was supplied.",
    "invalid_auth": "The SLACK_BOT_TOKEN is invalid or has been revoked.",
}


def _to_tool_error(exc: SlackApiError) -> SlackToolError:
    code = ""
    if exc.response is not None:
        code = exc.response.get("error", "") or ""
    message = _FRIENDLY_ERRORS.get(code) or f"Slack API error: {code or exc}"
    return SlackToolError(message, code=code or None)
