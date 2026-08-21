from collections.abc import Awaitable, Callable
from typing import Any

from app.core.models import ToolResult
from app.tools import github_tools, jira_tools

# Callable[..., ...] rather than a per-arity Protocol: this registry now mixes
# zero-arg tools with parameterized ones (jira_get_persons_open_issues,
# jira_draft_comment), and expressing that precisely per-tool isn't worth the
# ceremony. This does lose mypy's ability to check a tool's arity/param names
# against its TOOL_SCHEMAS entry - that check is enforced at runtime instead,
# by test_agent_registry.py.
TOOL_REGISTRY: dict[str, Callable[..., Awaitable[ToolResult[Any]]]] = {
    "jira_get_my_high_priority_issues": jira_tools.get_my_high_priority_issues,
    "jira_get_issues_without_prs": jira_tools.get_issues_without_prs,
    "jira_get_my_issues_with_linked_prs": jira_tools.get_my_issues_with_linked_prs,
    "jira_get_incomplete_issues_from_last_sprint": (
        jira_tools.get_incomplete_issues_from_last_sprint
    ),
    "jira_get_persons_open_issues": jira_tools.get_persons_open_issues,
    "jira_get_issues_awaiting_my_response": jira_tools.get_issues_awaiting_my_response,
    "jira_draft_comment": jira_tools.draft_comment,
    "github_get_my_open_prs": github_tools.get_my_open_prs,
    "github_get_prs_awaiting_my_review": github_tools.get_prs_awaiting_my_review,
}
