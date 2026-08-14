from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.tools import github_tools

RAW_PR = {
    "number": 42,
    "title": "Fix AL-1",
    "html_url": "https://github.com/org/repo/pull/42",
    "repository_url": "https://api.github.com/repos/org/repo",
    "created_at": "2026-01-01T12:00:00Z",
    "body": "",
}


async def test_get_my_open_prs_queries_author_and_sets_flag(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_my_open_prs()

    assert result.ok is True
    assert result.data is not None
    assert result.data[0].is_authored_by_me is True
    assert result.data[0].is_review_requested is False
    mock_search.assert_awaited_once()
    (query,), _ = mock_search.call_args
    assert "author:" in query
    assert "is:open" in query


async def test_get_prs_awaiting_review_queries_review_requested_flag(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_prs_awaiting_my_review()

    assert result.ok is True
    assert result.data is not None
    assert result.data[0].is_review_requested is True
    assert result.data[0].is_authored_by_me is False
    (query,), _ = mock_search.call_args
    assert "review-requested:" in query


async def test_both_tools_share_the_same_underlying_client_method(monkeypatch):
    """Validates the §6.9 shared-fetch design: no duplicated HTTP-calling code paths."""
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    await github_tools.get_my_open_prs()
    await github_tools.get_prs_awaiting_my_review()

    assert mock_search.await_count == 2
    for call in mock_search.await_args_list:
        assert call is not None


async def test_get_my_open_prs_returns_error_result_on_failure(monkeypatch):
    mock_search = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_my_open_prs()

    assert result.ok is False
    assert result.data is None
    assert result.error is not None


async def test_age_score_is_populated_on_returned_pull_requests(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_my_open_prs()

    assert result.data is not None
    assert result.data[0].age_score >= 0


async def test_get_prs_i_could_review_skips_search_when_repo_unset(monkeypatch):
    monkeypatch.setattr(settings, "github_repo", None)
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_prs_i_could_review()

    assert result.ok is True
    assert result.data == []
    mock_search.assert_not_awaited()


async def test_get_prs_i_could_review_scopes_query_and_excludes_self(monkeypatch):
    monkeypatch.setattr(settings, "github_repo", "artlawson/DevHelpTool")
    mock_search = AsyncMock(return_value=[RAW_PR])
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_prs_i_could_review()

    assert result.ok is True
    assert result.data is not None
    (query,), _ = mock_search.call_args
    assert "repo:artlawson/DevHelpTool" in query
    assert f"-author:{settings.github_username}" in query
    assert f"-review-requested:{settings.github_username}" in query


async def test_get_prs_i_could_review_returns_error_result_on_failure(monkeypatch):
    monkeypatch.setattr(settings, "github_repo", "artlawson/DevHelpTool")
    mock_search = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(github_tools.github_client, "search_prs", mock_search)

    result = await github_tools.get_prs_i_could_review()

    assert result.ok is False
    assert result.data is None


@pytest.mark.parametrize(
    ("repository_url", "expected"),
    [
        ("https://api.github.com/repos/org/repo", "org/repo"),
        ("https://api.github.com/repos/org/repo/", "org/repo"),
    ],
)
def test_repo_full_name_extraction(repository_url: str, expected: str):
    assert github_tools._repo_full_name(repository_url) == expected
