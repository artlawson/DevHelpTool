from datetime import UTC, datetime

from app.core.models import Issue, PullRequest
from app.slack.digest import build_digest, build_standup_summary


def _issue(
    key: str, priority: str = "Medium", *, linked_pr: PullRequest | None = None
) -> Issue:
    return Issue(
        key=key,
        summary=f"Summary for {key}",
        priority=priority,
        status="Open",
        due_date=None,
        has_linked_pr=linked_pr is not None,
        linked_pr=linked_pr,
        priority_score=0.0,
    )


def _pr(number: int = 1) -> PullRequest:
    return PullRequest(
        repo="acme/widgets",
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/acme/widgets/pull/{number}",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_review_requested=False,
        is_authored_by_me=True,
        age_score=0.0,
    )


def test_high_priority_present_upcoming_is_current_sprint_lower_priority_only():
    high = [_issue("AL-1", "High")]
    due_soon = [_issue("AL-2")]
    sprint_lower = [_issue("AL-3")]
    sprint_remainder = [_issue("AL-4")]
    backlog = [_issue("AL-5")]

    digest = build_digest(high, due_soon, sprint_lower, sprint_remainder, backlog, [])

    assert digest.high_priority.heading == "High Priority"
    assert digest.high_priority.items == high
    assert digest.upcoming.heading == "Upcoming"
    assert digest.upcoming.items == sprint_lower


def test_high_priority_absent_due_soon_present_upcoming_is_deduped_remainder():
    due_soon = [_issue("AL-2")]
    sprint_remainder = [_issue("AL-4"), _issue("AL-2")]  # AL-2 also due soon
    backlog = [_issue("AL-5")]

    digest = build_digest([], due_soon, [], sprint_remainder, backlog, [])

    assert digest.high_priority.heading == "High Priority"
    assert digest.high_priority.items == due_soon
    upcoming_keys = {i.key for i in digest.upcoming.items}
    assert upcoming_keys == {"AL-4", "AL-5"}
    assert "AL-2" not in upcoming_keys


def test_nothing_high_priority_and_nothing_due_soon_shows_explicit_empty_state():
    digest = build_digest([], [], [], [_issue("AL-4")], [_issue("AL-5")], [])

    assert digest.high_priority.heading == "Nothing due soon"
    assert digest.high_priority.items == []
    upcoming_keys = {i.key for i in digest.upcoming.items}
    assert upcoming_keys == {"AL-4", "AL-5"}


def test_dedup_removes_issue_present_in_both_remainder_and_backlog_sources():
    duplicate = _issue("AL-9")
    sprint_remainder = [duplicate]
    backlog = [duplicate]

    digest = build_digest([], [], [], sprint_remainder, backlog, [])

    assert [i.key for i in digest.upcoming.items] == ["AL-9"]


def test_upcoming_is_genuinely_empty_when_both_sources_are_empty():
    digest = build_digest([], [], [], [], [], [])

    assert digest.upcoming.items == []


def test_prs_to_review_passed_through_regardless_of_branch():
    prs = [_pr(1)]

    digest = build_digest([_issue("AL-1", "High")], [], [], [], [], prs)

    assert digest.prs_to_review == prs


def test_standup_doing_is_filtered_to_high_and_highest_priority():
    high = _issue("AL-1", "Highest")
    medium = _issue("AL-2", "Medium")

    summary = build_standup_summary([high, medium], [])

    assert summary.doing == [high]


def test_standup_doing_issue_carries_its_linked_pr_for_blocked_rendering():
    pr = _pr(1)
    blocked = _issue("AL-1", "High", linked_pr=pr)

    summary = build_standup_summary([blocked], [])

    assert summary.doing[0].linked_pr == pr


def test_standup_next_up_backfills_when_doing_and_reviewing_are_thin():
    doing_issue = _issue("AL-1", "High")
    filler_a = _issue("AL-2", "Medium")
    filler_b = _issue("AL-3", "Low")

    summary = build_standup_summary([doing_issue, filler_a, filler_b], [])

    # doing (1) + reviewing (0) = 1, short of the 2-update minimum by 1
    assert summary.next_up == [filler_a]


def test_standup_next_up_stays_empty_once_doing_and_reviewing_reach_minimum():
    doing_issue = _issue("AL-1", "High")
    filler = _issue("AL-2", "Medium")

    summary = build_standup_summary([doing_issue, filler], [_pr(1)])

    assert summary.next_up == []


def test_standup_next_up_never_repeats_an_issue_already_in_doing():
    only_issue = _issue("AL-1", "High")

    summary = build_standup_summary([only_issue], [])

    assert only_issue not in summary.next_up


def test_standup_doing_is_not_capped_when_more_than_four_in_flight():
    highs = [_issue(f"AL-{i}", "High") for i in range(6)]

    summary = build_standup_summary(highs, [])

    assert len(summary.doing) == 6
