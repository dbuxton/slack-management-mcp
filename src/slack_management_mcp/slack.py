"""Thin wrapper around the Slack Web API for the operations this MCP exposes.

The wrapper builds a client from the bot token or a user OAuth token and turns
Slack's ``SlackApiError`` responses into readable messages so the MCP tools can
surface actionable feedback to the calling model/user.
"""

from __future__ import annotations

from typing import Any, Iterable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .auth import SlackAuth, SlackConfigError, resolve_auth


class SlackToolError(RuntimeError):
    """Raised when a Slack API call fails in a way worth reporting to the caller.

    ``code`` carries Slack's machine-readable error (e.g. ``channel_not_found``)
    when available so tools can branch on it; ``str(self)`` is human readable.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class SlackClient:
    """Small helper around :class:`slack_sdk.WebClient`.

    A pre-built ``client`` can be injected (used by the tests); otherwise one is
    constructed from :func:`resolve_auth` (bot token, ``SLACK_USER_TOKEN``, or
    the saved user OAuth login). ``identity`` is ``bot`` or ``user``.
    """

    def __init__(
        self, client: WebClient | None = None, *, identity: str | None = None
    ) -> None:
        if client is not None:
            self._client = client
            self.identity = identity or "bot"
            return
        auth: SlackAuth = resolve_auth()
        self._client = WebClient(token=auth.token)
        self.identity = auth.identity

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        """Call a fixed endpoint supplied by a tool, preserving Slack metadata.

        JSON is required for nested canvas changes and list field values. Only
        None is omitted: false, empty strings and arrays may be meaningful.
        """
        try:
            response = self._client.api_call(
                method, json={key: value for key, value in payload.items() if value is not None}
            )
        except SlackApiError as exc:
            raise _to_tool_error(exc, self.identity) from exc
        return dict(response.data)

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
            raise _to_tool_error(exc, self.identity) from exc
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
                raise _to_tool_error(exc, self.identity) from exc
            return _format_channel(resp["channel"])

        target = name.lstrip("#").strip().lower()
        for channel in self._iter_channels():
            if channel.get("name", "").lower() == target:
                return _format_channel(channel)
        raise SlackToolError(
            f"No channel named '{name}' found. {_channel_visibility_note(self.identity)}",
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
                raise _to_tool_error(exc, self.identity) from exc
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
            raise _to_tool_error(exc, self.identity) from exc
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
            raise _to_tool_error(exc, self.identity) from exc
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
            raise _to_tool_error(exc, self.identity) from exc
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


# Slack error codes that do not depend on whether the token is a bot or a user.
_STATIC_ERRORS = {
    "users_not_found": "No Slack user matches that email or ID.",
    "user_not_found": "No Slack user matches that ID.",
    "already_in_channel": "That user is already a member of the channel.",
    "no_users_provided": "Provide at least one user to add to the group.",
    "subteam_not_found": "No user group matches that handle or ID.",
}


def _channel_visibility_note(identity: str) -> str:
    if identity == "user":
        return (
            "Note your Slack user can only see channels they are a member of "
            "plus public channels in the workspace."
        )
    return (
        "Note the bot can only see channels it is a member of plus public "
        "channels in the workspace."
    )


def _friendly_error(code: str, identity: str) -> str | None:
    """Map a Slack error code to guidance for the acting bot or user."""
    static = _STATIC_ERRORS.get(code)
    if static:
        return static
    if identity == "user":
        return _USER_ERRORS.get(code)
    return _BOT_ERRORS.get(code)


_BOT_ERRORS = {
    "channel_not_found": (
        "No channel matches that name or ID (the bot may not be able to see it)."
    ),
    "not_in_channel": (
        "The bot is not a member of that channel, so it cannot invite others. "
        "Add the bot to the channel first (it can self-join public channels but "
        "must be added manually to private channels)."
    ),
    "cant_invite_self": "The bot cannot invite itself.",
    "permission_denied": (
        "The bot is not allowed to perform this action. Check its access to the "
        "resource and the required OAuth scopes."
    ),
    "missing_scope": (
        "The bot token is missing a required OAuth scope. Check the scopes listed "
        "in the README and reinstall the app."
    ),
    "not_authed": "No valid SLACK_BOT_TOKEN was supplied.",
    "invalid_auth": "The SLACK_BOT_TOKEN is invalid or has been revoked.",
}

_USER_ERRORS = {
    "channel_not_found": (
        "No channel matches that name or ID (your Slack user may not be able to see it)."
    ),
    "not_in_channel": (
        "Your Slack user is not a member of that channel, so they cannot invite "
        "others. Join the channel first."
    ),
    "cant_invite_self": "Your Slack user cannot invite themselves.",
    "permission_denied": (
        "Your Slack user is not allowed to perform this action. Check their access "
        "to the resource and the required OAuth scopes."
    ),
    "missing_scope": (
        "Your user token is missing a required OAuth scope. Check the scopes listed "
        "in the README and run `slack-management-mcp login` again."
    ),
    "not_authed": "No valid user token was supplied.",
    "invalid_auth": "The user token is invalid or has been revoked.",
}


def _to_tool_error(exc: SlackApiError, identity: str) -> SlackToolError:
    code = ""
    if exc.response is not None:
        code = exc.response.get("error", "") or ""
    message = _friendly_error(code, identity) or f"Slack API error: {code or exc}"
    return SlackToolError(message, code=code or None)
