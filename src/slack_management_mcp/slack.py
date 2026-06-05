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

    # -- membership ------------------------------------------------------------

    def join(self, channel_id: str) -> dict[str, Any]:
        """Add the bot to a public channel (``conversations.join``).

        Only works for public channels and requires the ``channels:join`` scope.
        Private channels cannot be self-joined — the bot must be added by a member.
        """
        try:
            resp = self._client.conversations_join(channel=channel_id)
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return _format_channel(resp["channel"])

    # -- invites ---------------------------------------------------------------

    def invite(self, channel_id: str, user_ids: list[str]) -> dict[str, Any]:
        try:
            resp = self._client.conversations_invite(
                channel=channel_id, users=",".join(user_ids)
            )
        except SlackApiError as exc:
            raise _to_tool_error(exc) from exc
        return _format_channel(resp["channel"])


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


# Slack error codes mapped to friendlier guidance. Anything not listed falls back
# to the raw Slack error string.
_FRIENDLY_ERRORS = {
    "users_not_found": "No Slack user matches that email or ID.",
    "user_not_found": "No Slack user matches that ID.",
    "channel_not_found": "No channel matches that name or ID (the bot may not be able to see it).",
    "already_in_channel": "That user is already a member of the channel.",
    "not_in_channel": (
        "The bot is not a member of that channel, so it cannot invite others. "
        "Add the bot to the channel first by mentioning or inviting it from "
        "within the channel (e.g. '/invite @your-bot'). For public channels the "
        "bot can self-join only if it has the 'channels:join' scope."
    ),
    "cant_invite_self": "The bot cannot invite itself.",
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
