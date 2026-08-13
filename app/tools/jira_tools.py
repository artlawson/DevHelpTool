import re
from datetime import UTC, date, datetime

from app.clients.jira_client import JiraClient
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import Issue, PullRequest, ToolResult
from app.core.ranking import RawIssue, rank, score_issue
from app.tools.github_tools import github_client, to_pull_request

jira_client = JiraClient(settings)

# Note: GitHub's Search Issues API (used by GitHubClient.search_prs) does not
# return branch refs (head.ref) for PR items - only title and body. Issue-key
# linking therefore matches against title/body text, not branch names.
_ISSUE_KEY_PATTERN = re.compile(r"[A-Z]+-\d+")

_UNRESOLVED_JQL = (
    "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
)


def _map_issue(raw: dict, *, linked_pr: PullRequest | None = None) -> Issue:
    fields = raw["fields"]
    due_date_str = fields.get("duedate")
    due_date = date.fromisoformat(due_date_str) if due_date_str else None
    priority = fields["priority"]["name"]
    score = score_issue(RawIssue(priority=priority, due_date=due_date))
    return Issue(
        key=raw["key"],
        summary=fields["summary"],
        priority=priority,
        status=fields["status"]["name"],
        due_date=due_date,
        has_linked_pr=linked_pr is not None,
        linked_pr=linked_pr,
        priority_score=score,
    )


async def get_my_high_priority_issues() -> ToolResult[list[Issue]]:
    jql = (
        "assignee = currentUser() AND priority in (High, Highest) "
        "AND resolution = Unresolved ORDER BY updated DESC"
    )
    try:
        raw_issues = await jira_client.search(jql)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [_map_issue(raw) for raw in raw_issues]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


def _pr_searchable_text(raw_pr: dict) -> str:
    return f"{raw_pr.get('title', '')} {raw_pr.get('body') or ''}"


async def _fetch_raw_prs_by_issue_key() -> dict[str, dict]:
    gh_query = f"is:pr author:{settings.github_username}"
    raw_prs = await github_client.search_prs(gh_query)
    by_key: dict[str, dict] = {}
    for raw_pr in raw_prs:
        for key in _ISSUE_KEY_PATTERN.findall(_pr_searchable_text(raw_pr)):
            by_key.setdefault(key, raw_pr)
    return by_key


async def get_issues_without_prs() -> ToolResult[list[Issue]]:
    try:
        raw_issues = await jira_client.search(_UNRESOLVED_JQL)
        raw_prs_by_key = await _fetch_raw_prs_by_issue_key()
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues_without_prs = [
        _map_issue(raw) for raw in raw_issues if raw["key"] not in raw_prs_by_key
    ]
    return ToolResult(
        ok=True,
        data=rank(issues_without_prs, lambda i: i.priority_score),
        error=None,
    )


async def get_my_issues_with_linked_prs() -> ToolResult[list[Issue]]:
    try:
        raw_issues = await jira_client.search(_UNRESOLVED_JQL)
        raw_prs_by_key = await _fetch_raw_prs_by_issue_key()
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [
        _map_issue(
            raw,
            linked_pr=to_pull_request(raw_prs_by_key[raw["key"]])
            if raw["key"] in raw_prs_by_key
            else None,
        )
        for raw in raw_issues
    ]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


async def _overdue_active_sprint_note(boards: list[dict]) -> str | None:
    active_sprints: list[dict] = []
    for board in boards:
        active_sprints.extend(await jira_client.get_active_sprints(board["id"]))

    now = datetime.now(UTC)
    overdue = [
        s
        for s in active_sprints
        if s.get("endDate") and datetime.fromisoformat(s["endDate"]) < now
    ]
    if not overdue:
        return None

    most_overdue = max(overdue, key=lambda s: s["endDate"])
    return (
        "No sprint has been closed yet, so there's no completed 'last sprint' to "
        f"report on. Did you mean \"{most_overdue['name']}\"? It was scheduled to "
        f"end {most_overdue['endDate']} but is still open, not formally closed."
    )


async def get_incomplete_issues_from_last_sprint() -> ToolResult[list[Issue]]:
    try:
        boards = await jira_client.get_boards()
        closed_sprints: list[dict] = []
        for board in boards:
            closed_sprints.extend(await jira_client.get_closed_sprints(board["id"]))

        if not closed_sprints:
            note = await _overdue_active_sprint_note(boards)
            return ToolResult(ok=True, data=[], error=None, note=note)

        last_sprint = max(
            closed_sprints,
            key=lambda s: s.get("completeDate") or s.get("endDate") or "",
        )
        raw_issues = await jira_client.search(
            f"sprint = {last_sprint['id']} AND {_UNRESOLVED_JQL}"
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [_map_issue(raw) for raw in raw_issues]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)
