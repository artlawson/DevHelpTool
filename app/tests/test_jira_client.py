import httpx
import pytest
import respx

from app.clients.jira_client import JiraClient
from app.config import Settings
from app.core.cache import TTLCache


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="x",
        jira_base_url="https://example.atlassian.net",
        jira_email="me@example.com",
        jira_api_token="jira-token",
        github_token="x",
        github_username="x",
    )


@respx.mock
async def test_search_sends_get_with_jql_param_and_basic_auth(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "PROJ-1"}]})
    )

    client = JiraClient(settings)
    issues = await client.search("assignee = currentUser()")

    assert issues == [{"key": "PROJ-1"}]
    request = route.calls[0].request
    assert request.url.params["jql"] == "assignee = currentUser()"
    assert request.url.params["fields"] == "summary,priority,status,duedate"
    assert request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_search_raises_on_server_error(settings: Settings):
    respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    client = JiraClient(settings)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search("assignee = currentUser()")


@respx.mock
async def test_search_raises_on_auth_error(settings: Settings):
    respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    client = JiraClient(settings)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search("assignee = currentUser()")


@respx.mock
async def test_search_uses_cache_for_repeated_jql_within_ttl(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "PROJ-1"}]})
    )

    client = JiraClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.search("assignee = currentUser()")
    await client.search("assignee = currentUser()")

    assert route.call_count == 1


@respx.mock
async def test_search_bypasses_cache_for_different_jql(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )

    client = JiraClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.search("assignee = currentUser()")
    await client.search("assignee = currentUser() AND priority = High")

    assert route.call_count == 2


@respx.mock
async def test_search_refetches_after_ttl_expires(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )

    fake_time = [1000.0]
    cache = TTLCache(ttl_seconds=10, clock=lambda: fake_time[0])
    client = JiraClient(settings, cache=cache)
    await client.search("assignee = currentUser()")
    fake_time[0] += 11
    await client.search("assignee = currentUser()")

    assert route.call_count == 2
