import re
from datetime import date

from app.clients.jira_client import JiraClient
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import Issue, ToolResult
from app.core.ranking import RawIssue, rank, score_issue
from app.tools.github_tools import github_client

jira_client = JiraClient(settings)

# Note: GitHub's Search Issues API (used by GitHubClient.search_prs) does not
# return branch refs (head.ref) for PR items - only title and body. Issue-key
# linking therefore matches against title/body text, not branch names.
_ISSUE_KEY_PATTERN = re.compile(r"[A-Z]+-\d+")


def _map_issue(raw: dict, *, has_linked_pr: bool = False) -> Issue:
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
        has_linked_pr=has_linked_pr,
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


async def get_issues_without_prs() -> ToolResult[list[Issue]]:
    jql = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
    try:
        raw_issues = await jira_client.search(jql)
        gh_query = f"is:pr author:{settings.github_username}"
        raw_prs = await github_client.search_prs(gh_query)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    pr_text_blob = "\n".join(_pr_searchable_text(pr) for pr in raw_prs)
    referenced_keys = set(_ISSUE_KEY_PATTERN.findall(pr_text_blob))

    issues_without_prs = [
        _map_issue(raw, has_linked_pr=False)
        for raw in raw_issues
        if raw["key"] not in referenced_keys
    ]
    return ToolResult(
        ok=True,
        data=rank(issues_without_prs, lambda i: i.priority_score),
        error=None,
    )
