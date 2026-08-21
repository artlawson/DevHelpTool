import json
from datetime import UTC, date, datetime

from app.core.models import AskResponse, CommentDraft, Issue, PullRequest
from app.slack.digest import Digest, DigestSection, StandupSummary
from app.slack.formatting import (
    format_ask_response,
    format_digest,
    format_standup_summary,
    replace_comment_draft_blocks,
)


def _issue(
    key: str,
    *,
    due_date: date | None = None,
    priority: str = "Medium",
    linked_pr: PullRequest | None = None,
) -> Issue:
    return Issue(
        key=key,
        summary=f"Summary for {key}",
        priority=priority,
        status="Open",
        due_date=due_date,
        has_linked_pr=linked_pr is not None,
        linked_pr=linked_pr,
        priority_score=0.0,
    )


def _pr(
    number: int = 514, url: str = "https://github.com/acme/widgets/pull/514"
) -> PullRequest:
    return PullRequest(
        repo="acme/widgets",
        number=number,
        title="Fix auth bug",
        url=url,
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_review_requested=False,
        is_authored_by_me=True,
        age_score=0.0,
    )


# format_digest always ends with a divider + "*Summary*" block now, so tests
# that want "whatever block holds the Upcoming/PRs-to-review section" need to
# find it by heading prefix rather than assume it's the last block.
def _section_texts(blocks: list[dict]) -> list[str]:
    return [b["text"]["text"] for b in blocks if b["type"] == "section"]


def _find_section_text(blocks: list[dict], heading_prefix: str) -> str:
    for text in _section_texts(blocks):
        if text.startswith(heading_prefix):
            return text
    raise AssertionError(f"no section found starting with {heading_prefix!r}")


def test_nothing_due_soon_heading_renders_as_lone_heading_block():
    digest = Digest(
        high_priority=DigestSection("Nothing due soon", []),
        upcoming=DigestSection("Upcoming", [_issue("AL-1")]),
    )

    blocks = format_digest(digest)

    assert blocks[0]["text"]["text"] == "*Nothing due soon*"
    assert "•" not in blocks[0]["text"]["text"]


def test_empty_upcoming_section_renders_explicit_fallback_as_last_block():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-1")]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    upcoming_text = _find_section_text(blocks, "*Upcoming*")
    assert "Nothing else on deck" in upcoming_text


def test_high_priority_issue_gets_its_own_section_and_actions_block():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-4")]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    # [0] heading, [1] issue text, [2] issue actions, [3] upcoming fallback
    assert blocks[0]["text"]["text"] == "*High Priority*"
    issue_text = blocks[1]["text"]["text"]
    assert "<" in issue_text and "/browse/AL-4|AL-4>" in issue_text
    assert "Summary for AL-4" in issue_text
    assert blocks[2]["type"] == "actions"


def test_issue_actions_block_has_open_in_jira_button():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-4")]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    actions = blocks[2]["elements"]
    assert len(actions) == 1
    assert actions[0]["action_id"] == "digest_open_jira"
    assert actions[0]["url"].endswith("/browse/AL-4")


def test_issue_actions_block_adds_view_pr_button_when_linked():
    pr = _pr(number=514)
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-9", linked_pr=pr)]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    actions = blocks[2]["elements"]
    assert len(actions) == 2
    pr_button = actions[1]
    assert pr_button["action_id"] == "digest_view_pr"
    assert pr_button["url"] == pr.url
    assert "514" in pr_button["text"]["text"]


def test_issue_actions_block_has_no_pr_button_when_unlinked():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-4")]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    action_ids = [el["action_id"] for el in blocks[2]["elements"]]
    assert "digest_view_pr" not in action_ids


def test_overdue_issue_bullet_includes_overdue_indicator():
    overdue_issue = _issue("AL-5", due_date=date(2020, 1, 1))
    digest = Digest(
        high_priority=DigestSection("High Priority", [overdue_issue]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    assert "(overdue)" in blocks[1]["text"]["text"]


def test_non_overdue_issue_bullet_has_no_overdue_indicator():
    future_issue = _issue("AL-6", due_date=date(2099, 1, 1))
    digest = Digest(
        high_priority=DigestSection("High Priority", [future_issue]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    assert "(overdue)" not in blocks[1]["text"]["text"]


def test_non_overdue_issue_bullet_shows_due_date():
    future_issue = _issue("AL-6", due_date=date(2099, 1, 15))
    digest = Digest(
        high_priority=DigestSection("High Priority", [future_issue]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    assert "due Jan 15" in blocks[1]["text"]["text"]


def test_high_priority_items_are_numbered_in_rank_order():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-1"), _issue("AL-2")]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    # [0] heading, [1]/[2] issue1 text+actions, [3]/[4] issue2 text+actions
    assert blocks[1]["text"]["text"].startswith("1. ")
    assert blocks[3]["text"]["text"].startswith("2. ")


def test_upcoming_items_use_priority_emoji_as_the_bullet_not_a_number():
    # Unnumbered lists use the priority emoji itself as the bullet - no
    # separate "•" alongside it (that would double-mark the same line).
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", [_issue("AL-3", priority="Medium")]),
    )

    blocks = format_digest(digest)

    text = _find_section_text(blocks, "*Upcoming*")
    assert text.startswith("*Upcoming*\n🟡 ")
    assert "•" not in text
    assert "1. " not in text


def test_high_priority_section_is_always_red_regardless_of_actual_priority():
    # Membership in "High Priority" can come from the due-soon fallback path
    # (build_digest), not just an actual High/Highest Jira priority - so this
    # section is forced red rather than colored per-item.
    low_priority_but_due_soon = _issue("AL-7", priority="Low")
    digest = Digest(
        high_priority=DigestSection("High Priority", [low_priority_but_due_soon]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    assert "🔴" in blocks[1]["text"]["text"]


def test_upcoming_section_colors_by_actual_priority_tier():
    high = _issue("AL-8", priority="High")
    medium = _issue("AL-9", priority="Medium")
    low = _issue("AL-10", priority="Low")
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", [high, medium, low]),
    )

    blocks = format_digest(digest)

    text = _find_section_text(blocks, "*Upcoming*")
    assert "🔴" in text
    assert "🟡" in text
    assert "🟢" in text


def test_upcoming_section_unknown_priority_falls_back_to_green():
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", [_issue("AL-11", priority="P0")]),
    )

    blocks = format_digest(digest)

    assert "🟢" in _find_section_text(blocks, "*Upcoming*")


def test_upcoming_section_overdue_medium_priority_is_still_red():
    overdue_medium = _issue("AL-12", priority="Medium", due_date=date(2020, 1, 1))
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", [overdue_medium]),
    )

    blocks = format_digest(digest)

    assert "🔴" in _find_section_text(blocks, "*Upcoming*")


def test_issue_bullet_includes_linked_pr_link():
    pr = _pr(number=514)
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-9", linked_pr=pr)]),
        upcoming=DigestSection("Upcoming", []),
    )

    blocks = format_digest(digest)

    text = blocks[1]["text"]["text"]
    assert "<https://github.com/acme/widgets/pull/514|PR #514>" in text


def test_digest_omits_prs_to_review_section_when_empty():
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", []),
        prs_to_review=[],
    )

    blocks = format_digest(digest)

    assert not any("PRs You Could Review" in text for text in _section_texts(blocks))


def test_digest_lists_prs_to_review_one_per_line():
    pr_a = _pr(number=1, url="https://github.com/acme/widgets/pull/1")
    pr_b = _pr(number=2, url="https://github.com/acme/widgets/pull/2")
    digest = Digest(
        high_priority=DigestSection("High Priority", []),
        upcoming=DigestSection("Upcoming", []),
        prs_to_review=[pr_a, pr_b],
    )

    blocks = format_digest(digest)

    text = _find_section_text(blocks, "*PRs You Could Review*")
    lines = text.splitlines()[1:]
    assert len(lines) == 2
    assert "pull/1" in lines[0]
    assert "pull/2" in lines[1]


def test_digest_summary_names_top_priority_issue_and_counts_the_rest():
    top = _issue("AL-1", priority="Highest")
    other = _issue("AL-2", priority="Highest")
    digest = Digest(
        high_priority=DigestSection("High Priority", [top, other]),
        upcoming=DigestSection("Upcoming", [_issue("AL-3")]),
        prs_to_review=[_pr(number=9)],
    )

    blocks = format_digest(digest)

    text = _find_section_text(blocks, "*Summary*")
    assert "/browse/AL-1|AL-1>" in text
    assert "plus 1 more high-priority item" in text
    assert "1 upcoming" in text
    assert "1 PR to review" in text


def test_digest_summary_has_no_high_priority_fallback_text():
    digest = Digest(
        high_priority=DigestSection("Nothing due soon", []),
        upcoming=DigestSection("Upcoming", []),
        prs_to_review=[],
    )

    blocks = format_digest(digest)

    text = _find_section_text(blocks, "*Summary*")
    assert text == "*Summary*\nNothing high-priority or due soon right now."


def test_digest_summary_is_the_last_block():
    digest = Digest(
        high_priority=DigestSection("High Priority", [_issue("AL-1")]),
        upcoming=DigestSection("Upcoming", []),
        prs_to_review=[],
    )

    blocks = format_digest(digest)

    assert blocks[-1]["text"]["text"].startswith("*Summary*")
    assert blocks[-2]["type"] == "divider"


def test_format_standup_summary_shows_next_up_section():
    summary = StandupSummary(
        doing=[_issue("AL-1")], reviewing=[], next_up=[_issue("AL-2")]
    )

    blocks = format_standup_summary(summary)

    texts = [b["text"]["text"] for b in blocks]
    assert any(t.startswith("*Next Up*") for t in texts)


def test_format_ask_response_leads_with_the_answer():
    response = AskResponse(
        answer="You have 2 high priority tickets.", tool_calls=[], warnings=[]
    )

    blocks = format_ask_response(response)

    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == "You have 2 high priority tickets."


def test_format_ask_response_always_offers_standup_followup_buttons():
    response = AskResponse(answer="Some unrelated answer.", tool_calls=[], warnings=[])

    blocks = format_ask_response(response)

    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(actions_blocks) == 1
    action_ids = {el["action_id"] for el in actions_blocks[0]["elements"]}
    assert action_ids == {"ask_standup_dismiss", "ask_standup_summary"}


def test_format_ask_response_converts_double_asterisk_bold_to_slack_bold():
    response = AskResponse(answer="**Focus:** fix the bug", tool_calls=[], warnings=[])

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert text == "*Focus:* fix the bug"
    assert "**" not in text


def test_format_ask_response_converts_markdown_headers_to_bold():
    response = AskResponse(
        answer="## High priority\nSome detail", tool_calls=[], warnings=[]
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "*High priority*" in text
    assert "#" not in text


def test_format_ask_response_converts_markdown_bullets_to_slack_bullets():
    response = AskResponse(
        answer="- first item\n- second item", tool_calls=[], warnings=[]
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "• first item" in text
    assert "• second item" in text
    assert "- " not in text


def test_format_ask_response_converts_markdown_links_to_slack_links():
    response = AskResponse(
        answer="see [the PR](https://github.com/acme/widgets/pull/9)",
        tool_calls=[],
        warnings=[],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "<https://github.com/acme/widgets/pull/9|the PR>" in text
    assert "[" not in text


def test_format_ask_response_drops_bullet_when_line_starts_with_priority_emoji():
    # Claude writes its own "•" bullet per the system prompt; the priority-
    # emoji insertion then lands right after it - the "•" must be dropped so
    # the emoji is the only bullet, not a double marker.
    issue = _issue("AL-12", priority="Highest")
    response = AskResponse(
        answer="*High-priority issues:*\n• AL-12 — needs attention",
        tool_calls=[],
        warnings=[],
        referenced_issues=[issue],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "• 🔴" not in text
    assert "\n🔴 <" in text


def test_format_ask_response_leaves_bullet_alone_when_emoji_is_not_first():
    issue = _issue("AL-12", priority="Highest")
    response = AskResponse(
        answer="• Also check AL-12 later",
        tool_calls=[],
        warnings=[],
        referenced_issues=[issue],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert text.startswith("• Also check")


def test_format_ask_response_first_mention_links_key_and_title():
    issue = _issue("AL-12", priority="Highest")
    response = AskResponse(
        answer="Focus on AL-12 today.",
        tool_calls=[],
        warnings=[],
        referenced_issues=[issue],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "🔴 <" in text
    assert "/browse/AL-12|AL-12>" in text
    assert "/browse/AL-12|Summary for AL-12>" in text


def test_format_ask_response_subsequent_mention_is_bare_key_only():
    issue = _issue("AL-12", priority="Highest")
    response = AskResponse(
        answer="AL-12 is urgent. Please prioritize AL-12 today.",
        tool_calls=[],
        warnings=[],
        referenced_issues=[issue],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    # First mention: emoji + key link + title link. Second: bare key link only.
    assert text.count("🔴") == 1
    assert text.count("Summary for AL-12") == 1
    assert text.count("/browse/AL-12|AL-12>") == 2


def test_format_ask_response_leaves_unknown_issue_key_as_plain_text():
    response = AskResponse(
        answer="AL-99 was mentioned but never fetched.",
        tool_calls=[],
        warnings=[],
        referenced_issues=[],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert text.startswith("AL-99 was mentioned")
    assert "<" not in text


def test_format_ask_response_does_not_mangle_issue_key_inside_existing_link():
    # A converted markdown link's URL can itself contain an issue-key-shaped
    # substring (e.g. ".../browse/AL-12") - the issue-hyperlinking pass must
    # not match inside it and produce nested/broken markup.
    issue = _issue("AL-12", priority="High")
    response = AskResponse(
        answer="See [the ticket](https://example.com/AL-12) for more.",
        tool_calls=[],
        warnings=[],
        referenced_issues=[issue],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert text == "See <https://example.com/AL-12|the ticket> for more."


def test_format_ask_response_hyperlinks_referenced_pr():
    pr = _pr(number=514, url="https://github.com/acme/widgets/pull/514")
    response = AskResponse(
        answer="PR #514 is waiting on you.",
        tool_calls=[],
        warnings=[],
        referenced_prs=[pr],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "<https://github.com/acme/widgets/pull/514|PR #514>" in text


def test_format_ask_response_leaves_unknown_pr_mention_as_plain_text():
    response = AskResponse(
        answer="PR #999 was mentioned but never fetched.",
        tool_calls=[],
        warnings=[],
        referenced_prs=[],
    )

    blocks = format_ask_response(response)

    text = blocks[0]["text"]["text"]
    assert "PR #999" in text
    assert "<" not in text


def test_format_standup_summary_shows_doing_and_reviewing_sections():
    summary = StandupSummary(doing=[_issue("AL-1")], reviewing=[_pr()])

    blocks = format_standup_summary(summary)

    texts = [b["text"]["text"] for b in blocks]
    assert any(t.startswith("*Doing*") for t in texts)
    assert any(t.startswith("*Reviewing*") for t in texts)


def test_format_standup_summary_omits_empty_sections():
    summary = StandupSummary(doing=[_issue("AL-1")], reviewing=[])

    blocks = format_standup_summary(summary)

    texts = [b["text"]["text"] for b in blocks]
    assert not any(t.startswith("*Reviewing*") for t in texts)


def test_format_standup_summary_has_explicit_empty_state():
    summary = StandupSummary(doing=[], reviewing=[])

    blocks = format_standup_summary(summary)

    assert len(blocks) == 1


def test_format_ask_response_includes_comment_draft_blocks_when_pending():
    draft = CommentDraft(issue_key="AL-13", note_text="quick thought here")
    response = AskResponse(
        answer="Drafted a note for AL-13.",
        tool_calls=["jira_draft_comment"],
        warnings=[],
        pending_comment_draft=draft,
    )

    blocks = format_ask_response(response)

    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(actions_blocks) == 2
    draft_actions = actions_blocks[0]
    action_ids = {el["action_id"] for el in draft_actions["elements"]}
    assert action_ids == {"ask_confirm_comment", "ask_discard_comment_draft"}

    section_texts = [
        b["text"]["text"] for b in blocks if b["type"] == "section"
    ]
    assert any("AL-13" in t and "quick thought here" in t for t in section_texts)


def test_format_ask_response_comment_draft_button_value_round_trips():
    draft = CommentDraft(issue_key="AL-13", note_text="quick thought here")
    response = AskResponse(
        answer="Drafted a note for AL-13.",
        tool_calls=["jira_draft_comment"],
        warnings=[],
        pending_comment_draft=draft,
    )

    blocks = format_ask_response(response)

    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    confirm_button = next(
        el
        for el in actions_blocks[0]["elements"]
        if el["action_id"] == "ask_confirm_comment"
    )
    payload = json.loads(confirm_button["value"])
    assert payload == {"issue_key": "AL-13", "note_text": "quick thought here"}


def test_replace_comment_draft_blocks_preserves_answer_and_standup_blocks():
    draft = CommentDraft(issue_key="AL-13", note_text="quick thought here")
    response = AskResponse(
        answer="You have 1 high priority issue.",
        tool_calls=[],
        warnings=[],
        pending_comment_draft=draft,
    )
    original_blocks = format_ask_response(response)

    new_blocks = replace_comment_draft_blocks(
        original_blocks, "Comment posted to AL-13."
    )

    assert new_blocks[0]["text"]["text"] == "You have 1 high priority issue."
    assert not any(
        b.get("block_id", "").startswith("comment_draft") for b in new_blocks
    )
    outcome_texts = [
        b["text"]["text"] for b in new_blocks if b["type"] == "section"
    ]
    assert "Comment posted to AL-13." in outcome_texts
    # The standup-followup prompt/buttons (unrelated to the draft) must
    # survive - a bare replace_original with only `text=` would have wiped
    # the whole original message, not just the draft's 3 blocks.
    actions_blocks = [b for b in new_blocks if b["type"] == "actions"]
    assert len(actions_blocks) == 1
    action_ids = {el["action_id"] for el in actions_blocks[0]["elements"]}
    assert action_ids == {"ask_standup_dismiss", "ask_standup_summary"}


def test_replace_comment_draft_blocks_is_a_no_op_when_no_draft_blocks_present():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "plain answer"}}]

    result = replace_comment_draft_blocks(blocks, "irrelevant")

    assert result == blocks


def test_format_ask_response_omits_comment_draft_blocks_when_not_pending():
    response = AskResponse(answer="No draft here.", tool_calls=[], warnings=[])

    blocks = format_ask_response(response)

    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(actions_blocks) == 1
    action_ids = {el["action_id"] for el in actions_blocks[0]["elements"]}
    assert "ask_confirm_comment" not in action_ids
