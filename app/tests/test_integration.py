"""End-to-end integration tests exercising the real orchestrator, tool
registry, tools, and clients together through the FastAPI HTTP layer.
Only the Anthropic API call and the outbound Jira/GitHub HTTP calls are
mocked (via respx) - everything in between is the real production wiring.
This is what Task 13 / spec §6.3 partial-degradation behavior actually
depends on end-to-end, which the per-layer unit tests can't catch on their
own if two correctly-tested layers are wired together incorrectly.
"""

import httpx
import respx

from app.agent import orchestrator
from app.config import settings
from app.core.cache import TTLCache
from app.tests.test_orchestrator import FakeResponse, FakeTextBlock, FakeToolUseBlock
from app.tools import github_tools, jira_tools


async def test_partial_failure_flows_end_to_end_through_the_real_api(monkeypatch):
    # jira_client/github_client are process-wide singletons with real TTL
    # caches; reset them so this test never depends on what ran before it.
    monkeypatch.setattr(jira_tools.jira_client, "_cache", TTLCache())
    monkeypatch.setattr(github_tools.github_client, "_cache", TTLCache())

    tool_use_jira = FakeToolUseBlock("call_1", "jira.get_my_high_priority_issues")
    tool_use_github = FakeToolUseBlock("call_2", "github.get_prs_awaiting_my_review")
    first_response = FakeResponse([tool_use_jira, tool_use_github], "tool_use")
    final_response = FakeResponse(
        [
            FakeTextBlock(
                "GitHub data is currently unavailable, so this only reflects "
                "Jira: PROJ-1 is your top priority."
            )
        ],
        "end_turn",
    )
    # `messages` is a single list handle_query mutates in place across
    # iterations, so a plain AsyncMock's await_args_list would only ever show
    # its *final* state for every recorded call. Capture a shallow copy of
    # the argument at each call instead, so per-call inspection is accurate.
    captured_messages_per_call: list[list] = []

    async def fake_create(**kwargs):
        captured_messages_per_call.append(list(kwargs["messages"]))
        if len(captured_messages_per_call) == 1:
            return first_response
        return final_response

    monkeypatch.setattr(orchestrator.anthropic_client.messages, "create", fake_create)

    with respx.mock:
        respx.get(f"{settings.jira_base_url}/rest/api/3/search/jql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "key": "PROJ-1",
                            "fields": {
                                "summary": "Fix null pointer",
                                "priority": {"name": "Highest"},
                                "status": {"name": "Open"},
                                "duedate": None,
                            },
                        }
                    ]
                },
            )
        )
        respx.get("https://api.github.com/search/issues").mock(
            return_value=httpx.Response(503, json={"message": "internal error detail"})
        )

        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/ask", json={"query": "what should I work on today?"}
            )

    assert response.status_code == 200
    body = response.json()

    assert body["tool_calls"] == ["jira.get_my_high_priority_issues"]
    assert len(body["warnings"]) == 1
    assert "github.get_prs_awaiting_my_review" in body["warnings"][0]
    # Sanitized per spec §10 - the raw GitHub response body must not leak.
    assert "internal error detail" not in body["warnings"][0]

    # Verify the ranked Jira data actually reached the model's second turn -
    # this is what proves the full chain (client -> tool -> dispatch ->
    # orchestrator) produced correct data, not just that *a* 200 came back.
    second_call_messages = captured_messages_per_call[1]
    tool_result_blocks = second_call_messages[-1]["content"]
    jira_result = next(
        block for block in tool_result_blocks if block["tool_use_id"] == "call_1"
    )
    assert "PROJ-1" in jira_result["content"]
    github_result = next(
        block for block in tool_result_blocks if block["tool_use_id"] == "call_2"
    )
    assert github_result.get("is_error") is True
