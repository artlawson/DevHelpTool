from unittest.mock import AsyncMock

from app.agent.orchestrator import OrchestratorUnavailable
from app.core.models import AskResponse, Issue, ToolResult
from app.slack import bolt_app


def _issue(key: str) -> Issue:
    return Issue(
        key=key,
        summary=f"Summary for {key}",
        priority="High",
        status="Open",
        due_date=None,
        has_linked_pr=False,
        priority_score=3.0,
    )


def test_strip_bot_mention_removes_leading_mention_token():
    text = "<@U012ABCDEF> what should I work on today?"

    assert bolt_app._strip_bot_mention(text) == "what should I work on today?"


def test_strip_bot_mention_leaves_plain_text_unchanged():
    text = "what should I work on today?"

    assert bolt_app._strip_bot_mention(text) == text


async def test_handle_mention_replies_in_thread_with_formatted_answer(monkeypatch):
    response = AskResponse(
        answer="You have 1 high priority issue.", tool_calls=[], warnings=[]
    )
    mock_handle_query = AsyncMock(return_value=response)
    monkeypatch.setattr(bolt_app, "handle_query", mock_handle_query)

    say = AsyncMock()
    event = {"text": "<@U012ABCDEF> what should I work on today?", "ts": "123.456"}

    await bolt_app.handle_mention(event, say)

    mock_handle_query.assert_awaited_once_with("what should I work on today?")
    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "123.456"
    assert kwargs["blocks"][0]["text"]["text"] == "You have 1 high priority issue."


async def test_handle_mention_replies_with_apology_when_orchestrator_unavailable(
    monkeypatch,
):
    mock_handle_query = AsyncMock(side_effect=OrchestratorUnavailable("boom"))
    monkeypatch.setattr(bolt_app, "handle_query", mock_handle_query)

    say = AsyncMock()
    event = {"text": "<@U012ABCDEF> status?", "ts": "789.012"}

    await bolt_app.handle_mention(event, say)

    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "789.012"
    assert "couldn't reach the assistant" in kwargs["text"]


async def test_ack_only_acknowledges_the_interaction():
    ack = AsyncMock()

    await bolt_app._ack_only(ack)

    ack.assert_awaited_once()


async def test_handle_standup_summary_request_acks_and_replies_in_the_root_thread(
    monkeypatch,
):
    high_priority_result = ToolResult(ok=True, data=[_issue("AL-1")], error=None)
    reviewing_result = ToolResult(ok=True, data=[], error=None)
    monkeypatch.setattr(
        bolt_app.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=high_priority_result),
    )
    monkeypatch.setattr(
        bolt_app.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=reviewing_result),
    )

    ack = AsyncMock()
    say = AsyncMock()
    # A button click's message is the threaded reply itself, so the root of the
    # conversation is its own thread_ts, not the click's message ts.
    body = {"message": {"ts": "222.000", "thread_ts": "111.000"}}

    await bolt_app.handle_standup_summary_request(ack, body, say)

    ack.assert_awaited_once()
    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "111.000"
    assert "Doing" in kwargs["blocks"][0]["text"]["text"]


async def test_handle_standup_summary_request_falls_back_to_message_ts_when_untreaded(
    monkeypatch,
):
    monkeypatch.setattr(
        bolt_app.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )
    monkeypatch.setattr(
        bolt_app.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )

    ack = AsyncMock()
    say = AsyncMock()
    body = {"message": {"ts": "333.000"}}

    await bolt_app.handle_standup_summary_request(ack, body, say)

    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "333.000"


async def test_handle_standup_summary_request_degrades_gracefully_on_tool_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        bolt_app.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=False, data=None, error="boom")),
    )
    monkeypatch.setattr(
        bolt_app.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )

    ack = AsyncMock()
    say = AsyncMock()
    body = {"message": {"ts": "444.000"}}

    await bolt_app.handle_standup_summary_request(ack, body, say)

    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert "Nothing high-priority" in kwargs["blocks"][0]["text"]["text"]
