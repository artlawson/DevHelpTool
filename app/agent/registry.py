from collections.abc import Awaitable, Callable

from app.core.models import ToolResult
from app.tools import github_tools, jira_tools

TOOL_REGISTRY: dict[str, Callable[[], Awaitable[ToolResult]]] = {
    "jira_get_my_high_priority_issues": jira_tools.get_my_high_priority_issues,
    "jira_get_issues_without_prs": jira_tools.get_issues_without_prs,
    "jira_get_my_issues_with_linked_prs": jira_tools.get_my_issues_with_linked_prs,
    "jira_get_incomplete_issues_from_last_sprint": (
        jira_tools.get_incomplete_issues_from_last_sprint
    ),
    "github_get_my_open_prs": github_tools.get_my_open_prs,
    "github_get_prs_awaiting_my_review": github_tools.get_prs_awaiting_my_review,
}
