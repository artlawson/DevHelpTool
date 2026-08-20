from datetime import UTC, datetime
from unittest.mock import AsyncMock

import app.cli as cli_module
from app.agent.orchestrator import OrchestratorUnavailable
from app.core.models import AskResponse, Issue, PullRequest, ToolResult


def _issue(key: str, *, priority: str = "High") -> Issue:
    return Issue(
        key=key,
        summary=f"Summary for {key}",
        priority=priority,
        status="Open",
        due_date=None,
        has_linked_pr=False,
        priority_score=0.0,
    )


def _pr(number: int = 514) -> PullRequest:
    return PullRequest(
        repo="acme/widgets",
        number=number,
        title="Fix auth bug",
        url=f"https://github.com/acme/widgets/pull/{number}",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_review_requested=True,
        is_authored_by_me=False,
        age_score=0.0,
    )


async def test_main_prints_answer_and_returns_zero(monkeypatch, capsys):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(
            answer="you're all caught up",
            tool_calls=["jira_get_my_high_priority_issues"],
            warnings=[],
        )
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["what", "should", "I", "work", "on", "today?"])

    assert exit_code == 0
    mock_handle_query.assert_awaited_once_with("what should I work on today?")
    assert capsys.readouterr().out.strip() == "you're all caught up"


async def test_main_prints_warnings_to_stderr(monkeypatch, capsys):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(
            answer="answer",
            tool_calls=[],
            warnings=["github_get_my_open_prs: HTTPStatusError: request failed"],
        )
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["status?"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "answer"
    assert "github_get_my_open_prs" in captured.err


async def test_main_returns_one_on_orchestrator_unavailable(monkeypatch, capsys):
    mock_handle_query = AsyncMock(
        side_effect=OrchestratorUnavailable("connection reset")
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["status?"])

    assert exit_code == 1
    captured = capsys.readouterr()
    # The real exception message must never leak to the terminal.
    assert "connection reset" not in captured.err
    assert "unavailable" in captured.err


async def test_main_prompts_when_no_query_given(monkeypatch, capsys):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(answer="answer", tool_calls=[], warnings=[])
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "what should I work on today?"
    )

    exit_code = await cli_module.main([])

    assert exit_code == 0
    mock_handle_query.assert_awaited_once_with("what should I work on today?")


async def test_main_prints_blank_line_after_prompt_before_answer(monkeypatch, capsys):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(answer="*Focus: AL-12*", tool_calls=[], warnings=[])
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)
    monkeypatch.setattr("builtins.input", lambda prompt: "what should I work on?")

    exit_code = await cli_module.main([])

    assert exit_code == 0
    assert capsys.readouterr().out == "\n*Focus: AL-12*\n"


async def test_main_returns_two_on_blank_prompt_input(monkeypatch, capsys):
    mock_handle_query = AsyncMock()
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)
    monkeypatch.setattr("builtins.input", lambda prompt: "   ")

    exit_code = await cli_module.main([])

    assert exit_code == 2
    mock_handle_query.assert_not_awaited()
    assert "no query given" in capsys.readouterr().err


async def test_main_returns_two_on_whitespace_only_query_args(monkeypatch, capsys):
    mock_handle_query = AsyncMock()
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["   "])

    assert exit_code == 2
    mock_handle_query.assert_not_awaited()
    assert "no query given" in capsys.readouterr().err


async def test_main_returns_two_on_eof_at_prompt(monkeypatch, capsys):
    def _raise_eof(prompt: str) -> str:
        raise EOFError

    mock_handle_query = AsyncMock()
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)
    monkeypatch.setattr("builtins.input", _raise_eof)

    exit_code = await cli_module.main([])

    assert exit_code == 2
    mock_handle_query.assert_not_awaited()


async def test_main_standup_prints_doing_and_reviewing_sections(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=True, data=[_issue("AL-1")], error=None)),
    )
    monkeypatch.setattr(
        cli_module.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[_pr()], error=None)),
    )
    mock_handle_query = AsyncMock()
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["standup"])

    assert exit_code == 0
    mock_handle_query.assert_not_awaited()  # bypasses the LLM, same as the Slack button
    out = capsys.readouterr().out
    assert "Doing:" in out
    assert "AL-1" in out
    assert "Reviewing:" in out
    assert "acme/widgets#514" in out


async def test_main_standup_prints_next_up_when_doing_and_reviewing_are_thin(
    monkeypatch, capsys
):
    # One High (-> Doing) plus two Low issues; Doing+Reviewing totals 1, under
    # build_standup_summary's _MIN_UPDATES=2 threshold, so Next Up should
    # backfill from the Low issues, excluding the one already in Doing.
    in_flight = [
        _issue("AL-1", priority="High"),
        _issue("AL-2", priority="Low"),
        _issue("AL-3", priority="Low"),
    ]
    monkeypatch.setattr(
        cli_module.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=True, data=in_flight, error=None)),
    )
    monkeypatch.setattr(
        cli_module.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )

    exit_code = await cli_module.main(["standup"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Doing:" in out
    assert "AL-1" in out
    assert "Next Up:" in out
    assert "AL-2" in out
    assert "AL-3" not in out  # shortfall is 1, so only one backfilled item


async def test_main_standup_prints_empty_state_when_nothing_to_show(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli_module.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )
    monkeypatch.setattr(
        cli_module.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )

    exit_code = await cli_module.main(["standup"])

    assert exit_code == 0
    assert "Nothing high-priority" in capsys.readouterr().out


async def test_main_standup_degrades_gracefully_on_tool_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=False, data=None, error="boom")),
    )
    monkeypatch.setattr(
        cli_module.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )

    exit_code = await cli_module.main(["standup"])

    assert exit_code == 0
    assert "Nothing high-priority" in capsys.readouterr().out


async def test_main_treats_standup_with_extra_words_as_a_regular_query(monkeypatch):
    mock_handle_query = AsyncMock(
        return_value=AskResponse(answer="answer", tool_calls=[], warnings=[])
    )
    monkeypatch.setattr(cli_module, "handle_query", mock_handle_query)

    exit_code = await cli_module.main(["standup", "please"])

    assert exit_code == 0
    mock_handle_query.assert_awaited_once_with("standup please")


# --- terminal hyperlinking ---------------------------------------------------


def test_hyperlink_prs_wraps_known_pr_mentions_in_osc8():
    text = "Please check PR #514 when you get a chance."
    pr = _pr(number=514)

    result = cli_module._hyperlink_prs(text, [pr])

    assert cli_module._terminal_link(pr.url, "PR #514") in result


def test_hyperlink_prs_leaves_unknown_pr_mentions_unchanged():
    text = "PR #999 isn't one we fetched this turn."

    result = cli_module._hyperlink_prs(text, [])

    assert result == text


def test_hyperlink_issues_links_key_and_title_on_first_mention_only():
    issue = _issue("AL-12")
    text = "Start with AL-12. AL-12 is your only high-priority ticket."

    result = cli_module._hyperlink_issues(text, [issue])

    url = cli_module.jira_issue_url("AL-12")
    key_link = cli_module._terminal_link(url, "AL-12")
    title_link = cli_module._terminal_link(url, issue.summary)
    assert result.count(f"{key_link} {title_link}") == 1
    # Second mention: bare key only, no title repeated.
    assert result.count(key_link) == 2


def test_hyperlink_issues_does_not_corrupt_a_pr_link_with_a_lookalike_key_in_its_url():
    # The PR's URL happens to contain an issue-key-shaped substring (e.g. a
    # "?ref=AL-1" query param) - _hyperlink_prs' OSC 8 span embeds that raw
    # URL text verbatim, so running _hyperlink_issues afterward must not
    # reach inside that span and re-wrap the embedded "AL-1".
    issue = _issue("AL-1")
    pr = PullRequest(
        repo="acme/widgets",
        number=1,
        title="Fix auth bug",
        url="https://github.com/acme/widgets/pull/1?ref=AL-1",
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_review_requested=True,
        is_authored_by_me=False,
        age_score=0.0,
    )
    text = "See PR #1."

    text = cli_module._hyperlink_prs(text, [pr])
    linked_pr_span = cli_module._terminal_link(pr.url, "PR #1")
    assert linked_pr_span in text

    text = cli_module._hyperlink_issues(text, [issue])

    # The PR's OSC 8 span, "AL-1"-shaped URL text and all, must survive
    # completely intact - if the issue pass had reached inside it, this
    # exact span wouldn't still be present.
    assert linked_pr_span in text


async def test_main_hyperlinks_issues_and_prs_when_stdout_is_a_tty(
    monkeypatch, capsys
):
    issue = _issue("AL-12")
    pr = _pr(number=514)
    monkeypatch.setattr(
        cli_module,
        "handle_query",
        AsyncMock(
            return_value=AskResponse(
                answer="Focus on AL-12; PR #514 is also waiting.",
                tool_calls=[],
                warnings=[],
                referenced_issues=[issue],
                referenced_prs=[pr],
            )
        ),
    )
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)

    exit_code = await cli_module.main(["what", "next?"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "\033]8;;" in out
    assert cli_module.jira_issue_url("AL-12") in out
    assert pr.url in out


async def test_main_does_not_hyperlink_when_stdout_is_not_a_tty(monkeypatch, capsys):
    issue = _issue("AL-12")
    monkeypatch.setattr(
        cli_module,
        "handle_query",
        AsyncMock(
            return_value=AskResponse(
                answer="Focus on AL-12.",
                tool_calls=[],
                warnings=[],
                referenced_issues=[issue],
                referenced_prs=[],
            )
        ),
    )
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: False)

    exit_code = await cli_module.main(["what", "next?"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.strip() == "Focus on AL-12."
    assert "\033]8;;" not in out


async def test_main_standup_hyperlinks_when_stdout_is_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.jira_tools,
        "get_my_issues_with_linked_prs",
        AsyncMock(return_value=ToolResult(ok=True, data=[_issue("AL-1")], error=None)),
    )
    monkeypatch.setattr(
        cli_module.github_tools,
        "get_prs_awaiting_my_review",
        AsyncMock(return_value=ToolResult(ok=True, data=[], error=None)),
    )
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)

    exit_code = await cli_module.main(["standup"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "\033]8;;" in out
    assert cli_module.jira_issue_url("AL-1") in out
