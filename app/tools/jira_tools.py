import asyncio
import re
from datetime import UTC, date, datetime

from app.clients.jira_client import JiraClient
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import CommentDraft, Issue, PullRequest, ToolResult
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


# Best-effort plain-text extraction from an Atlassian Document Format node -
# concatenates every "text" leaf found anywhere in the tree. Not a full ADF
# renderer (list/table structure isn't preserved); only used for display, never
# for the empty/non-empty check below.
def _adf_text(node: dict) -> str:
    text = node.get("text", "")
    for child in node.get("content", []):
        child_text = _adf_text(child)
        if child_text:
            text = f"{text} {child_text}".strip() if text else child_text
    return text.strip()


# Jira returns `description` as an Atlassian Document Format (ADF) object, not
# plain text. A doc with no content, or whose only content is empty paragraphs,
# counts as empty; any other node type (list, code block, media, etc.) or a
# paragraph with real content means it isn't.
def _is_empty_description(raw_description: dict | None) -> bool:
    if not raw_description:
        return True
    content = raw_description.get("content") or []
    for node in content:
        if node.get("type") != "paragraph":
            return False
        if node.get("content"):
            return False
    return True


def _map_issue(raw: dict, *, linked_pr: PullRequest | None = None) -> Issue:
    fields = raw["fields"]
    due_date_str = fields.get("duedate")
    due_date = date.fromisoformat(due_date_str) if due_date_str else None
    priority = fields["priority"]["name"]
    score = score_issue(RawIssue(priority=priority, due_date=due_date))
    raw_description = fields.get("description")
    description = (
        None if _is_empty_description(raw_description) else _adf_text(raw_description)
    )
    return Issue(
        key=raw["key"],
        summary=fields["summary"],
        priority=priority,
        status=fields["status"]["name"],
        due_date=due_date,
        description=description,
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
        issues = [_map_issue(raw) for raw in raw_issues]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

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
        issues_without_prs = [
            _map_issue(raw) for raw in raw_issues if raw["key"] not in raw_prs_by_key
        ]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    return ToolResult(
        ok=True,
        data=rank(issues_without_prs, lambda i: i.priority_score),
        error=None,
    )


async def get_my_issues_with_linked_prs() -> ToolResult[list[Issue]]:
    try:
        raw_issues = await jira_client.search(_UNRESOLVED_JQL)
        raw_prs_by_key = await _fetch_raw_prs_by_issue_key()
        issues = [
            _map_issue(
                raw,
                linked_pr=to_pull_request(raw_prs_by_key[raw["key"]])
                if raw["key"] in raw_prs_by_key
                else None,
            )
            for raw in raw_issues
        ]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


async def get_current_sprint_issues() -> ToolResult[list[Issue]]:
    try:
        boards = await jira_client.get_boards()
        active_sprints: list[dict] = []
        for board in boards:
            active_sprints.extend(await jira_client.get_active_sprints(board["id"]))

        if not active_sprints:
            # Unlike "no closed sprint" (below), "no active sprint right now" has
            # exactly one meaning - no note/ambiguity handling needed here.
            return ToolResult(ok=True, data=[], error=None)

        sprint_ids = ",".join(str(s["id"]) for s in active_sprints)
        raw_issues = await jira_client.search(
            f"sprint in ({sprint_ids}) AND {_UNRESOLVED_JQL}"
        )
        issues = [_map_issue(raw) for raw in raw_issues]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


async def get_lower_priority_issues_due_soon(days: int = 7) -> ToolResult[list[Issue]]:
    jql = (
        "assignee = currentUser() AND priority in (Medium, Low, Lowest) "
        f'AND duedate >= startOfDay() AND duedate <= startOfDay("+{days}d") '
        "AND resolution = Unresolved ORDER BY duedate ASC"
    )
    try:
        raw_issues = await jira_client.search(jql)
        issues = [_map_issue(raw) for raw in raw_issues]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


async def get_backlog_issues_needing_details() -> ToolResult[list[Issue]]:
    jql = (
        "(reporter = currentUser() OR assignee = currentUser()) "
        "AND sprint is EMPTY AND resolution = Unresolved "
        "ORDER BY created ASC"
    )
    try:
        raw_issues = await jira_client.search(jql)
        issues = [
            _map_issue(raw)
            for raw in raw_issues
            if _is_empty_description(raw["fields"].get("description"))
        ]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

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
        issues = [_map_issue(raw) for raw in raw_issues]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


async def get_persons_open_issues(person: str) -> ToolResult[list[Issue]]:
    """Fetch a named teammate's (not the current user's) assigned, unresolved
    issues. PR-linking is intentionally not wired in here - the existing
    linking helper (_fetch_raw_prs_by_issue_key) is hardcoded to
    settings.github_username, i.e. "my" authored PRs only; extending it to an
    arbitrary other GitHub author is a separate, bigger change."""
    try:
        raw_users = await jira_client.search_users(person)
        users = [u for u in raw_users if u.get("active", True)]
        exact = [
            u
            for u in users
            if u.get("emailAddress", "").lower() == person.lower()
            or u.get("displayName", "").lower() == person.lower()
        ]
        # Prefer exact matches whenever any exist, even 2+ of them - falling
        # back to the full fuzzy `users` set here would pull in irrelevant
        # fuzzy-only matches (and could even drop one of the real exact
        # matches via the [:5] truncation below).
        matched = exact or users

        if not matched:
            return ToolResult(
                ok=True,
                data=[],
                error=None,
                note=f'No Jira user found matching "{person}".',
            )
        if len(matched) > 1:
            names = ", ".join(
                f"{u.get('displayName', 'unknown')} "
                f"({u.get('emailAddress', 'no email on file')})"
                for u in matched[:5]
            )
            return ToolResult(
                ok=True,
                data=[],
                error=None,
                note=(
                    f'Multiple Jira users match "{person}": {names}. '
                    "Ask which one is meant."
                ),
            )

        account_id = matched[0]["accountId"]
        jql = (
            f'assignee = "{account_id}" '
            "AND resolution = Unresolved ORDER BY updated DESC"
        )
        raw_issues = await jira_client.search(jql)
        issues = [_map_issue(raw) for raw in raw_issues]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)


def _plain_text_to_adf(text: str) -> dict:
    # ADF has no notion of a raw "\n" inside a text node - it renders as one
    # run-on line unless each line is its own paragraph node, so split on
    # newlines rather than packing the whole note into a single text leaf.
    # A blank line becomes a paragraph with no content - ADF text nodes can't
    # be empty strings (same empty-paragraph shape _is_empty_description
    # already treats as empty, above).
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        if line
        else {"type": "paragraph", "content": []}
        for line in text.split("\n")
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


# Registered in TOOL_REGISTRY/TOOL_SCHEMAS - LLM-callable, but performs no
# Jira I/O at all. Only packages the user's own note text + the literal issue
# key into a CommentDraft; the actual write happens exclusively in
# post_comment below, which is never registered as an LLM tool and is only
# ever invoked after an explicit, separate human confirmation (CLI y/n
# prompt, Slack button) - mirroring the standup-summary bypass-the-LLM
# precedent (app/slack/bolt_app.py:handle_standup_summary_request).
async def draft_comment(issue_key: str, note_text: str) -> ToolResult[CommentDraft]:
    return ToolResult(
        ok=True, data=CommentDraft(issue_key=issue_key, note_text=note_text), error=None
    )


# NOT registered in TOOL_REGISTRY/TOOL_SCHEMAS - the orchestrator's
# tool-calling loop must never reach this. Called only by app/cli.py's
# confirm prompt or app/slack/bolt_app.py's ask_confirm_comment handler,
# after the human has seen the exact drafted text and explicitly said yes.
async def post_comment(issue_key: str, note_text: str) -> ToolResult[str]:
    try:
        raw = await jira_client.add_comment(issue_key, _plain_text_to_adf(note_text))
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))
    return ToolResult(ok=True, data=raw.get("id", ""), error=None)


# Mirrors _adf_text's recursive tree-walk above. A mention node looks like
# {"type": "mention", "attrs": {"id": "<accountId>", ...}} nested anywhere in
# a comment body's content tree.
def _adf_contains_mention(node: dict, account_id: str) -> bool:
    if node.get("type") == "mention" and node.get("attrs", {}).get("id") == account_id:
        return True
    return any(
        _adf_contains_mention(child, account_id) for child in node.get("content", [])
    )


# Walks comments in chronological order (guaranteed by JiraClient.get_comments'
# orderBy=created). A comment authored by me clears any prior unreplied-to
# mention; a mention with no *subsequent comment by me* stays "awaiting" even
# if someone else commented after it - this last clause is the deliberate
# distinction from the simpler, rejected "last comment isn't mine" rule (that
# rule false-positives whenever someone else's comment is actually the
# resolution, not a question aimed at me).
def _awaiting_response(comments: list[dict], my_account_id: str) -> bool:
    awaiting = False
    for comment in comments:
        author_id = comment.get("author", {}).get("accountId")
        if author_id == my_account_id:
            awaiting = False
        elif _adf_contains_mention(comment.get("body", {}), my_account_id):
            awaiting = True
    return awaiting


# Broader than "assigned to me" (reporter OR assignee) since a teammate can
# ask a clarifying question on an issue I only filed but isn't assigned to me.
# Kept as its own dedicated tool rather than folded into score_issue()/
# RawIssue, to avoid an expensive per-issue comment-fetch on every existing
# tool call - Claude's system prompt weaves this into narration, the same way
# cross-tool synthesis already works for e.g. "Focus: ..." today.
async def get_issues_awaiting_my_response() -> ToolResult[list[Issue]]:
    try:
        myself = await jira_client.get_myself()
        my_account_id = myself["accountId"]
        jql = (
            "(reporter = currentUser() OR assignee = currentUser()) "
            "AND resolution = Unresolved ORDER BY updated DESC"
        )
        raw_issues = await jira_client.search(jql)
        comments_by_key = dict(
            zip(
                (raw["key"] for raw in raw_issues),
                await asyncio.gather(
                    *[jira_client.get_comments(raw["key"]) for raw in raw_issues]
                ),
                strict=True,
            )
        )
        issues = [
            _map_issue(raw)
            for raw in raw_issues
            if _awaiting_response(comments_by_key[raw["key"]], my_account_id)
        ]
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)
