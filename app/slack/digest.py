from dataclasses import dataclass, field

from app.core.models import Issue, PullRequest
from app.core.ranking import is_urgent


@dataclass
class DigestSection:
    heading: str
    items: list[Issue]


@dataclass
class Digest:
    high_priority: DigestSection
    upcoming: DigestSection
    prs_to_review: list[PullRequest] = field(default_factory=list)


@dataclass
class StandupSummary:
    doing: list[Issue]
    reviewing: list[PullRequest]
    next_up: list[Issue] = field(default_factory=list)


_MIN_UPDATES = 2


# "Doing" is priority-filtered from an already fully-ranked, PR-annotated list
# (jira_tools.get_my_issues_with_linked_prs) rather than the plain
# get_my_high_priority_issues() this used to call - so any Doing issue that has
# a linked PR still open (i.e. blocking further progress on it) surfaces that
# PR inline for free via _issue_bullet's existing linked_pr rendering, no
# separate "blocked" concept needed.
# "Reviewing" (PRs awaiting review) is unchanged.
# "Next up" only exists to keep the summary from being a single bare bullet:
# if Doing + Reviewing together are thin (<2), backfill from the same ranked
# in_flight_issues list (excluding whatever's already in Doing) as suggestions
# for what to pick up next. Deliberately uncapped on the high end - if there
# are genuinely more than a handful of in-flight issues, showing all of them
# is more honest than truncating to a fixed count.
def build_standup_summary(
    in_flight_issues: list[Issue], prs_awaiting_review: list[PullRequest]
) -> StandupSummary:
    # is_urgent (High/Highest priority, or overdue regardless of priority) -
    # the same predicate _priority_emoji uses for its red tier, so "Doing"
    # matches exactly what would render red, not a separately-drifting check.
    doing = [i for i in in_flight_issues if is_urgent(i.priority, i.due_date)]
    reviewing = prs_awaiting_review

    next_up: list[Issue] = []
    shortfall = _MIN_UPDATES - (len(doing) + len(reviewing))
    if shortfall > 0:
        doing_keys = {i.key for i in doing}
        next_up = [i for i in in_flight_issues if i.key not in doing_keys][:shortfall]

    return StandupSummary(doing=doing, reviewing=reviewing, next_up=next_up)


def _dedupe_by_key(
    issues: list[Issue], exclude_keys: set[str] | None = None
) -> list[Issue]:
    exclude_keys = exclude_keys or set()
    seen: dict[str, Issue] = {}
    for issue in issues:
        if issue.key in exclude_keys or issue.key in seen:
            continue
        seen[issue.key] = issue
    return list(seen.values())


def build_digest(
    high_priority_issues: list[Issue],
    due_soon_lower_priority: list[Issue],
    current_sprint_lower_priority: list[Issue],
    current_sprint_remainder: list[Issue],
    backlog_needing_details: list[Issue],
    prs_i_could_review: list[PullRequest],
) -> Digest:
    if high_priority_issues:
        return Digest(
            high_priority=DigestSection("High Priority", high_priority_issues),
            upcoming=DigestSection("Upcoming", current_sprint_lower_priority),
            prs_to_review=prs_i_could_review,
        )

    if due_soon_lower_priority:
        upcoming = _dedupe_by_key(
            current_sprint_remainder + backlog_needing_details,
            exclude_keys={i.key for i in due_soon_lower_priority},
        )
        return Digest(
            high_priority=DigestSection("High Priority", due_soon_lower_priority),
            upcoming=DigestSection("Upcoming", upcoming),
            prs_to_review=prs_i_could_review,
        )

    upcoming = _dedupe_by_key(current_sprint_remainder + backlog_needing_details)
    return Digest(
        high_priority=DigestSection("Nothing due soon", []),
        upcoming=DigestSection("Upcoming", upcoming),
        prs_to_review=prs_i_could_review,
    )
