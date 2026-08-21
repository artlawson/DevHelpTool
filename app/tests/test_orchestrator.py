from datetime import UTC, datetime
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from app.agent import orchestrator
from app.core.models import CommentDraft, Issue, PullRequest, ToolResult


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, block_id: str, name: str, tool_input: dict | None = None):
        self.id = block_id
        self.name = name
        self.input = tool_input or {}


class FakeResponse:
    def __init__(self, content: list, stop_reason: str):
        self.content = content
        self.stop_reason = stop_reason


async def test_loop_dispatches_tool_and_produces_final_answer(monkeypatch):
    tool_use = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse(
        [FakeTextBlock("You have 1 high priority issue.")], "end_turn"
    )

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", mock_tool
    )

    result = await orchestrator.handle_query("what should I work on today?")

    assert result.answer == "You have 1 high priority issue."
    assert result.tool_calls == ["jira_get_my_high_priority_issues"]
    assert result.warnings == []
    mock_tool.assert_awaited_once()


async def test_tool_result_note_is_forwarded_to_the_model(monkeypatch):
    tool_use = FakeToolUseBlock(
        "call_1", "jira_get_incomplete_issues_from_last_sprint"
    )
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("No closed sprint yet.")], "end_turn")

    # `messages` is mutated in place across iterations (see CLAUDE.md
    # troubleshooting) - capture a shallow copy per call instead of relying
    # on await_args_list, which would only ever show the final state.
    captured_messages_per_call: list[list] = []

    async def fake_create(**kwargs):
        captured_messages_per_call.append(list(kwargs["messages"]))
        if len(captured_messages_per_call) == 1:
            return first_response
        return final_response

    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", fake_create)

    mock_tool = AsyncMock(
        return_value=ToolResult(
            ok=True,
            data=[],
            error=None,
            note='Did you mean "Sprint 1"?',
        )
    )
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY,
        "jira_get_incomplete_issues_from_last_sprint",
        mock_tool,
    )

    await orchestrator.handle_query("what's incomplete from last sprint?")

    second_call_messages = captured_messages_per_call[1]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert "Sprint 1" in tool_result_content


async def test_failing_tool_produces_warning_and_loop_continues(monkeypatch):
    tool_use_1 = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    tool_use_2 = FakeToolUseBlock("call_2", "github_get_my_open_prs")
    first_response = FakeResponse([tool_use_1, tool_use_2], "tool_use")
    final_response = FakeResponse([FakeTextBlock("Partial results.")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    failing_tool = AsyncMock(return_value=ToolResult(ok=False, data=None, error="503"))
    succeeding_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", failing_tool
    )
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "github_get_my_open_prs", succeeding_tool
    )

    result = await orchestrator.handle_query("status?")

    assert result.answer == "Partial results."
    assert result.tool_calls == ["github_get_my_open_prs"]
    assert len(result.warnings) == 1
    assert "jira_get_my_high_priority_issues" in result.warnings[0]
    assert "503" in result.warnings[0]


async def test_tool_raising_exception_produces_warning_and_loop_continues(monkeypatch):
    tool_use = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("Something went wrong.")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    raising_tool = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", raising_tool
    )

    result = await orchestrator.handle_query("status?")

    assert result.tool_calls == []
    assert len(result.warnings) == 1
    # sanitize_error() strips the raw exception message (spec §10); only the
    # exception type and tool name should be visible, never "boom" itself.
    assert "jira_get_my_high_priority_issues" in result.warnings[0]
    assert "RuntimeError" in result.warnings[0]
    assert "boom" not in result.warnings[0]


async def test_loop_terminates_at_max_iterations_with_forced_final_call(monkeypatch):
    tool_use = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    always_tool_use = FakeResponse([tool_use], "tool_use")
    forced_final = FakeResponse([FakeTextBlock("forced final answer")], "end_turn")

    call_kwargs = []

    async def fake_create(**kwargs):
        call_kwargs.append(kwargs)
        if kwargs["tool_choice"] == {"type": "none"}:
            return forced_final
        return always_tool_use

    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", fake_create)
    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", mock_tool
    )

    result = await orchestrator.handle_query("status?")

    assert result.answer == "forced final answer"
    assert len(call_kwargs) == orchestrator.MAX_ITERATIONS
    assert call_kwargs[-1]["tool_choice"] == {"type": "none"}
    assert call_kwargs[0]["tool_choice"] == {"type": "auto"}


async def test_handle_query_collects_referenced_issues_and_prs_from_tool_data(
    monkeypatch,
):
    tool_use_jira = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    tool_use_github = FakeToolUseBlock("call_2", "github_get_my_open_prs")
    first_response = FakeResponse([tool_use_jira, tool_use_github], "tool_use")
    final_response = FakeResponse([FakeTextBlock("Focus on AL-1.")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    issue = Issue(
        key="AL-1",
        summary="Fix auth bug",
        priority="High",
        status="Open",
        due_date=None,
        has_linked_pr=False,
        priority_score=3.0,
    )
    pr = PullRequest(
        repo="acme/widgets",
        number=514,
        title="Fix auth bug",
        url="https://github.com/acme/widgets/pull/514",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_review_requested=False,
        is_authored_by_me=True,
        age_score=0.0,
    )
    jira_tool = AsyncMock(return_value=ToolResult(ok=True, data=[issue], error=None))
    github_tool = AsyncMock(return_value=ToolResult(ok=True, data=[pr], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", jira_tool
    )
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "github_get_my_open_prs", github_tool
    )

    result = await orchestrator.handle_query("what should I work on today?")

    assert result.referenced_issues == [issue]
    assert result.referenced_prs == [pr]


async def test_anthropic_api_error_raises_orchestrator_unavailable(monkeypatch):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def fake_create(**kwargs):
        raise anthropic.APIConnectionError(request=request)

    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", fake_create)

    with pytest.raises(orchestrator.OrchestratorUnavailable):
        await orchestrator.handle_query("status?")


async def test_concurrent_tool_use_blocks_dispatched_via_gather(monkeypatch):
    import asyncio

    tool_use_1 = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    tool_use_2 = FakeToolUseBlock("call_2", "github_get_my_open_prs")
    first_response = FakeResponse([tool_use_1, tool_use_2], "tool_use")
    final_response = FakeResponse([FakeTextBlock("done")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    started_at: dict[str, float] = {}
    finished_at: dict[str, float] = {}

    async def slow_tool(name: str) -> ToolResult:
        loop = asyncio.get_event_loop()
        started_at[name] = loop.time()
        await asyncio.sleep(0.05)
        finished_at[name] = loop.time()
        return ToolResult(ok=True, data=[], error=None)

    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY,
        "jira_get_my_high_priority_issues",
        lambda: slow_tool("jira_get_my_high_priority_issues"),
    )
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY,
        "github_get_my_open_prs",
        lambda: slow_tool("github_get_my_open_prs"),
    )

    await orchestrator.handle_query("status?")

    # If dispatched sequentially, the second tool's start would be >= the
    # first tool's finish. Concurrent dispatch means both start before either
    # finishes.
    jira_name = "jira_get_my_high_priority_issues"
    github_name = "github_get_my_open_prs"
    assert started_at[github_name] < finished_at[jira_name]
    assert started_at[jira_name] < finished_at[github_name]


async def test_dispatch_passes_block_input_as_kwargs_to_parameterized_tool(
    monkeypatch,
):
    tool_use = FakeToolUseBlock(
        "call_1", "jira_get_persons_open_issues", {"person": "Sam"}
    )
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("Sam has 1 issue.")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_persons_open_issues", mock_tool
    )

    await orchestrator.handle_query("what is Sam working on?")

    mock_tool.assert_awaited_once_with(person="Sam")


async def test_existing_zero_arg_tools_still_called_with_no_args(monkeypatch):
    tool_use = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("done")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", mock_tool
    )

    await orchestrator.handle_query("status?")

    mock_tool.assert_awaited_once_with()


async def test_handle_query_threads_comment_draft_into_ask_response(monkeypatch):
    tool_use = FakeToolUseBlock(
        "call_1",
        "jira_draft_comment",
        {"issue_key": "AL-13", "note_text": "quick thought"},
    )
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("Drafted it.")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    draft = CommentDraft(issue_key="AL-13", note_text="quick thought")
    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=draft, error=None))
    monkeypatch.setitem(orchestrator.TOOL_REGISTRY, "jira_draft_comment", mock_tool)

    result = await orchestrator.handle_query("add a note to AL-13")

    assert result.pending_comment_draft == draft


async def test_handle_query_pending_comment_draft_none_when_not_drafted(monkeypatch):
    tool_use = FakeToolUseBlock("call_1", "jira_get_my_high_priority_issues")
    first_response = FakeResponse([tool_use], "tool_use")
    final_response = FakeResponse([FakeTextBlock("done")], "end_turn")

    mock_create = AsyncMock(side_effect=[first_response, final_response])
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    mock_tool = AsyncMock(return_value=ToolResult(ok=True, data=[], error=None))
    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_get_my_high_priority_issues", mock_tool
    )

    result = await orchestrator.handle_query("status?")

    assert result.pending_comment_draft is None


async def test_handle_query_last_drafted_comment_wins_when_drafted_twice(monkeypatch):
    tool_use_1 = FakeToolUseBlock(
        "call_1", "jira_draft_comment", {"issue_key": "AL-1", "note_text": "first"}
    )
    tool_use_2 = FakeToolUseBlock(
        "call_2", "jira_draft_comment", {"issue_key": "AL-2", "note_text": "second"}
    )
    first_response = FakeResponse([tool_use_1], "tool_use")
    second_response = FakeResponse([tool_use_2], "tool_use")
    final_response = FakeResponse([FakeTextBlock("done")], "end_turn")

    mock_create = AsyncMock(
        side_effect=[first_response, second_response, final_response]
    )
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    async def fake_draft_comment(issue_key: str, note_text: str) -> ToolResult:
        return ToolResult(
            ok=True,
            data=CommentDraft(issue_key=issue_key, note_text=note_text),
            error=None,
        )

    monkeypatch.setitem(
        orchestrator.TOOL_REGISTRY, "jira_draft_comment", fake_draft_comment
    )

    result = await orchestrator.handle_query("add notes to AL-1 and AL-2")

    assert result.pending_comment_draft == CommentDraft(
        issue_key="AL-2", note_text="second"
    )


async def test_handle_conversational_query_with_no_history_behaves_like_handle_query(
    monkeypatch,
):
    final_response = FakeResponse([FakeTextBlock("a fresh answer")], "end_turn")
    mock_create = AsyncMock(return_value=final_response)
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    response, history = await orchestrator.handle_conversational_query("hello")

    assert response.answer == "a fresh answer"
    assert history[0] == {"role": "user", "content": "hello"}


async def test_handle_conversational_query_prepends_prior_history(monkeypatch):
    prior_history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": [FakeTextBlock("earlier answer")]},
    ]
    final_response = FakeResponse([FakeTextBlock("follow-up answer")], "end_turn")

    captured_messages_per_call: list[list] = []

    async def fake_create(**kwargs):
        captured_messages_per_call.append(list(kwargs["messages"]))
        return final_response

    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", fake_create)

    response, _ = await orchestrator.handle_conversational_query(
        "a follow-up", prior_history
    )

    assert response.answer == "follow-up answer"
    first_call_messages = captured_messages_per_call[0]
    assert first_call_messages[0] == prior_history[0]
    assert first_call_messages[1] == prior_history[1]
    assert first_call_messages[2] == {"role": "user", "content": "a follow-up"}


async def test_handle_conversational_query_returns_capped_history(monkeypatch):
    # Build a history already at the cap: MAX_HISTORY_TURNS complete,
    # tool-free turns (user string -> assistant text each).
    prior_history: list[dict] = []
    for i in range(orchestrator.MAX_HISTORY_TURNS):
        prior_history.append({"role": "user", "content": f"question {i}"})
        prior_history.append(
            {"role": "assistant", "content": [FakeTextBlock(f"answer {i}")]}
        )

    final_response = FakeResponse([FakeTextBlock("newest answer")], "end_turn")
    mock_create = AsyncMock(return_value=final_response)
    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", mock_create)

    _, history = await orchestrator.handle_conversational_query(
        "newest question", prior_history
    )

    boundaries = [
        m
        for m in history
        if m["role"] == "user" and isinstance(m["content"], str)
    ]
    assert len(boundaries) == orchestrator.MAX_HISTORY_TURNS
    # The oldest turn ("question 0") must have been dropped to make room.
    assert not any(m.get("content") == "question 0" for m in history)
    assert any(m.get("content") == "newest question" for m in history)


def test_cap_history_returns_all_messages_when_under_the_cap():
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [FakeTextBlock("a1")]},
    ]

    assert orchestrator._cap_history(messages, max_turns=5) == messages


def test_cap_history_keeps_last_n_turns_and_drops_older_ones():
    messages = []
    for i in range(4):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": [FakeTextBlock(f"a{i}")]})

    capped = orchestrator._cap_history(messages, max_turns=2)

    assert capped[0] == {"role": "user", "content": "q2"}
    assert capped[-1] is messages[-1]
    assert not any(m.get("content") == "q0" for m in capped)
    assert not any(m.get("content") == "q1" for m in capped)


def test_cap_history_never_slices_inside_a_tool_use_tool_result_pair():
    # Turn 0: a single-tool-call round trip (user str -> assistant tool_use ->
    # user list(tool_result) -> assistant text).
    turn_0 = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [FakeToolUseBlock("t1", "some_tool")]},
        {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        {"role": "assistant", "content": [FakeTextBlock("a0")]},
    ]
    # Turn 1: plain, no tool calls.
    turn_1 = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [FakeTextBlock("a1")]},
    ]
    messages = turn_0 + turn_1

    capped = orchestrator._cap_history(messages, max_turns=1)

    # Only turn 1 should remain, starting exactly at its user/string boundary
    # - never mid-turn-0's tool_use/tool_result pair.
    assert capped == turn_1


def test_cap_history_drops_a_capped_away_turn_with_multiple_tool_rounds():
    # Turn 0 takes two sequential tool-calling iterations before its final
    # text answer - the exact shape a multi-step "what should I work on"
    # question produces. It must be droppable as a single whole turn, same
    # as a tool-free one, when the cap requires dropping it entirely.
    turn_0 = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [FakeToolUseBlock("t1", "tool_a")]},
        {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        {"role": "assistant", "content": [FakeToolUseBlock("t2", "tool_b")]},
        {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        {"role": "assistant", "content": [FakeTextBlock("a0")]},
    ]
    turn_1 = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [FakeTextBlock("a1")]},
    ]
    messages = turn_0 + turn_1

    capped = orchestrator._cap_history(messages, max_turns=1)

    assert capped == turn_1
    assert capped[0]["role"] == "user"
    assert isinstance(capped[0]["content"], str)
