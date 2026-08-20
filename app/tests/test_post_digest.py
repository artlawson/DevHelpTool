"""Integration test for scripts/post_digest.py - exercises the real
app.tools.jira_tools functions and app.clients.jira_client together (only the
outbound Jira HTTP call and the Slack post are mocked), the same "wiring, not
just units" style as app/tests/test_integration.py."""

from unittest.mock import AsyncMock

import httpx
import respx

import scripts.post_digest as post_digest
from app.config import settings
from app.core.cache import TTLCache
from app.core.models import ToolResult
from app.tests.test_jira_tools import RAW_ISSUE
from app.tools import jira_tools


def _reset_jira_cache(monkeypatch) -> None:
    monkeypatch.setattr(jira_tools.jira_client, "_cache", TTLCache())


async def test_post_digest_posts_real_data_end_to_end(monkeypatch):
    _reset_jira_cache(monkeypatch)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", "C12345")

    mock_post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(post_digest.AsyncWebClient, "chat_postMessage", mock_post)

    with respx.mock:
        respx.get(f"{settings.jira_base_url}/rest/api/3/search/jql").mock(
            return_value=httpx.Response(200, json={"issues": [RAW_ISSUE]})
        )
        respx.get(f"{settings.jira_base_url}/rest/agile/1.0/board").mock(
            return_value=httpx.Response(200, json={"values": [{"id": 1}]})
        )
        respx.get(f"{settings.jira_base_url}/rest/agile/1.0/board/1/sprint").mock(
            return_value=httpx.Response(
                200, json={"values": [{"id": 99, "name": "Sprint X"}]}
            )
        )
        # get_my_issues_with_linked_prs (used for the digest's High Priority
        # list, so its linked_pr data is available) also hits GitHub's search
        # API internally - unmocked, this would raise inside its try/except and
        # silently degrade to an empty list, masking whether High Priority
        # actually got populated via the intended path.
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        exit_code = await post_digest.main()

    assert exit_code == 0
    mock_post.assert_awaited_once()
    _, kwargs = mock_post.call_args
    assert kwargs["channel"] == "C12345"
    rendered = str(kwargs["blocks"])
    assert RAW_ISSUE["key"] in rendered


async def test_post_digest_exits_nonzero_when_channel_id_missing(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", None)

    mock_post = AsyncMock()
    monkeypatch.setattr(post_digest.AsyncWebClient, "chat_postMessage", mock_post)

    exit_code = await post_digest.main()

    assert exit_code == 1
    mock_post.assert_not_awaited()


async def test_post_digest_degrades_gracefully_when_one_tool_fails(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", "C12345")

    failing = AsyncMock(
        return_value=ToolResult(ok=False, data=None, error="500 Internal Server Error")
    )
    empty_ok = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setattr(jira_tools, "get_my_issues_with_linked_prs", failing)
    monkeypatch.setattr(jira_tools, "get_lower_priority_issues_due_soon", empty_ok)
    monkeypatch.setattr(jira_tools, "get_current_sprint_issues", empty_ok)
    monkeypatch.setattr(jira_tools, "get_backlog_issues_needing_details", empty_ok)

    mock_post = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(post_digest.AsyncWebClient, "chat_postMessage", mock_post)

    exit_code = await post_digest.main()

    assert exit_code == 0
    mock_post.assert_awaited_once()


async def test_post_digest_exits_nonzero_when_slack_post_fails(monkeypatch):
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_channel_id", "C12345")

    empty_ok = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setattr(jira_tools, "get_my_issues_with_linked_prs", empty_ok)
    monkeypatch.setattr(jira_tools, "get_lower_priority_issues_due_soon", empty_ok)
    monkeypatch.setattr(jira_tools, "get_current_sprint_issues", empty_ok)
    monkeypatch.setattr(jira_tools, "get_backlog_issues_needing_details", empty_ok)

    mock_post = AsyncMock(side_effect=RuntimeError("network error"))
    monkeypatch.setattr(post_digest.AsyncWebClient, "chat_postMessage", mock_post)

    exit_code = await post_digest.main()

    assert exit_code == 1
    mock_post.assert_awaited_once()
