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
        jira_project_key="TEST",
        github_token="x",
        github_username="x",
    )


@respx.mock
async def test_search_sends_get_with_jql_param_and_basic_auth(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "AL-1"}]})
    )

    client = JiraClient(settings)
    issues = await client.search("assignee = currentUser()")

    assert issues == [{"key": "AL-1"}]
    request = route.calls[0].request
    assert request.url.params["jql"] == "assignee = currentUser()"
    assert (
        request.url.params["fields"] == "summary,priority,status,duedate,description"
    )
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
        return_value=httpx.Response(200, json={"issues": [{"key": "AL-1"}]})
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


@respx.mock
async def test_get_boards_sends_get_scoped_to_project(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/agile/1.0/board").mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": 1, "name": "Board 1"}]}
        )
    )

    client = JiraClient(settings)
    boards = await client.get_boards()

    assert boards == [{"id": 1, "name": "Board 1"}]
    request = route.calls[0].request
    assert request.url.params["projectKeyOrId"] == "TEST"


@respx.mock
async def test_get_boards_uses_cache(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/agile/1.0/board").mock(
        return_value=httpx.Response(200, json={"values": []})
    )

    client = JiraClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.get_boards()
    await client.get_boards()

    assert route.call_count == 1


@respx.mock
async def test_get_closed_sprints_sends_get_with_state_param(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/agile/1.0/board/1/sprint").mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": 5, "name": "Sprint 1"}]}
        )
    )

    client = JiraClient(settings)
    sprints = await client.get_closed_sprints(1)

    assert sprints == [{"id": 5, "name": "Sprint 1"}]
    request = route.calls[0].request
    assert request.url.params["state"] == "closed"


@respx.mock
async def test_get_closed_sprints_uses_cache(settings: Settings):
    route = respx.get(
        "https://example.atlassian.net/rest/agile/1.0/board/1/sprint"
    ).mock(return_value=httpx.Response(200, json={"values": []}))

    client = JiraClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.get_closed_sprints(1)
    await client.get_closed_sprints(1)

    assert route.call_count == 1


@respx.mock
async def test_get_active_sprints_sends_get_with_state_param(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/agile/1.0/board/1/sprint").mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": 9, "name": "Sprint 2"}]}
        )
    )

    client = JiraClient(settings)
    sprints = await client.get_active_sprints(1)

    assert sprints == [{"id": 9, "name": "Sprint 2"}]
    request = route.calls[0].request
    assert request.url.params["state"] == "active"


@respx.mock
async def test_closed_and_active_sprints_do_not_share_a_cache_key(settings: Settings):
    route = respx.get("https://example.atlassian.net/rest/agile/1.0/board/1/sprint").mock(
        return_value=httpx.Response(200, json={"values": []})
    )

    client = JiraClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.get_closed_sprints(1)
    await client.get_active_sprints(1)

    assert route.call_count == 2
