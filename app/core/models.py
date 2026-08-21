from datetime import date, datetime

from pydantic import BaseModel


class Issue(BaseModel):
    key: str
    summary: str
    priority: str
    status: str
    due_date: date | None
    description: str | None = None
    has_linked_pr: bool
    linked_pr: "PullRequest | None" = None
    priority_score: float  # set by core/ranking.py, not the API layer


class PullRequest(BaseModel):
    repo: str
    number: int
    title: str
    url: str
    opened_at: datetime
    is_review_requested: bool
    is_authored_by_me: bool
    age_score: float  # set by core/ranking.py


class ToolResult[T](BaseModel):
    ok: bool
    data: T | None
    error: str | None
    note: str | None = None


# Deliberately no issue title/summary field - enriching this with anything not
# actually fetched by a tool call this turn would violate the same
# anti-hallucination principle orchestrator.py's _record_referenced_data
# already enforces for referenced_issues/referenced_prs below.
class CommentDraft(BaseModel):
    issue_key: str
    note_text: str


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[str]
    warnings: list[str] = []
    # Every Issue/PullRequest actually returned by a tool call this turn,
    # deduped by key/number - not rendered here (this module stays
    # presentation-agnostic), but Slack's formatting.py uses these to turn
    # plain-text mentions in `answer` into real hyperlinks + priority emoji
    # without having to re-fetch or guess at URLs.
    referenced_issues: list[Issue] = []
    referenced_prs: list[PullRequest] = []
    # Set only when the model called jira_draft_comment this turn - the
    # actual Jira write never happens here or anywhere in the orchestrator's
    # loop; callers (CLI, Slack) show this to the user and only invoke
    # jira_tools.post_comment() after an explicit separate confirmation.
    pending_comment_draft: CommentDraft | None = None
