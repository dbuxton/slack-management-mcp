"""Unit tests for the Slack tools with a mocked Slack WebClient (no network)."""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from slack_management_mcp import server
from slack_management_mcp.slack import SlackClient


class FakeResponse(dict):
    """Mimics slack_sdk's SlackResponse, which behaves like a dict."""


class FakeWebClient:
    """Records calls and returns canned responses / raises canned errors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    # Each method optionally raises a SlackApiError by inspecting class attrs set
    # per-test; by default returns successful canned data.

    def users_lookupByEmail(self, email):  # noqa: N802 (matches slack_sdk name)
        self.calls.append(("users_lookupByEmail", {"email": email}))
        if email == "missing@example.com":
            raise _api_error("users_not_found")
        return FakeResponse(
            user={
                "id": "U123",
                "name": "alice",
                "real_name": "Alice Example",
                "profile": {"email": email},
                "is_bot": False,
            }
        )

    def users_info(self, user):  # noqa: N802
        self.calls.append(("users_info", {"user": user}))
        return FakeResponse(
            user={"id": user, "name": "alice", "profile": {"email": "a@example.com"}}
        )

    def conversations_info(self, channel):  # noqa: N802
        self.calls.append(("conversations_info", {"channel": channel}))
        return FakeResponse(
            channel={"id": channel, "name": "general", "is_private": False}
        )

    def conversations_list(self, **kwargs):  # noqa: N802
        self.calls.append(("conversations_list", kwargs))
        cursor = kwargs.get("cursor")
        if not cursor:
            return FakeResponse(
                channels=[{"id": "C001", "name": "random"}],
                response_metadata={"next_cursor": "page2"},
            )
        return FakeResponse(
            channels=[{"id": "C002", "name": "engineering", "is_private": False}],
            response_metadata={"next_cursor": ""},
        )

    def conversations_invite(self, channel, users):  # noqa: N802
        self.calls.append(("conversations_invite", {"channel": channel, "users": users}))
        if channel == "C_ALREADY":
            raise _api_error("already_in_channel")
        return FakeResponse(channel={"id": channel, "name": "engineering"})


def _api_error(code: str) -> SlackApiError:
    return SlackApiError(message=code, response=FakeResponse(ok=False, error=code))


@pytest.fixture
def fake_client():
    fake = FakeWebClient()
    client = SlackClient(client=fake)
    server.set_client(client)
    yield fake, client
    server.set_client(None)


def test_lookup_user_by_email(fake_client):
    _, client = fake_client
    result = client.lookup_user(email="alice@example.com")
    assert result["id"] == "U123"
    assert result["email"] == "alice@example.com"


def test_lookup_user_not_found_maps_to_friendly_error(fake_client):
    result = server.lookup_user(email="missing@example.com")
    assert "No Slack user" in result["error"]


def test_find_channel_by_name_paginates(fake_client):
    _, client = fake_client
    result = client.find_channel(name="#engineering")
    assert result["id"] == "C002"


def test_find_channel_not_found(fake_client):
    result = server.lookup_channel(name="nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]


def test_invite_success_resolves_ids(fake_client):
    fake, _ = fake_client
    result = server.invite_user_to_channel(channel="engineering", user="alice@example.com")
    assert result["ok"] is True
    assert result["channel"]["id"] == "C002"
    assert result["user"]["id"] == "U123"
    invited = [c for c in fake.calls if c[0] == "conversations_invite"]
    assert invited and invited[0][1]["users"] == "U123"


def test_invite_already_in_channel_error_mapping(fake_client):
    _, client = fake_client
    with pytest.raises(Exception) as excinfo:
        client.invite("C_ALREADY", ["U123"])
    assert "already a member" in str(excinfo.value)
