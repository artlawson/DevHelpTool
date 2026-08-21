from datetime import UTC, date, datetime

from app.core.models import AskResponse, CommentDraft, Issue, PullRequest, ToolResult


def test_issue_model_constructs():
    issue = Issue(
        key="AL-1",
        summary="Fix bug",
        priority="High",
        status="In Progress",
        due_date=date(2026, 1, 1),
        has_linked_pr=False,
        priority_score=3.0,
    )
    assert issue.key == "AL-1"
    assert issue.due_date == date(2026, 1, 1)


def test_pull_request_model_constructs():
    pr = PullRequest(
        repo="org/repo",
        number=42,
        title="Fix AL-1",
        url="https://github.com/org/repo/pull/42",
        opened_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        is_review_requested=True,
        is_authored_by_me=False,
        age_score=5.0,
    )
    assert pr.number == 42
    assert pr.is_review_requested is True


def test_tool_result_success_and_failure_variants():
    success = ToolResult[list[Issue]](ok=True, data=[], error=None)
    assert success.ok is True
    assert success.data == []
    assert success.error is None

    failure = ToolResult[list[Issue]](ok=False, data=None, error="boom")
    assert failure.ok is False
    assert failure.data is None
    assert failure.error == "boom"

    pr_result = ToolResult[list[PullRequest]](ok=True, data=[], error=None)
    assert pr_result.data == []


def test_ask_response_defaults_warnings_to_empty_list():
    response = AskResponse(answer="you're all caught up", tool_calls=[])
    assert response.warnings == []


def test_ask_response_defaults_pending_comment_draft_to_none():
    response = AskResponse(answer="you're all caught up", tool_calls=[])
    assert response.pending_comment_draft is None


def test_comment_draft_model_constructs():
    draft = CommentDraft(issue_key="AL-13", note_text="quick thought here")
    assert draft.issue_key == "AL-13"
    assert draft.note_text == "quick thought here"
