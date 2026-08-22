import asyncio
import json
import re
from collections import defaultdict

from slack_bolt.async_app import AsyncApp
from slack_bolt.context.ack.async_ack import AsyncAck
from slack_bolt.context.respond.async_respond import AsyncRespond
from slack_bolt.context.say.async_say import AsyncSay

from app.agent.orchestrator import OrchestratorUnavailable, handle_conversational_query
from app.config import settings
from app.core.errors import unwrap_tool_result
from app.slack.digest import build_standup_summary
from app.slack.formatting import (
    format_ask_response,
    format_standup_summary,
    format_standup_summary_fallback_text,
    replace_comment_draft_blocks,
)
from app.tools import github_tools, jira_tools

_MENTION_PREFIX = re.compile(r"^\s*<@[A-Z0-9]+>\s*")
_MENTION_TOKEN = re.compile(r"<@([A-Z0-9]+)>")

# Raises if settings.slack_bot_token is falsy - only import this module once
# Slack is confirmed configured (see app/main.py's lifespan handler, which
# imports it conditionally rather than at module load).
slack_app = AsyncApp(token=settings.slack_bot_token)

# Per-thread conversation history, in-memory only - lost on process restart,
# and unbounded in the *number* of threads tracked over the process's
# lifetime (each thread's own history is capped by orchestrator.py's
# MAX_HISTORY_TURNS). Accepted limitation, consistent with this being a
# local, single-user tool with no eviction machinery anywhere else either.
_thread_histories: dict[str, list[dict]] = {}

# Socket Mode dispatches every incoming event as its own concurrent task
# (confirmed in slack_sdk's async socket-mode client) - without a per-thread
# lock, two messages landing in the same thread close together (e.g. a quick
# follow-up correction) could both read the same pre-update history and the
# second write would silently clobber the first, dropping a whole exchange
# from what gets remembered. One lock per thread_ts, created lazily.
_thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _strip_bot_mention(text: str) -> str:
    return _MENTION_PREFIX.sub("", text)


def _mentions_bot(text: str, bot_user_id: str | None) -> bool:
    return bool(bot_user_id) and bot_user_id in _MENTION_TOKEN.findall(text)


# Shared by handle_mention and handle_thread_reply, which otherwise differ
# only in how they derive `query`/`thread_ts` - keeps the locking, history
# read/write, error handling, and reply identical between the two entry
# points rather than risking one getting a fix the other doesn't.
async def _answer_and_reply(query: str, thread_ts: str, say: AsyncSay) -> None:
    async with _thread_locks[thread_ts]:
        history = _thread_histories.get(thread_ts)
        is_initial_question = history is None
        try:
            response, history = await handle_conversational_query(query, history)
        except OrchestratorUnavailable:
            await say(
                text="Sorry, I couldn't reach the assistant just now.",
                thread_ts=thread_ts,
            )
            return
        _thread_histories[thread_ts] = history
    await say(
        blocks=format_ask_response(
            response, include_standup_prompt=is_initial_question
        ),
        text=response.answer,
        thread_ts=thread_ts,
    )


@slack_app.event("app_mention")
async def handle_mention(event: dict, say: AsyncSay) -> None:
    # .get("thread_ts") falls back to the mention's own ts only when it isn't
    # itself inside an existing thread - using event["ts"] unconditionally
    # here would reply with the mention's own ts as the thread root, forking
    # a new thread instead of continuing one a second in-thread @-mention is
    # replying within (handle_standup_summary_request below already gets
    # this right; this was a pre-existing, untested bug in this handler).
    thread_ts = event.get("thread_ts") or event["ts"]
    query = _strip_bot_mention(event["text"])
    await _answer_and_reply(query, thread_ts, say)


# Continues a conversation the bot already started via @-mention, without
# requiring a re-mention on every reply - locked design decision, not the
# more conservative "must @-mention every time" alternative.
@slack_app.event("message")
async def handle_thread_reply(event: dict, say: AsyncSay, context: dict) -> None:
    if event.get("subtype") not in (None, "thread_broadcast"):
        # Excludes message_changed/message_deleted/channel_join/etc.
        # thread_broadcast ("also send to channel") is a real, legitimate
        # reply and must NOT be excluded here.
        return
    if event.get("user") == context.get("bot_user_id"):
        return  # never respond to our own posted messages
    thread_ts = event.get("thread_ts")
    if thread_ts is None or thread_ts not in _thread_histories:
        # Only continues a thread app_mention already started - a plain
        # reply to a thread this app never began is never answered.
        return
    if _mentions_bot(event.get("text", ""), context.get("bot_user_id")):
        # Slack delivers both app_mention and this message event for a
        # message that mentions the bot - app_mention already handles it,
        # so answering here too would double-reply to the same message.
        return

    await _answer_and_reply(event["text"], thread_ts, say)


# "Open in Jira" / "View PR" digest buttons and "This is enough" are plain
# acknowledgements - Slack opens `url` buttons client-side regardless, but the
# interaction still has to be ack'd within 3s or the user sees an error toast.
async def _ack_only(ack: AsyncAck) -> None:
    await ack()


slack_app.action("digest_open_jira")(_ack_only)
slack_app.action("digest_view_pr")(_ack_only)
slack_app.action("ask_standup_dismiss")(_ack_only)
slack_app.action("ask_discard_comment_draft")(_ack_only)


@slack_app.action("ask_confirm_comment")
async def handle_confirm_comment(
    ack: AsyncAck, body: dict, respond: AsyncRespond
) -> None:
    await ack()
    payload = json.loads(body["actions"][0]["value"])
    issue_key = payload["issue_key"]
    result = await jira_tools.post_comment(issue_key, payload["note_text"])

    message = body["message"]
    text = (
        f"Comment posted to {issue_key}."
        if result.ok
        else f"Couldn't post the comment to {issue_key}: {result.error}"
    )

    # The draft's 3 blocks live inside the SAME message as the main answer and
    # the standup-followup prompt (format_ask_response posts them all in one
    # say() call) - replace_comment_draft_blocks swaps out only those 3 for a
    # single outcome line via response_url, so replace_original here can't
    # wipe out the original answer/standup buttons the way a bare
    # respond(text=...) would. This also closes the double-click-to-double-post
    # window (the buttons are gone from the message after the first click),
    # so no separate say() reply is needed here.
    new_blocks = replace_comment_draft_blocks(message.get("blocks", []), text)
    await respond(replace_original=True, blocks=new_blocks, text=text)


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
