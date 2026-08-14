from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass
class RawIssue:
    priority: str
    due_date: date | None


@dataclass
class RawPR:
    opened_at: datetime
    is_review_requested: bool


PRIORITY_WEIGHTS: dict[str, float] = {
    "Highest": 4.0,
    "High": 3.0,
    "Medium": 2.0,
    "Low": 1.0,
    "Lowest": 0.0,
}
OVERDUE_BONUS: float = 1.5


def is_overdue(due_date: date | None) -> bool:
    return due_date is not None and due_date < datetime.now(UTC).date()


# High/Highest priority, or overdue regardless of priority - the single
# definition of "urgent enough to read as red/in-flight" shared by
# app/slack/formatting.py's priority emoji and app/slack/digest.py's standup
# "Doing" filter, so the two never silently diverge on what counts.
def is_urgent(priority: str, due_date: date | None) -> bool:
    return priority in {"Highest", "High"} or is_overdue(due_date)


def score_issue(issue: RawIssue) -> float:
    score = PRIORITY_WEIGHTS[issue.priority]
    if is_overdue(issue.due_date):
        score += OVERDUE_BONUS
    return score


def score_pr(pr: RawPR) -> float:
    age_days = (datetime.now(UTC) - pr.opened_at).days
    return age_days * (2 if pr.is_review_requested else 1)


def rank[ItemT](items: list[ItemT], score_of: Callable[[ItemT], float]) -> list[ItemT]:
    return sorted(items, key=score_of, reverse=True)
