from unittest.mock import AsyncMock

from app.tools import jira_tools

RAW_ISSUE = {
    "key": "AL-1",
    "fields": {
        "summary": "Fix null pointer",
        "priority": {"name": "High"},
        "status": {"name": "In Progress"},
        "duedate": None,
        "description": None,
    },
}

RAW_ISSUE_2 = {
    "key": "AL-2",
    "fields": {
        "summary": "Add logging",
        "priority": {"name": "Highest"},
        "status": {"name": "Open"},
        "duedate": None,
        "description": None,
    },
}

# Real Jira instances commonly use custom priority schemes (P0-P4,
# Blocker/Critical/...) that don't match core/ranking.py's fixed
# PRIORITY_WEIGHTS keys. Used to regression-test that a KeyError from
# _map_issue()/score_issue() during mapping degrades to ToolResult(ok=False,
# ...) like any other failure, rather than propagating uncaught.
RAW_ISSUE_UNRECOGNIZED_PRIORITY = {
    "key": "AL-9",
    "fields": {
        "summary": "Uses a custom priority scheme",
        "priority": {"name": "P1"},
        "status": {"name": "Open"},
        "duedate": None,
        "description": None,
    },
}


def test_is_empty_description_treats_none_as_empty():
    assert jira_tools._is_empty_description(None) is True


def test_is_empty_description_treats_empty_dict_as_empty():
    assert jira_tools._is_empty_description({}) is True


def test_is_empty_description_treats_empty_adf_doc_as_empty():
    # Real shape confirmed against a live Jira Cloud instance (2026-08-13): when
    # a description is actively cleared (not just never set), Jira collapses it
    # to an empty top-level `content` array, not a lingering empty paragraph.
    empty_doc = {"type": "doc", "version": 1, "content": []}
    assert jira_tools._is_empty_description(empty_doc) is True


def test_is_empty_description_treats_empty_paragraph_as_empty_too():
    # Defensive case: live Jira doesn't actually return this shape (see the
    # test above), but the function should still treat it as empty if it did.
    doc_with_empty_paragraph = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": []}],
    }
    assert jira_tools._is_empty_description(doc_with_empty_paragraph) is True


def test_is_empty_description_treats_adf_doc_with_text_as_not_empty():
    doc_with_text = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Steps to reproduce..."}],
            }
        ],
    }
    assert jira_tools._is_empty_description(doc_with_text) is False


async def test_get_my_high_priority_issues_maps_description_when_present(monkeypatch):
    raw_issue_with_description = {
        "key": "AL-3",
        "fields": {
            "summary": "Needs detail",
            "priority": {"name": "High"},
            "status": {"name": "Open"},
            "duedate": None,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Some real detail"}],
                    }
                ],
            },
        },
    }
    mock_search = AsyncMock(return_value=[raw_issue_with_description])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.data is not None
    assert result.data[0].description == "Some real detail"


async def test_get_my_high_priority_issues_leaves_description_none_when_empty(
    monkeypatch,
):
    mock_search = AsyncMock(return_value=[RAW_ISSUE])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.data is not None
    assert result.data[0].description is None


async def test_get_my_high_priority_issues_maps_and_ranks(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.ok is True
    assert result.data is not None
    assert [i.key for i in result.data] == ["AL-2", "AL-1"]  # Highest ranked first
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


async def test_get_my_high_priority_issues_errors_on_unrecognized_priority(
    monkeypatch,
):
    # Regression test: mapping happens inside the try/except now, so a Jira
    # instance using a non-standard priority scheme degrades to ok=False
    # instead of raising an uncaught KeyError out of the tool function.
    mock_search = AsyncMock(return_value=[RAW_ISSUE_UNRECOGNIZED_PRIORITY])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_my_high_priority_issues()

    assert result.ok is False
    assert result.data is None
    assert result.error is not None


async def test_get_issues_without_prs_excludes_issue_in_pr_title(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    mock_github_search = AsyncMock(
        return_value=[{"title": "Fix AL-1: null pointer", "body": ""}]
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.ok is True
    assert result.data is not None
    keys = [i.key for i in result.data]
    assert "AL-1" not in keys
    assert "AL-2" in keys


async def test_get_issues_without_prs_includes_issue_referenced_in_pr_body(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE, RAW_ISSUE_2])
    mock_github_search = AsyncMock(
        return_value=[{"title": "Unrelated change", "body": "closes AL-2"}]
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.data is not None
    keys = [i.key for i in result.data]
    assert "AL-1" in keys
    assert "AL-2" not in keys


async def test_get_issues_without_prs_includes_issue_with_no_matching_pr(monkeypatch):
    mock_jira_search = AsyncMock(return_value=[RAW_ISSUE])
    mock_github_search = AsyncMock(return_value=[{"title": "Unrelated PR", "body": ""}])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_jira_search)
    monkeypatch.setattr(jira_tools.github_client, "search_prs", mock_github_search)

    result = await jira_tools.get_issues_without_prs()

    assert result.data is not None
    assert [i.key for i in result.data] == ["AL-1"]


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
        "title": "Fix AL-1: null pointer",
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
    assert by_key["AL-1"].has_linked_pr is True
    assert by_key["AL-1"].linked_pr is not None
    assert by_key["AL-1"].linked_pr.number == 42
    assert by_key["AL-2"].has_linked_pr is False
    assert by_key["AL-2"].linked_pr is None


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
    assert [i.key for i in result.data] == ["AL-1"]
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


async def test_get_incomplete_issues_from_last_sprint_errors_on_bad_priority(
    monkeypatch,
):
    # Regression test for the same class of bug as
    # test_get_my_high_priority_issues_errors_on_unrecognized_priority, but
    # exercising the more complex multi-await try block (boards -> sprints ->
    # search) to confirm moving the mapping step inside it didn't change scope.
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_closed = AsyncMock(
        return_value=[{"id": 10, "completeDate": "2026-07-20T00:00:00.000Z"}]
    )
    mock_search = AsyncMock(return_value=[RAW_ISSUE_UNRECOGNIZED_PRIORITY])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_closed_sprints", mock_get_closed)
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_incomplete_issues_from_last_sprint()

    assert result.ok is False
    assert result.error is not None


async def test_get_current_sprint_issues_single_board_one_active_sprint(monkeypatch):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_active = AsyncMock(return_value=[{"id": 20, "name": "Sprint 1"}])
    mock_search = AsyncMock(return_value=[RAW_ISSUE])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_active_sprints", mock_get_active)
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_current_sprint_issues()

    assert result.ok is True
    assert result.data is not None
    assert [i.key for i in result.data] == ["AL-1"]
    (jql,), _ = mock_search.call_args
    assert "sprint in (20)" in jql
    assert "assignee = currentUser()" in jql


async def test_get_current_sprint_issues_no_active_sprint_returns_empty_no_note(
    monkeypatch,
):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}])
    mock_get_active = AsyncMock(return_value=[])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(jira_tools.jira_client, "get_active_sprints", mock_get_active)

    result = await jira_tools.get_current_sprint_issues()

    assert result.ok is True
    assert result.data == []
    assert result.note is None


async def test_get_current_sprint_issues_unions_across_multiple_boards(monkeypatch):
    mock_get_boards = AsyncMock(return_value=[{"id": 1}, {"id": 2}])

    async def fake_get_active_sprints(board_id: int) -> list[dict]:
        if board_id == 1:
            return [{"id": 20, "name": "Sprint A"}]
        return [{"id": 30, "name": "Sprint B"}]

    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)
    monkeypatch.setattr(
        jira_tools.jira_client, "get_active_sprints", fake_get_active_sprints
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    await jira_tools.get_current_sprint_issues()

    (jql,), _ = mock_search.call_args
    assert "sprint in (20,30)" in jql


async def test_get_current_sprint_issues_returns_error_on_failure(monkeypatch):
    mock_get_boards = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "get_boards", mock_get_boards)

    result = await jira_tools.get_current_sprint_issues()

    assert result.ok is False
    assert result.error is not None


async def test_get_lower_priority_issues_due_soon_maps_and_ranks(monkeypatch):
    mock_search = AsyncMock(return_value=[RAW_ISSUE])
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_lower_priority_issues_due_soon()

    assert result.ok is True
    assert result.data is not None
    (jql,), _ = mock_search.call_args
    assert "priority in (Medium, Low, Lowest)" in jql
    assert 'startOfDay() AND duedate <= startOfDay("+7d")' in jql


async def test_get_lower_priority_issues_due_soon_returns_error_on_failure(monkeypatch):
    mock_search = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_lower_priority_issues_due_soon()

    assert result.ok is False
    assert result.error is not None


async def test_get_backlog_issues_needing_details_filters_by_empty_description(
    monkeypatch,
):
    raw_issue_no_description = {
        "key": "AL-1",
        "fields": {**RAW_ISSUE["fields"], "description": None},
    }
    raw_issue_with_description = {
        "key": "AL-9",
        "fields": {
            "summary": "Has detail",
            "priority": {"name": "Low"},
            "status": {"name": "Open"},
            "duedate": None,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "detail"}],
                    }
                ],
            },
        },
    }
    mock_search = AsyncMock(
        return_value=[raw_issue_no_description, raw_issue_with_description]
    )
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_backlog_issues_needing_details()

    assert result.ok is True
    assert result.data is not None
    assert [i.key for i in result.data] == ["AL-1"]
    (jql,), _ = mock_search.call_args
    assert "reporter = currentUser() OR assignee = currentUser()" in jql
    assert "sprint is EMPTY" in jql


async def test_get_backlog_issues_needing_details_returns_error_on_failure(
    monkeypatch,
):
    mock_search = AsyncMock(side_effect=RuntimeError("500 Internal Server Error"))
    monkeypatch.setattr(jira_tools.jira_client, "search", mock_search)

    result = await jira_tools.get_backlog_issues_needing_details()

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
