import httpx
import pytest
import respx

from app.clients.github_client import GitHubClient
from app.config import Settings
from app.core.cache import TTLCache


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="x",
        jira_base_url="x",
        jira_email="x",
        jira_api_token="x",
        jira_project_key="x",
        github_token="gh-token",
        github_username="octocat",
    )


@respx.mock
async def test_search_prs_sends_get_with_query_and_bearer_auth(settings: Settings):
    route = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [{"number": 42}]})
    )

    client = GitHubClient(settings)
    items = await client.search_prs("is:pr is:open author:octocat")

    assert items == [{"number": 42}]
    request = route.calls[0].request
    assert request.url.params["q"] == "is:pr is:open author:octocat"
    assert request.headers["Authorization"] == "Bearer gh-token"


@respx.mock
async def test_search_prs_raises_on_server_error(settings: Settings):
    respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    client = GitHubClient(settings)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search_prs("is:pr is:open author:octocat")


@respx.mock
async def test_search_prs_raises_on_forbidden(settings: Settings):
    respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )

    client = GitHubClient(settings)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search_prs("is:pr is:open author:octocat")


@respx.mock
async def test_search_prs_uses_cache_for_repeated_query_within_ttl(settings: Settings):
    route = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [{"number": 42}]})
    )

    client = GitHubClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.search_prs("is:pr is:open author:octocat")
    await client.search_prs("is:pr is:open author:octocat")

    assert route.call_count == 1


@respx.mock
async def test_search_prs_bypasses_cache_for_different_query(settings: Settings):
    route = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    client = GitHubClient(settings, cache=TTLCache(ttl_seconds=60))
    await client.search_prs("is:pr is:open author:octocat")
    await client.search_prs("is:pr is:open review-requested:octocat")

    assert route.call_count == 2


@respx.mock
async def test_search_prs_refetches_after_ttl_expires(settings: Settings):
    route = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    fake_time = [1000.0]
    cache = TTLCache(ttl_seconds=10, clock=lambda: fake_time[0])
    client = GitHubClient(settings, cache=cache)
    await client.search_prs("is:pr is:open author:octocat")
    fake_time[0] += 11
    await client.search_prs("is:pr is:open author:octocat")

    assert route.call_count == 2
