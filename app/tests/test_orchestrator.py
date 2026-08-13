from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from app.agent import orchestrator
from app.core.models import ToolResult


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
