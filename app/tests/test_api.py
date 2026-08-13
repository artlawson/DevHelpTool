from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import slack_bolt.adapter.socket_mode.async_handler as async_handler_module

import app.main as main_module
from app.agent.orchestrator import OrchestratorUnavailable
from app.core.models import AskResponse
from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_ask_returns_answer_on_success(client, monkeypatch):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(
            answer="you're all caught up",
            tool_calls=["jira_get_my_high_priority_issues"],
            warnings=[],
        )
    )
    monkeypatch.setattr("app.main.handle_query", mock_handle_query)

    response = await client.post("/ask", json={"query": "what should I work on today?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "you're all caught up"
    assert body["tool_calls"] == ["jira_get_my_high_priority_issues"]
    assert body["warnings"] == []
    mock_handle_query.assert_awaited_once_with("what should I work on today?")


async def test_ask_returns_503_on_orchestrator_unavailable(client, monkeypatch):
    mock_handle_query = AsyncMock(
        side_effect=OrchestratorUnavailable("connection reset")
    )
    monkeypatch.setattr("app.main.handle_query", mock_handle_query)

    response = await client.post("/ask", json={"query": "status?"})

    assert response.status_code == 503
    assert response.json() == {"detail": "assistant temporarily unavailable"}
    # The real exception message must never leak to the client.
    assert "connection reset" not in response.text


async def test_health_returns_200_without_any_credentials(client, monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_USERNAME",
    ):
        monkeypatch.delenv(var, raising=False)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_lifespan_never_touches_socket_mode_when_slack_not_configured(
    monkeypatch,
):
    monkeypatch.setattr(main_module.settings, "slack_bot_token", None)
    monkeypatch.setattr(main_module.settings, "slack_app_token", None)

    mock_handler_cls = MagicMock()
    monkeypatch.setattr(
        async_handler_module, "AsyncSocketModeHandler", mock_handler_cls
    )

    async with main_module.lifespan(app):
        pass

    mock_handler_cls.assert_not_called()


async def test_lifespan_connects_and_disconnects_socket_mode_when_configured(
    monkeypatch,
):
    monkeypatch.setattr(main_module.settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(main_module.settings, "slack_app_token", "xapp-test")

    mock_handler_instance = AsyncMock()
    mock_handler_cls = MagicMock(return_value=mock_handler_instance)
    monkeypatch.setattr(
        async_handler_module, "AsyncSocketModeHandler", mock_handler_cls
    )

    async with main_module.lifespan(app):
        mock_handler_instance.connect_async.assert_awaited_once()

    mock_handler_instance.disconnect_async.assert_awaited_once()
