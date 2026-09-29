"""Exercise tool payloads through the real SDK, stopping at the HTTP boundary."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from slack_sdk import WebClient

from slack_management_mcp import server
from slack_management_mcp.slack import SlackClient


@pytest.fixture
def transport():
    web = WebClient(token="xoxb-test")
    send = Mock(return_value={"status": 200, "headers": {}, "body": '{"ok": true}'})
    web._perform_urllib_http_request = send
    server.set_client(SlackClient(web))
    yield send
    server.set_client(None)


def request(transport):
    args = transport.call_args.kwargs
    return args["url"].rsplit("/", 1)[-1], args["args"]["json"]


@pytest.mark.parametrize("tool,kwargs,method,payload", [
    (server.create_canvas, {"title": "Notes", "markdown": "# Hello"},
     "canvases.create", {"title": "Notes", "document_content": {"type": "markdown", "markdown": "# Hello"}}),
    (server.lookup_canvas_sections, {"canvas_id": "F1", "contains_text": "Status", "section_types": ["h1"]},
     "canvases.sections.lookup", {"canvas_id": "F1", "criteria": {"contains_text": "Status", "section_types": ["h1"]}}),
    (server.delete_canvas, {"canvas_id": "F1"}, "canvases.delete", {"canvas_id": "F1"}),
    (server.create_list, {"name": "Tasks", "schema": [{"key": "title", "name": "Title", "type": "text"}], "todo_mode": True},
     "slackLists.create", {"name": "Tasks", "schema": [{"key": "title", "name": "Title", "type": "text"}], "todo_mode": True}),
    (server.update_list, {"list_id": "F1", "todo_mode": False, "description_blocks": []},
     "slackLists.update", {"id": "F1", "todo_mode": False, "description_blocks": []}),
    (server.get_list_item, {"list_id": "F1", "item_id": "Rec1"},
     "slackLists.items.info", {"list_id": "F1", "id": "Rec1"}),
    (server.create_list_item, {"list_id": "F1", "initial_fields": [{"column_id": "Col1", "checkbox": False}], "parent_item_id": "Rec1"},
     "slackLists.items.create", {"list_id": "F1", "initial_fields": [{"column_id": "Col1", "checkbox": False}], "parent_item_id": "Rec1"}),
    (server.create_list_item, {"list_id": "F1", "duplicated_item_id": "Rec1"},
     "slackLists.items.create", {"list_id": "F1", "duplicated_item_id": "Rec1"}),
    (server.update_list_items, {"list_id": "F1", "cells": [{"row_id": "Rec1", "column_id": "Col1", "user": ["U1"]}]},
     "slackLists.items.update", {"list_id": "F1", "cells": [{"row_id": "Rec1", "column_id": "Col1", "user": ["U1"]}]}),
    (server.delete_list_item, {"list_id": "F1", "item_id": "Rec1"},
     "slackLists.items.delete", {"list_id": "F1", "id": "Rec1"}),
])
def test_api_contract(transport, tool, kwargs, method, payload):
    assert tool(**kwargs) == {"ok": True}
    assert request(transport) == (method, payload)
    assert transport.call_count == 1


@pytest.mark.parametrize("operation,section", [
    ("insert_at_start", None), ("insert_at_end", None),
    ("insert_before", "temp:C:section"), ("insert_after", "temp:C:section"),
    ("replace", None), ("replace", "temp:C:section"),
    ("delete", "temp:C:section"), ("rename", None),
])
def test_canvas_edit_operations(transport, operation, section):
    markdown = None if operation == "delete" else "Updated"
    assert server.edit_canvas("F1", operation, markdown, section) == {"ok": True}
    method, payload = request(transport)
    assert method == "canvases.edit"
    change = {"operation": operation}
    if section:
        change["section_id"] = section
    if markdown:
        key = "title_content" if operation == "rename" else "document_content"
        change[key] = {"type": "markdown", "markdown": markdown}
    assert payload == {"canvas_id": "F1", "changes": [change]}


@pytest.mark.parametrize("resource", ["canvas", "list"])
@pytest.mark.parametrize("recipient", ["user_ids", "channel_ids"])
@pytest.mark.parametrize("action", ["set", "remove"])
def test_sharing_contract(transport, resource, recipient, action):
    tool = getattr(server, f"{action}_{resource}_access")
    kwargs = {f"{resource}_id": "F1", recipient: ["U1" if recipient == "user_ids" else "C1"]}
    if action == "set":
        kwargs["access_level"] = "write"
    assert tool(**kwargs) == {"ok": True}
    prefix = "canvases" if resource == "canvas" else "slackLists"
    suffix = "set" if action == "set" else "delete"
    assert request(transport) == (f"{prefix}.access.{suffix}", kwargs)


@pytest.mark.parametrize("tool,kwargs", [
    (server.edit_canvas, {"canvas_id": "F1", "operation": "delete"}),
    (server.edit_canvas, {"canvas_id": "F1", "operation": "insert_before", "markdown": "x"}),
    (server.edit_canvas, {"canvas_id": "F1", "operation": "replace"}),
    (server.edit_canvas, {"canvas_id": "F1", "operation": "rename", "markdown": "x", "section_id": "s"}),
    (server.edit_canvas, {"canvas_id": "F1", "operation": "delete", "markdown": "x", "section_id": "s"}),
    (server.lookup_canvas_sections, {"canvas_id": "F1"}),
    (server.update_list, {"list_id": "F1"}),
    (server.get_list_items, {"list_id": "F1", "limit": 0}),
    (server.get_list_items, {"list_id": "F1", "limit": 101}),
    (server.update_list_items, {"list_id": "F1", "cells": []}),
    (server.update_list_items, {"list_id": "F1", "cells": [{}] * 101}),
    (server.update_list_items, {"list_id": "F1", "cells": [{"column_id": "Col1"}]}),
    (server.set_canvas_access, {"canvas_id": "F1", "access_level": "read"}),
    (server.set_list_access, {"list_id": "F1", "access_level": "owner", "channel_ids": ["C1"]}),
    (server.remove_canvas_access, {"canvas_id": "F1", "user_ids": ["U1"], "channel_ids": ["C1"]}),
    (server.remove_list_access, {"list_id": "F1", "user_ids": []}),
])
def test_invalid_requests_do_not_reach_slack(transport, tool, kwargs):
    assert "error" in tool(**kwargs)
    transport.assert_not_called()


def test_pagination_preserves_cursor_schema_and_rows(transport):
    page = {"ok": True, "items": [{"id": "Rec1"}], "list": {"schema": [{"id": "Col1"}]},
            "response_metadata": {"next_cursor": "next-page"}}
    transport.return_value["body"] = json.dumps(page)
    assert server.get_list_items("F1") == page
    assert request(transport)[1] == {"list_id": "F1", "limit": 100, "archived": False, "include_list": True}
    transport.return_value["body"] = '{"ok":true,"items":[],"response_metadata":{"next_cursor":""}}'
    result = server.get_list_items("F1", cursor="next-page", include_list=False)
    assert result["response_metadata"]["next_cursor"] == ""
    assert request(transport)[1]["cursor"] == "next-page"
    assert request(transport)[1]["include_list"] is False


@pytest.mark.parametrize("code", ["missing_scope", "canvas_not_found", "permission_denied", "invalid_list_id"])
def test_slack_errors_are_actionable(transport, code):
    transport.return_value["body"] = json.dumps({"ok": False, "error": code})
    result = server.create_canvas("Notes", "Hello")
    assert result["code"] == code
    assert result["error"]
    if code == "missing_scope":
        assert "reinstall" in result["error"]
    if code == "permission_denied":
        assert "user groups" not in result["error"]


def test_missing_token_is_reported(monkeypatch, tmp_path):
    server.set_client(None)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_AUTH_MODE", raising=False)
    monkeypatch.setenv("SLACK_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    error = server.create_list("Tasks")["error"]
    assert "SLACK_BOT_TOKEN" in error
    assert "login" in error


def test_mcp_registration_schema_and_dispatch(transport):
    async def exercise():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert len(tools) == 20
        assert tools["edit_canvas"].inputSchema["properties"]["operation"]["enum"] == [
            "insert_at_start", "insert_at_end", "insert_before", "insert_after", "replace", "delete", "rename"]
        assert tools["update_list_items"].inputSchema["required"] == ["list_id", "cells"]
        result = await server.mcp.call_tool("create_canvas", {"title": "Notes", "markdown": "Hello"})
        assert result
    asyncio.run(exercise())
    assert request(transport)[0] == "canvases.create"
