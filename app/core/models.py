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
