from datetime import date, datetime

from pydantic import BaseModel


class Issue(BaseModel):
    key: str
    summary: str
    priority: str
    status: str
    due_date: date | None
    has_linked_pr: bool
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


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[str]
    warnings: list[str] = []
