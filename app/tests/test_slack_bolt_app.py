import asyncio
import json
from collections import defaultdict
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
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    response = AskResponse(
        answer="You have 1 high priority issue.", tool_calls=[], warnings=[]
    )
    mock_handle_conversational_query = AsyncMock(return_value=(response, []))
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {"text": "<@U012ABCDEF> what should I work on today?", "ts": "123.456"}

    await bolt_app.handle_mention(event, say)

    mock_handle_conversational_query.assert_awaited_once_with(
        "what should I work on today?", None
    )
    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "123.456"
    assert kwargs["blocks"][0]["text"]["text"] == "You have 1 high priority issue."


async def test_handle_mention_replies_with_apology_when_orchestrator_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    mock_handle_conversational_query = AsyncMock(
        side_effect=OrchestratorUnavailable("boom")
    )
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {"text": "<@U012ABCDEF> status?", "ts": "789.012"}

    await bolt_app.handle_mention(event, say)

    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "789.012"
    assert "couldn't reach the assistant" in kwargs["text"]


async def test_handle_mention_uses_thread_root_not_own_ts_when_already_in_a_thread(
    monkeypatch,
):
    # Regression test: a second @-mention inside an already-open thread must
    # reply rooted at the thread, not fork a new one at its own ts.
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    response = AskResponse(answer="continuing...", tool_calls=[], warnings=[])
    monkeypatch.setattr(
        bolt_app,
        "handle_conversational_query",
        AsyncMock(return_value=(response, [])),
    )

    say = AsyncMock()
    event = {
        "text": "<@U012ABCDEF> and what about AL-2?",
        "ts": "999.999",
        "thread_ts": "111.000",
    }

    await bolt_app.handle_mention(event, say)

    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "111.000"


async def test_handle_mention_stores_and_reuses_thread_history(monkeypatch):
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    history_after_1 = [{"role": "user", "content": "first question"}]
    response1 = AskResponse(answer="answer 1", tool_calls=[], warnings=[])
    response2 = AskResponse(answer="answer 2", tool_calls=[], warnings=[])
    mock_handle_conversational_query = AsyncMock(
        side_effect=[(response1, history_after_1), (response2, [])]
    )
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {"text": "<@U012ABCDEF> first question", "ts": "111.000"}
    await bolt_app.handle_mention(event, say)

    event2 = {
        "text": "<@U012ABCDEF> second question",
        "ts": "222.000",
        "thread_ts": "111.000",
    }
    await bolt_app.handle_mention(event2, say)

    first_call, second_call = mock_handle_conversational_query.await_args_list
    assert first_call.args == ("first question", None)
    assert second_call.args == ("second question", history_after_1)


async def test_answer_and_reply_serializes_concurrent_calls_to_the_same_thread(
    monkeypatch,
):
    # Regression test for a real race: Socket Mode dispatches every event as
    # its own concurrent task, so two messages landing in the same thread
    # close together could both read the same pre-update history and the
    # slower write would silently clobber the faster one. The per-thread
    # lock in _answer_and_reply must serialize them instead.
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    monkeypatch.setattr(bolt_app, "_thread_locks", defaultdict(asyncio.Lock))

    calls_seen: list[tuple[str, list[dict] | None]] = []

    async def fake_handle_conversational_query(query, history):
        calls_seen.append((query, history))
        await asyncio.sleep(0.01)  # simulates the LLM round trip
        new_history = [*(history or []), {"role": "user", "content": query}]
        response = AskResponse(answer=f"answer to {query}", tool_calls=[], warnings=[])
        return response, new_history

    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", fake_handle_conversational_query
    )
    say = AsyncMock()

    await asyncio.gather(
        bolt_app._answer_and_reply("first", "111.000", say),
        bolt_app._answer_and_reply("second", "111.000", say),
    )

    assert len(calls_seen) == 2
    histories_seen = [history for _, history in calls_seen]
    # Without the lock, both calls would race and both see history=None -
    # with it, whichever runs second must see the first call's completed
    # one-entry history, proving the two ran serialized, not concurrently.
    assert histories_seen.count(None) == 1
    other_history = next(h for h in histories_seen if h is not None)
    assert len(other_history) == 1


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


async def test_handle_confirm_comment_acks_decodes_value_and_posts(monkeypatch):
    mock_post_comment = AsyncMock(
        return_value=ToolResult(ok=True, data="1", error=None)
    )
    monkeypatch.setattr(bolt_app.jira_tools, "post_comment", mock_post_comment)

    ack = AsyncMock()
    respond = AsyncMock()
    value = json.dumps({"issue_key": "AL-13", "note_text": "quick thought here"})
    body = {
        "actions": [{"value": value}],
        "message": {
            "ts": "222.000",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "the answer"}},
                {"type": "divider", "block_id": "comment_draft_divider"},
                {"type": "section", "block_id": "comment_draft_section", "text": {}},
                {
                    "type": "actions",
                    "block_id": "comment_draft_actions",
                    "elements": [],
                },
            ],
        },
    }

    await bolt_app.handle_confirm_comment(ack, body, respond)

    ack.assert_awaited_once()
    mock_post_comment.assert_awaited_once_with("AL-13", "quick thought here")

    # Strips the draft's buttons via response_url (closing the
    # double-click-to-double-post window) while preserving the rest of the
    # original message - the answer text block must survive.
    respond.assert_awaited_once()
    _, respond_kwargs = respond.call_args
    assert respond_kwargs["replace_original"] is True
    assert "Comment posted to AL-13." in respond_kwargs["text"]
    new_blocks = respond_kwargs["blocks"]
    assert new_blocks[0]["text"]["text"] == "the answer"
    remaining_block_ids = [b.get("block_id", "") for b in new_blocks]
    assert not any(bid.startswith("comment_draft") for bid in remaining_block_ids)
    assert not any(b["type"] == "actions" for b in new_blocks)


async def test_handle_confirm_comment_replies_with_sanitized_error_on_failure(
    monkeypatch,
):
    mock_post_comment = AsyncMock(
        return_value=ToolResult(ok=False, data=None, error="404 Not Found")
    )
    monkeypatch.setattr(bolt_app.jira_tools, "post_comment", mock_post_comment)

    ack = AsyncMock()
    respond = AsyncMock()
    value = json.dumps({"issue_key": "AL-13", "note_text": "quick thought here"})
    body = {"actions": [{"value": value}], "message": {"ts": "555.000", "blocks": []}}

    await bolt_app.handle_confirm_comment(ack, body, respond)

    _, respond_kwargs = respond.call_args
    assert "404 Not Found" in respond_kwargs["text"]


async def test_handle_thread_reply_ignores_messages_in_untracked_threads(monkeypatch):
    monkeypatch.setattr(bolt_app, "_thread_histories", {})
    mock_handle_conversational_query = AsyncMock()
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {"text": "a plain reply", "user": "U999", "thread_ts": "222.000"}

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_not_awaited()
    say.assert_not_awaited()


async def test_handle_thread_reply_continues_conversation_in_a_tracked_thread(
    monkeypatch,
):
    history = [{"role": "user", "content": "earlier question"}]
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": history})
    response = AskResponse(answer="continuing the thread", tool_calls=[], warnings=[])
    mock_handle_conversational_query = AsyncMock(return_value=(response, []))
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {
        "text": "a follow-up with no mention",
        "user": "U999",
        "thread_ts": "111.000",
    }

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_awaited_once_with(
        "a follow-up with no mention", history
    )
    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "111.000"
    assert bolt_app._thread_histories["111.000"] == []


async def test_handle_thread_reply_ignores_the_bots_own_messages(monkeypatch):
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": []})
    mock_handle_conversational_query = AsyncMock()
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {"text": "the bot's own reply", "user": "UBOT", "thread_ts": "111.000"}

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_not_awaited()
    say.assert_not_awaited()


async def test_handle_thread_reply_ignores_edits_and_deletes(monkeypatch):
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": []})
    mock_handle_conversational_query = AsyncMock()
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {
        "text": "edited text",
        "user": "U999",
        "thread_ts": "111.000",
        "subtype": "message_changed",
    }

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_not_awaited()


async def test_handle_thread_reply_processes_thread_broadcast_replies(monkeypatch):
    history = []
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": history})
    response = AskResponse(answer="broadcast reply handled", tool_calls=[], warnings=[])
    mock_handle_conversational_query = AsyncMock(return_value=(response, []))
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {
        "text": "also sent to channel",
        "user": "U999",
        "thread_ts": "111.000",
        "subtype": "thread_broadcast",
    }

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_awaited_once()


async def test_handle_thread_reply_ignores_messages_that_mention_the_bot(monkeypatch):
    # app_mention already handles a message that mentions the bot - answering
    # it here too via the plain "message" event would double-reply.
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": []})
    mock_handle_conversational_query = AsyncMock()
    monkeypatch.setattr(
        bolt_app, "handle_conversational_query", mock_handle_conversational_query
    )

    say = AsyncMock()
    event = {
        "text": "<@UBOT> what about this?",
        "user": "U999",
        "thread_ts": "111.000",
    }

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    mock_handle_conversational_query.assert_not_awaited()
    say.assert_not_awaited()


async def test_handle_thread_reply_replies_with_apology_when_orchestrator_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(bolt_app, "_thread_histories", {"111.000": []})
    monkeypatch.setattr(
        bolt_app,
        "handle_conversational_query",
        AsyncMock(side_effect=OrchestratorUnavailable("boom")),
    )

    say = AsyncMock()
    event = {"text": "a follow-up", "user": "U999", "thread_ts": "111.000"}

    await bolt_app.handle_thread_reply(event, say, {"bot_user_id": "UBOT"})

    say.assert_awaited_once()
    _, kwargs = say.call_args
    assert kwargs["thread_ts"] == "111.000"
    assert "couldn't reach the assistant" in kwargs["text"]
