from unittest.mock import AsyncMock

from app.tools import jira_tools

RAW_ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "Fix null pointer",
        "priority": {"name": "High"},
        "status": {"name": "In Progress"},
        "duedate": None,
    },
}

RAW_ISSUE_2 = {
    "key": "PROJ-2",
    "fields": {
        "summary": "Add logging",
        "priority": {"name": "Highest"},
        "status": {"name": "Open"},
        "duedate": None,
    },
}


async def test_get_my_high_priority_issues_maps_and_ranks(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.ok is True
    assert result.data is not None
    assert [i.key for i in result.data] == ["PROJ-2", "PROJ-1"]  # Highest ranked first
    (jql,), _ = mock_search.call_args
    assert "priority in (High, Highest)" in jql
    assert "assignee = currentUser()" in jql


async def test_get_my_high_priority_issues_returns_error_result_on_failure(monkeypatch):
    mock_search = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.ok is False
    assert result.data is None
    assert result.error is not None


async def test_get_issues_without_prs_excludes_issue_in_pr_title(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    mock_github_search = AsyncMock(
        return_value=[{"title": "Fix PROJ-1: null pointer", "body": ""}]
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.ok is True
    assert result.data is not None
    keys = [i.key for i in result.data]
    assert "PROJ-1" not in keys
    assert "PROJ-2" in keys


async def test_get_issues_without_prs_includes_issue_referenced_in_pr_body(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    mock_github_search = AsyncMock(
        return_value=[{"title": "Unrelated change", "body": "closes PROJ-2"}]
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.data is not None
    keys = [i.key for i in result.data]
    assert "PROJ-1" in keys
    assert "PROJ-2" not in keys


async def test_get_issues_without_prs_includes_issue_with_no_matching_pr(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE])
    mock_github_search = AsyncMock(return_value=[{"title": "Unrelated PR", "body": ""}])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.data is not None
    assert [i.key for i in result.data] == ["PROJ-1"]


async def test_get_issues_without_prs_returns_error_when_jira_fails(monkeypatch):
    mock_jira_search = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.ok is False
    assert result.error is not None


async def test_get_issues_without_prs_returns_error_when_github_fails(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE])
    mock_github_search = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.ok is False
    assert result.error is not None


async def test_get_my_issues_with_linked_prs_attaches_matching_pr(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    raw_pr = {
        "title": "Fix PROJ-1: null pointer",
        "body": "",
        "repository_url": "https://api.github.com/repos/org/repo",
        "number": 42,
        "html_url": "https://github.com/org/repo/pull/42",
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    mock_github_search = AsyncMock(return_value=[raw_pr])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_my_issues_with_linked_prs()

    assert result.ok is True
    assert result.data is not None
    by_key = {i.key: i for i in result.data}
    assert by_key["PROJ-1"].has_linked_pr is True
    assert by_key["PROJ-1"].linked_pr is not None
    assert by_key["PROJ-1"].linked_pr.number == 42
    assert by_key["PROJ-2"].has_linked_pr is False
    assert by_key["PROJ-2"].linked_pr is None


async def test_get_my_issues_with_linked_prs_returns_error_when_jira_fails(monkeypatch):
    mock_jira_search = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)

    result = await jira_tools.get_my_issues_with_linked_prs()

    assert result.ok is False
    assert result.error is not None


async def test_get_my_issues_with_linked_prs_returns_error_when_github_fails(
    monkeypatch,
):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE])
    mock_github_search = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_my_issues_with_linked_prs()

    assert result.ok is False
    assert result.error is not None


async def test_get_incomplete_issues_from_last_sprint_picks_most_recent_sprint(
    monkeypatch,
):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_sprints = AsyncMock(
        return_value=[
            {"id": 10, "completeDate": "2026-07-20T00:00:00.000Z"},
            {"id": 11, "completeDate": "2026-08-03T00:00:00.000Z"},
        ]
    )
    mock_search = AsyncMock(return_value=[RAW_ISSUE])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_closed_sprints", mock_get_sprints)
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is True
    assert result.data is not None
    assert [i.key for i in result.data] == ["PROJ-1"]
    (jql,), _ = mock_search.call_args
    assert "sprint = 11" in jql
    assert "assignee = currentUser()" in jql


async def test_get_incomplete_issues_from_last_sprint_returns_empty_when_no_sprints(
    monkeypatch,
):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_closed = AsyncMock(return_value=[])
    mock_get_active = AsyncMock(return_value=[])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_closed_sprints", mock_get_closed)
    monkeypatch.setattr(jira_tools.jira_client, "get_active_sprints", mock_get_active)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is True
    assert result.data == []
    assert result.note is None


async def test_get_incomplete_issues_from_last_sprint_suggests_overdue_active_sprint(
    monkeypatch,
):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_closed = AsyncMock(return_value=[])
    mock_get_active = AsyncMock(
        return_value=[
            {"id": 20, "name": "Sprint 1", "endDate": "2020-01-01T00:00:00.000Z"}
        ]
    )
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_closed_sprints", mock_get_closed)
    monkeypatch.setattr(jira_tools.jira_client, "get_active_sprints", mock_get_active)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is True
    assert result.data == []
    assert result.note is not None
    assert "Sprint 1" in result.note


async def test_get_incomplete_issues_from_last_sprint_no_note_when_sprint_not_overdue(
    monkeypatch,
):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_closed = AsyncMock(return_value=[])
    mock_get_active = AsyncMock(
        return_value=[
            {"id": 21, "name": "Sprint 2", "endDate": "2099-01-01T00:00:00.000Z"}
        ]
    )
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_closed_sprints", mock_get_closed)
    monkeypatch.setattr(jira_tools.jira_client, "get_active_sprints", mock_get_active)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is True
    assert result.data == []
    assert result.note is None


async def test_get_incomplete_issues_from_last_sprint_returns_error_on_failure(
    monkeypatch,
):
    mock_get_boards = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is False
    assert result.error is not None


async def test_neither_tool_module_imports_anthropic():
    import ast
    import inspect

    from app.tools import github_tools

    for module in (jira_tools, github_tools):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "anthropic" not in imported_names
