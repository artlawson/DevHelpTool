from datetime import UTC, date, datetime, timedelta

from app.core.ranking import RawIssue, RawPR, rank, score_issue, score_pr


def test_overdue_highest_priority_scores_higher_than_non_overdue_high():
    overdue_highest = RawIssue(priority="Highest", due_date=date(2020, 1, 1))
    non_overdue_high = RawIssue(priority="High", due_date=date(2999, 1, 1))

    assert score_issue(overdue_highest) > score_issue(non_overdue_high)


def test_priority_ordering_is_monotonic_holding_due_date_constant():
    scores = [
        score_issue(RawIssue(priority=p, due_date=None))
        for p in ("Highest", "High", "Medium", "Low", "Lowest")
    ]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)  # strictly decreasing, no ties


def test_review_requested_pr_scores_higher_than_authored_pr_same_age():
    opened_at = datetime.now(UTC) - timedelta(days=5)
    review_requested = RawPR(opened_at=opened_at, is_review_requested=True)
    authored = RawPR(opened_at=opened_at, is_review_requested=False)

    assert score_pr(review_requested) > score_pr(authored)


def test_pr_score_increases_monotonically_with_age():
    now = datetime.now(UTC)
    younger = RawPR(opened_at=now - timedelta(days=1), is_review_requested=False)
    older = RawPR(opened_at=now - timedelta(days=10), is_review_requested=False)

    assert score_pr(older) > score_pr(younger)


def test_rank_sorts_descending_by_score():
    issues = [
        RawIssue(priority="Low", due_date=None),
        RawIssue(priority="Highest", due_date=None),
        RawIssue(priority="Medium", due_date=None),
    ]

    ranked = rank(issues, score_issue)

    assert [i.priority for i in ranked] == ["Highest", "Medium", "Low"]
