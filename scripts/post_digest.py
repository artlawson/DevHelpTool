import asyncio
import logging
import sys

from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings
from app.core.errors import sanitize_error, unwrap_tool_result
from app.core.ranking import is_urgent
from app.slack.digest import build_digest
from app.slack.formatting import format_digest, format_digest_fallback_text
from app.tools import github_tools, jira_tools

logger = logging.getLogger(__name__)

_LOWER_PRIORITIES = {"Medium", "Low", "Lowest"}


async def main() -> int:
    if not settings.slack_bot_token or not settings.slack_channel_id:
        logger.error(
            "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID must both be set to post the digest"
        )
        return 1

    # get_my_issues_with_linked_prs (not get_my_high_priority_issues) so a
    # High Priority issue's still-open PR comes along with it - _issue_bullet
    # already renders a linked_pr inline, which is what makes "blocked on
    # review" visible in the priority section without a separate section for
    # it (same reasoning as the standup summary's "Doing" filter below).
    unresolved_with_links = unwrap_tool_result(
        "get_my_issues_with_linked_prs",
        await jira_tools.get_my_issues_with_linked_prs(),
    )
    high_priority = [
        issue
        for issue in unresolved_with_links
        if is_urgent(issue.priority, issue.due_date)
    ]
    due_soon = unwrap_tool_result(
        "get_lower_priority_issues_due_soon",
        await jira_tools.get_lower_priority_issues_due_soon(),
    )
    current_sprint = unwrap_tool_result(
        "get_current_sprint_issues", await jira_tools.get_current_sprint_issues()
    )
    backlog = unwrap_tool_result(
        "get_backlog_issues_needing_details",
        await jira_tools.get_backlog_issues_needing_details(),
    )
    prs_i_could_review = unwrap_tool_result(
        "get_prs_i_could_review", await github_tools.get_prs_i_could_review()
    )

    # Both build_digest branches that need "the rest of the current sprint" want
    # the same lower-priority slice - compute it once and reuse it, since the
    # branches are mutually exclusive (see app/slack/digest.py).
    current_sprint_lower_priority = [
        issue for issue in current_sprint if issue.priority in _LOWER_PRIORITIES
    ]

    digest = build_digest(
        high_priority_issues=high_priority,
        due_soon_lower_priority=due_soon,
        current_sprint_lower_priority=current_sprint_lower_priority,
        current_sprint_remainder=current_sprint_lower_priority,
        backlog_needing_details=backlog,
        prs_i_could_review=prs_i_could_review,
    )

    blocks = format_digest(digest)
    fallback_text = format_digest_fallback_text(digest)

    client = AsyncWebClient(token=settings.slack_bot_token)
    try:
        await client.chat_postMessage(
            channel=settings.slack_channel_id, blocks=blocks, text=fallback_text
        )
    except Exception as exc:
        logger.error("Failed to post Slack digest: %s", sanitize_error(exc))
        return 1

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
