import json
import re

from app.core.models import AskResponse, CommentDraft, Issue, PullRequest
from app.core.ranking import is_overdue, priority_emoji
from app.core.text import apply_outside_links, jira_issue_url
from app.slack.digest import Digest, DigestSection, StandupSummary


def _issue_bullet(
    issue: Issue, *, number: int | None = None, emoji: str | None = None
) -> str:
    url = jira_issue_url(issue.key)
    resolved_emoji = (
        emoji
        if emoji is not None
        else priority_emoji(issue.priority, issue.due_date)
    )
    # Unnumbered lists use the priority emoji itself as the bullet rather than
    # a separate "•" - a literal bullet char next to an emoji double-marks the
    # same line. Numbered (ranked) lists keep the number, which the emoji can't
    # substitute for.
    prefix = f"{number}. {resolved_emoji}" if number is not None else resolved_emoji
    line = f"{prefix} <{url}|{issue.key}> — {issue.summary}"

    details: list[str] = []
    if is_overdue(issue.due_date):
        details.append("overdue")
    elif issue.due_date is not None:
        details.append(f"due {issue.due_date.strftime('%b %-d')}")
    if issue.linked_pr is not None:
        details.append(f"<{issue.linked_pr.url}|PR #{issue.linked_pr.number}>")
    if details:
        line += f" ({', '.join(details)})"
    return line


def _heading_block(heading: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{heading}*"}}


def _issue_actions_block(issue: Issue) -> dict:
    url = jira_issue_url(issue.key)
    elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open in Jira"},
            "url": url,
            "action_id": "digest_open_jira",
        }
    ]
    if issue.linked_pr is not None:
        elements.append(
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": f"View PR #{issue.linked_pr.number}",
                },
                "url": issue.linked_pr.url,
                "action_id": "digest_view_pr",
            }
        )
    return {"type": "actions", "elements": elements}


# High Priority gets one section+actions block pair per issue (a button row
# under each), since rank order and per-issue links are the point of this
# section. Every item here is forced red rather than colored by its own
# priority field - membership in this section already means "high priority OR
# due soon" (see digest.py's build_digest fallback chain), so a Medium-priority
# issue that landed here via the due-soon path should still read as urgent.
# Upcoming stays a single combined block, colored per-issue - lower emphasis,
# no buttons, same vertical-list-only layout as before.
def _high_priority_blocks(section: DigestSection) -> list[dict]:
    blocks: list[dict] = [_heading_block(section.heading)]
    for i, issue in enumerate(section.items, start=1):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _issue_bullet(issue, number=i, emoji="🔴"),
                },
            }
        )
        blocks.append(_issue_actions_block(issue))
    return blocks


def _upcoming_blocks(section: DigestSection) -> list[dict]:
    if not section.items:
        text = f"*{section.heading}*\nNothing else on deck"
    else:
        bullets = "\n".join(_issue_bullet(issue) for issue in section.items)
        text = f"*{section.heading}*\n{bullets}"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _pr_bullet(pr: PullRequest) -> str:
    return f"• <{pr.url}|{pr.repo}#{pr.number}> — {pr.title}"


# Omitted entirely when empty, unlike High Priority/Upcoming - this is
# supplementary discovery content (and unset GITHUB_REPO means it's always
# empty for anyone who hasn't opted in), not a core section the anti-ambiguity
# "always show an explicit state" principle applies to.
def _prs_to_review_blocks(prs: list[PullRequest]) -> list[dict]:
    if not prs:
        return []
    bullets = "\n".join(_pr_bullet(pr) for pr in prs)
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*PRs You Could Review*\n{bullets}"},
        }
    ]


# Deterministic, not LLM-narrated - the digest script never calls Claude (see
# CLAUDE.md's "deterministic Python ranking, not LLM-driven" project purpose).
# Names the top-ranked High Priority issue as the lead, since that's already
# the single most important thing per this digest's own ranking; everything
# else is a count, not a restatement of items already listed above.
def _digest_summary_blocks(digest: Digest) -> list[dict]:
    high_items = digest.high_priority.items
    upcoming_count = len(digest.upcoming.items)
    review_count = len(digest.prs_to_review)

    if high_items:
        top = high_items[0]
        lead = f"Start with <{jira_issue_url(top.key)}|{top.key}>"
        rest = len(high_items) - 1
        if rest:
            lead += f", plus {rest} more high-priority item{'s' if rest != 1 else ''}"
    else:
        lead = "Nothing high-priority or due soon right now"

    trailing = []
    if upcoming_count:
        trailing.append(f"{upcoming_count} upcoming")
    if review_count:
        plural = "s" if review_count != 1 else ""
        trailing.append(f"{review_count} PR{plural} to review")
    text = f"{lead} — {', '.join(trailing)}." if trailing else f"{lead}."

    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{text}"}},
    ]


def format_digest(digest: Digest) -> list[dict]:
    return (
        _high_priority_blocks(digest.high_priority)
        + _upcoming_blocks(digest.upcoming)
        + _prs_to_review_blocks(digest.prs_to_review)
        + _digest_summary_blocks(digest)
    )


def format_standup_summary(summary: StandupSummary) -> list[dict]:
    if not summary.doing and not summary.reviewing and not summary.next_up:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Nothing high-priority or awaiting your review right now.",
                },
            }
        ]
    blocks: list[dict] = []
    if summary.doing:
        bullets = "\n".join(_issue_bullet(issue) for issue in summary.doing)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Doing*\n{bullets}"},
            }
        )
    if summary.reviewing:
        bullets = "\n".join(_pr_bullet(pr) for pr in summary.reviewing)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Reviewing*\n{bullets}"},
            }
        )
    if summary.next_up:
        bullets = "\n".join(_issue_bullet(issue) for issue in summary.next_up)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Next Up*\n{bullets}"},
            }
        )
    return blocks


# Slack notification previews render this top-level `text`, not `blocks`.
def format_standup_summary_fallback_text(summary: StandupSummary) -> str:
    return (
        f"Standup summary: {len(summary.doing)} doing, "
        f"{len(summary.reviewing)} to review, {len(summary.next_up)} next up"
    )


# Slack notification previews render this top-level `text`, not `blocks`.
def format_digest_fallback_text(digest: Digest) -> str:
    return f"Daily digest: {digest.high_priority.heading}, {digest.upcoming.heading}"


# --- /ask answer formatting -------------------------------------------------
#
# response.answer is free text from Claude. The system prompt (orchestrator.py)
# instructs it to write Slack mrkdwn directly, but LLM instruction-following
# isn't a guarantee - these four passes are a deterministic safety net /
# enrichment layer applied regardless of what the model actually produced:
#   1. normalize stray standard-Markdown syntax to Slack mrkdwn
#   2. hyperlink "PR #<n>" mentions using AskResponse.referenced_prs
#   3. hyperlink + priority-flag issue-key mentions using referenced_issues -
#      first mention of a key gets both the key and its title linked, every
#      later mention of the same key is just the linked key (see
#      _hyperlink_and_flag_issues)
#   4. drop the "•" Claude wrote when step 3 put a priority emoji right after
#      it - the emoji is the bullet now, not a second marker alongside it
# Both reference lists are built by the orchestrator from tool data actually
# fetched this turn (see orchestrator.py:_record_referenced_data), never by
# guessing a URL from text alone.

_MD_HEADER_PATTERN = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MD_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_MD_BULLET_PATTERN = re.compile(r"^[*-]\s+", re.MULTILINE)
_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _sanitize_markdown_for_slack(text: str) -> str:
    text = _MD_HEADER_PATTERN.sub(r"*\1*", text)
    text = _MD_BOLD_PATTERN.sub(r"*\1*", text)
    text = _MD_BULLET_PATTERN.sub("• ", text)
    text = _MD_LINK_PATTERN.sub(r"<\2|\1>", text)
    return text


# Mirrors app.tools.jira_tools._ISSUE_KEY_PATTERN - kept as its own constant
# rather than importing the tools layer, since formatting.py is Slack
# presentation and shouldn't depend on it.
_ISSUE_KEY_PATTERN = re.compile(r"[A-Z]+-\d+")
_PR_MENTION_PATTERN = re.compile(r"PR\s*#(\d+)", re.IGNORECASE)
_SLACK_LINK_PATTERN = re.compile(r"<[^<>]+>")


def _hyperlink_prs(text: str, referenced_prs: list[PullRequest]) -> str:
    by_number = {pr.number: pr for pr in referenced_prs}

    def replace(match: re.Match[str]) -> str:
        pr = by_number.get(int(match.group(1)))
        if pr is None:
            return match.group(0)
        return f"<{pr.url}|PR #{pr.number}>"

    return apply_outside_links(
        text, _SLACK_LINK_PATTERN, lambda s: _PR_MENTION_PATTERN.sub(replace, s)
    )


# First mention of a ticket links both its key and its title (two separate
# link spans, both clickable) with the priority emoji leading; every mention
# after that links just the bare key, no emoji or title repeated - avoids
# restating the same title over and over in an answer that references one
# ticket several times. "First" tracks order of appearance in the text, not
# insertion order in referenced_issues.
def _hyperlink_and_flag_issues(text: str, referenced_issues: list[Issue]) -> str:
    by_key = {issue.key: issue for issue in referenced_issues}
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(0)
        issue = by_key.get(key)
        if issue is None:
            return key
        url = jira_issue_url(key)
        if key in seen:
            return f"<{url}|{key}>"
        seen.add(key)
        emoji = priority_emoji(issue.priority, issue.due_date)
        return f"{emoji} <{url}|{key}> <{url}|{issue.summary}>"

    return apply_outside_links(
        text, _SLACK_LINK_PATTERN, lambda s: _ISSUE_KEY_PATTERN.sub(replace, s)
    )


# Claude writes its own "•" per the system prompt's bullet instruction, then
# _hyperlink_and_flag_issues inserts a priority emoji at the start of whatever
# it links first on that line - the two compose into a double-marked "• 🔴 ...".
# Applied last, after both hyperlinking passes, since only their emoji
# insertions can produce this pattern.
_BULLET_BEFORE_PRIORITY_EMOJI_PATTERN = re.compile(r"^• (?=[🔴🟡🟢])", re.MULTILINE)


def _drop_bullet_before_priority_emoji(text: str) -> str:
    return _BULLET_BEFORE_PRIORITY_EMOJI_PATTERN.sub("", text)


# Offered on the first reply in a conversation, never inferred from what was
# asked (see CLAUDE.md/conversation: the assistant must not assume "what
# should I work on today" implies a standup summary is wanted) - but also
# never repeated on every thread reply after that, since a multi-turn
# back-and-forth would otherwise end with a stack of these prompts, one per
# reply. bolt_app.py's _answer_and_reply decides "first reply" by checking
# whether the thread had no prior history before this call.
_STANDUP_FOLLOWUP_PROMPT = (
    "Is this enough for standup, or would you like a more succinct summary "
    "to prep with?"
)


# These 3 blocks live inside the same single message as the main answer and
# the standup-followup prompt (format_ask_response returns one combined list,
# posted via one say() call) - block_id lets bolt_app.py's handle_confirm_comment
# find and replace just these blocks via response_url, without disturbing the
# answer text or the standup buttons that share the same message.
COMMENT_DRAFT_BLOCK_ID_PREFIX = "comment_draft"


# First button in this codebase to carry a `value` payload - every existing
# button either opens a `url` client-side or has the handler re-derive
# everything it needs from body["message"]. The actual Jira write happens
# only in bolt_app.py's ask_confirm_comment handler, never here and never in
# the orchestrator's tool-calling loop.
def _comment_draft_blocks(draft: CommentDraft) -> list[dict]:
    value = json.dumps({"issue_key": draft.issue_key, "note_text": draft.note_text})
    return [
        {"type": "divider", "block_id": f"{COMMENT_DRAFT_BLOCK_ID_PREFIX}_divider"},
        {
            "type": "section",
            "block_id": f"{COMMENT_DRAFT_BLOCK_ID_PREFIX}_section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Draft comment for {draft.issue_key}:*\n> {draft.note_text}",
            },
        },
        {
            "type": "actions",
            "block_id": f"{COMMENT_DRAFT_BLOCK_ID_PREFIX}_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Post to Jira"},
                    "style": "primary",
                    "action_id": "ask_confirm_comment",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Discard"},
                    "action_id": "ask_discard_comment_draft",
                },
            ],
        },
    ]


# Used by bolt_app.py's handle_confirm_comment after a click, so the
# original message's answer text and standup buttons survive a
# response_url replace_original call - only the draft's own 3 blocks (tagged
# above) are swapped out for a single outcome line, in place.
def replace_comment_draft_blocks(blocks: list[dict], outcome_text: str) -> list[dict]:
    draft_indices = [
        i
        for i, block in enumerate(blocks)
        if block.get("block_id", "").startswith(COMMENT_DRAFT_BLOCK_ID_PREFIX)
    ]
    if not draft_indices:
        return blocks
    outcome_block = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": outcome_text},
    }
    remaining = [b for i, b in enumerate(blocks) if i not in draft_indices]
    insert_at = draft_indices[0]
    return [*remaining[:insert_at], outcome_block, *remaining[insert_at:]]


def format_ask_response(
    response: AskResponse, *, include_standup_prompt: bool = True
) -> list[dict]:
    text = _sanitize_markdown_for_slack(response.answer)
    text = _hyperlink_prs(text, response.referenced_prs)
    text = _hyperlink_and_flag_issues(text, response.referenced_issues)
    text = _drop_bullet_before_priority_emoji(text)
    draft_blocks = (
        _comment_draft_blocks(response.pending_comment_draft)
        if response.pending_comment_draft is not None
        else []
    )
    standup_blocks: list[dict] = (
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _STANDUP_FOLLOWUP_PROMPT},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "This is enough"},
                        "action_id": "ask_standup_dismiss",
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Give me a succinct summary",
                        },
                        "action_id": "ask_standup_summary",
                    },
                ],
            },
        ]
        if include_standup_prompt
        else []
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        *draft_blocks,
        *standup_blocks,
    ]
