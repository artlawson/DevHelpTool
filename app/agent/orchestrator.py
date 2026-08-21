import asyncio
import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.agent.registry import TOOL_REGISTRY
from app.agent.schemas import TOOL_SCHEMAS
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import AskResponse, CommentDraft, Issue, PullRequest

MAX_ITERATIONS = 6
MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are an engineering-status assistant with access to read-only tools over \
Jira and GitHub. Call whichever tools are relevant to the user's question, \
including multiple tools in the same turn when the question spans both \
systems (for example, "what should I work on today" needs high-priority \
issues, issues without PRs, PRs awaiting your review, and your own open PRs \
still waiting on someone else's review).

Base your final answer only on the data returned by tools - never invent \
ticket keys, PR numbers, or counts. Produce a concise, prioritized summary, \
not a restatement of raw tool output. If a tool result reports an error, \
acknowledge the gap in your answer (e.g. "GitHub data is unavailable, \
showing Jira only") rather than silently omitting it.

When the user names a specific teammate and asks what they're working on, \
call jira_get_persons_open_issues with that person's name or email exactly \
as they said it - never answer that question from jira_get_my_* tools, \
which are always scoped to the current user.

Call jira_get_issues_awaiting_my_response on broad status questions (e.g. \
"what should I work on today", "what's my status"). If it returns anything, \
weave "someone's waiting on your reply on <key>" into your narration - and \
into the Focus line when it's the most pressing thing - the same way you \
already combine high-priority issues, overdue items, and PR review requests \
into one coherent recommendation.

When the user wants to leave a note on a ticket (e.g. they don't have time \
to work on it but have a quick thought), call jira_draft_comment with the \
exact literal issue key and the user's note text written close to what they \
said, not your own summary of it. This does not post anything - a separate, \
human-confirmed step outside your control handles the actual write. Never \
tell the user the comment has been posted; at most, confirm you've drafted \
it and that they'll be asked to approve it.

Formatting rules for your final answer, no exceptions:
- Use Slack's markup only, never standard Markdown: bold is a single \
asterisk (*like this*), never double asterisks; bullets are "•", never "-" \
or "*"; never use "#"-style headers; never use "[text](url)" markdown \
links - just write the bare ticket key or "PR #<number>" and it will be \
linked automatically, so don't build the link yourself.
- When your answer identifies one clear next action, start with a single \
bolded line naming it, formatted exactly as "*Focus: <the one thing, in a \
few words>*", before any other detail. Skip this line if there genuinely \
isn't one standout item (e.g. a purely informational question, or several \
equally-weighted items).
- Refer to Jira issues by their exact literal key (e.g. "AL-12") and GitHub \
pull requests as "PR #<number>" - not a paraphrase, not a markdown link - \
so they can be matched and linked automatically downstream.
- Never state a ticket's priority level in words (e.g. "this is High \
priority", "a Medium-priority ticket") and never name which section/bucket \
it came from - a priority-colored dot is added automatically next to every \
linked ticket, and restating the same thing in words next to it is \
redundant.
- When you note that a ticket has no PR yet, always use the exact same \
phrase every time it comes up in an answer: "no PR started" - not "no PR \
yet", "hasn't been started", "missing a PR", or any other variation.
- Whenever you mention two or more tickets or PRs, list them one per bullet \
line ("•") - never comma-separate them or run them together in a sentence, \
even for an answer about a single topic/category. A single ticket or PR \
mentioned in passing can stay inline prose.
- When the answer spans more than one category of data, format it as one \
short bulleted section per category: a bolded label (e.g. "*High-priority \
issues:*"), followed by "•" bullet points, or "None" if that category is \
empty.

For questions unrelated to Jira/GitHub work status, answer directly without \
calling any tool.
"""

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


class OrchestratorUnavailable(Exception):
    """Raised when the Anthropic API call itself fails (auth/network/rate limit)."""


def extract_text(content: list[Any]) -> str:
    return "".join(block.text for block in content if block.type == "text")


def _serialize_tool_data(data: Any) -> Any:
    """Tool results are typically list[BaseModel] (e.g. list[Issue]); handle
    that shape explicitly since a list itself has no model_dump()."""
    if isinstance(data, list):
        return [item.model_dump(mode="json") for item in data]
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    return data


# Keyed by Jira key / PR number so repeated mentions across tool calls just
# overwrite with the latest fetch rather than duplicating. Populated here
# (not derived from the LLM's answer text) so Slack's formatting.py can
# hyperlink/flag exactly what was actually fetched, never a guess.
def _record_referenced_data(
    data: Any, known_issues: dict[str, Issue], known_prs: dict[int, PullRequest]
) -> None:
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, Issue):
            known_issues[item.key] = item
            if item.linked_pr is not None:
                known_prs[item.linked_pr.number] = item.linked_pr
        elif isinstance(item, PullRequest):
            known_prs[item.number] = item


def _record_comment_draft(data: Any, draft_holder: list[CommentDraft]) -> None:
    if isinstance(data, CommentDraft):
        draft_holder.append(data)


async def dispatch(
    block: Any,
    called_tools: list[str],
    warnings: list[str],
    known_issues: dict[str, Issue],
    known_prs: dict[int, PullRequest],
    draft_holder: list[CommentDraft],
) -> dict:
    tool_fn = TOOL_REGISTRY[block.name]
    try:
        result = await tool_fn(**block.input)
    except Exception as exc:
        message = sanitize_error(exc)
        warnings.append(f"{block.name}: {message}")
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"error: {message}",
            "is_error": True,
        }

    if not result.ok:
        warnings.append(f"{block.name}: {result.error}")
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"error: {result.error}",
            "is_error": True,
        }

    called_tools.append(block.name)
    _record_referenced_data(result.data, known_issues, known_prs)
    _record_comment_draft(result.data, draft_holder)
    payload = _serialize_tool_data(result.data)
    if result.note:
        payload = {"data": payload, "note": result.note}
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": json.dumps(payload),
    }


async def _run_loop(messages: list[dict]) -> tuple[AskResponse, list[dict]]:
    """The tool-calling loop shared by handle_query (fresh, single-turn) and
    handle_conversational_query (prior turns prepended into `messages`
    already). Mutates and returns `messages` so a caller can carry the full
    exchange forward as the next call's history."""
    warnings: list[str] = []
    called_tools: list[str] = []
    known_issues: dict[str, Issue] = {}
    known_prs: dict[int, PullRequest] = {}
    draft_holder: list[CommentDraft] = []

    for iteration in range(MAX_ITERATIONS):
        force_final = iteration == MAX_ITERATIONS - 1
        try:
            # Plain dicts (not the SDK's TypedDict param constructors) are used
            # for messages/tools/tool_choice - structurally correct at runtime,
            # but mypy's strict overload matching can't verify that.
            response = await anthropic_client.messages.create(  # type: ignore[call-overload]
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                tool_choice={"type": "none"} if force_final else {"type": "auto"},
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise OrchestratorUnavailable(str(exc)) from exc

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            ask_response = AskResponse(
                answer=extract_text(response.content),
                tool_calls=called_tools,
                warnings=warnings,
                referenced_issues=list(known_issues.values()),
                referenced_prs=list(known_prs.values()),
                pending_comment_draft=draft_holder[-1] if draft_holder else None,
            )
            return ask_response, messages

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(
            *[
                dispatch(
                    block,
                    called_tools,
                    warnings,
                    known_issues,
                    known_prs,
                    draft_holder,
                )
                for block in tool_use_blocks
            ]
        )
        messages.append({"role": "user", "content": results})

    # Unreachable in practice: the forced tool_choice="none" turn above
    # always returns stop_reason != "tool_use". Kept as a defensive guard.
    raise OrchestratorUnavailable("model did not produce a final answer")


async def handle_query(query: str) -> AskResponse:
    messages: list[dict] = [{"role": "user", "content": query}]
    response, _ = await _run_loop(messages)
    return response


# Bounds token cost for a long-running Slack thread or CLI session - without
# this, history grows every turn for as long as the conversation continues.
MAX_HISTORY_TURNS = 5


def _cap_history(messages: list[dict], max_turns: int) -> list[dict]:
    """Keeps only the last `max_turns` complete turns. A turn boundary is a
    user message whose content is a plain string (the query itself) - every
    other message _run_loop appends (the assistant's response.content, or the
    user-role tool_result followups) never has string content, so this can
    never cut in the middle of a tool_use/tool_result exchange, which would
    produce a sequence the Anthropic API would reject."""
    boundaries = [
        i
        for i, message in enumerate(messages)
        if message["role"] == "user" and isinstance(message["content"], str)
    ]
    if len(boundaries) <= max_turns:
        return messages
    return messages[boundaries[-max_turns] :]


async def handle_conversational_query(
    query: str, history: list[dict] | None = None
) -> tuple[AskResponse, list[dict]]:
    """Like handle_query, but prepends `history` (a prior call's returned
    messages) so the model has the earlier turns of the conversation, and
    returns the updated, capped history for the caller to pass into the next
    call. Slack (per-thread) and the CLI (per-session) hold this in memory
    between calls; `/ask` has no equivalent concept and stays on handle_query."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": query})
    response, updated_messages = await _run_loop(messages)
    return response, _cap_history(updated_messages, MAX_HISTORY_TURNS)
