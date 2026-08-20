import re

from slack_bolt.async_app import AsyncApp
from slack_bolt.context.ack.async_ack import AsyncAck
from slack_bolt.context.say.async_say import AsyncSay

from app.agent.orchestrator import OrchestratorUnavailable, handle_query
from app.config import settings
from app.core.errors import unwrap_tool_result
from app.slack.digest import build_standup_summary
from app.slack.formatting import (
    format_ask_response,
    format_standup_summary,
    format_standup_summary_fallback_text,
)
from app.tools import github_tools, jira_tools

_MENTION_PREFIX = re.compile(r"^\s*<@[A-Z0-9]+>\s*")

# Raises if settings.slack_bot_token is falsy - only import this module once
# Slack is confirmed configured (see app/main.py's lifespan handler, which
# imports it conditionally rather than at module load).
slack_app = AsyncApp(token=settings.slack_bot_token)


def _strip_bot_mention(text: str) -> str:
    return _MENTION_PREFIX.sub("", text)


@slack_app.event("app_mention")
async def handle_mention(event: dict, say: AsyncSay) -> None:
    query = _strip_bot_mention(event["text"])
    try:
        response = await handle_query(query)
    except OrchestratorUnavailable:
        await say(
            text="Sorry, I couldn't reach the assistant just now.",
            thread_ts=event["ts"],
        )
        return
    await say(
        blocks=format_ask_response(response),
        text=response.answer,
        thread_ts=event["ts"],
    )


# "Open in Jira" / "View PR" digest buttons and "This is enough" are plain
# acknowledgements - Slack opens `url` buttons client-side regardless, but the
# interaction still has to be ack'd within 3s or the user sees an error toast.
async def _ack_only(ack: AsyncAck) -> None:
    await ack()


slack_app.action("digest_open_jira")(_ack_only)
slack_app.action("digest_view_pr")(_ack_only)
slack_app.action("ask_standup_dismiss")(_ack_only)


@slack_app.action("ask_standup_summary")
async def handle_standup_summary_request(
    ack: AsyncAck, body: dict, say: AsyncSay
) -> None:
    await ack()

    # get_my_issues_with_linked_prs (not get_my_high_priority_issues) so that
    # any in-flight issue's still-open PR comes along with it - build_standup_summary
    # relies on that to surface "blocked on PR review" via the issue's own
    # linked_pr, not a separate lookup.
    in_flight_issues = unwrap_tool_result(
        "get_my_issues_with_linked_prs",
        await jira_tools.get_my_issues_with_linked_prs(),
    )
    prs_awaiting_review = unwrap_tool_result(
        "get_prs_awaiting_my_review", await github_tools.get_prs_awaiting_my_review()
    )
    summary = build_standup_summary(
        in_flight_issues=in_flight_issues,
        prs_awaiting_review=prs_awaiting_review,
    )

    message = body["message"]
    thread_ts = message.get("thread_ts") or message["ts"]
    await say(
        blocks=format_standup_summary(summary),
        text=format_standup_summary_fallback_text(summary),
        thread_ts=thread_ts,
    )
