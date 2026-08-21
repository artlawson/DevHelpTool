import argparse
import asyncio
import re
import sys

from app.agent.orchestrator import (
    OrchestratorUnavailable,
    handle_conversational_query,
    handle_query,
)
from app.core.errors import unwrap_tool_result
from app.core.models import AskResponse, CommentDraft, Issue, PullRequest
from app.core.ranking import priority_emoji
from app.core.text import apply_outside_links, jira_issue_url
from app.slack.digest import StandupSummary, build_standup_summary
from app.tools import github_tools, jira_tools


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="devhelp",
        description="Ask the engineering-productivity agent a question.",
    )
    parser.add_argument(
        "query",
        nargs="*",
        help='the question to ask, e.g. "what should I work on today?" - quote it to '
        "avoid the shell interpreting ? or * as a glob; omit it entirely to be "
        'prompted instead, which sidesteps shell quoting altogether. Pass "standup" '
        "alone to print a Doing/Reviewing/Next Up summary instead (no LLM call).",
    )
    return parser.parse_args(argv)


# OSC 8 is the terminal-emulator equivalent of Slack's <url|text> markup -
# supported by most modern terminals (iTerm2, Terminal.app, kitty, Windows
# Terminal, VS Code's integrated terminal), which render `text` as a clickable
# link to `url`; unsupported terminals just print the escape codes as inert
# bytes around plain text, so this is safe to emit unconditionally as long as
# callers only do so when sys.stdout.isatty() - see the `hyperlink` parameter
# threaded through everything below.
def _terminal_link(url: str, text: str) -> str:
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


_TERMINAL_LINK_PATTERN = re.compile(r"\033\]8;;.*?\033\\.*?\033\]8;;\033\\", re.DOTALL)
_ISSUE_KEY_PATTERN = re.compile(r"[A-Z]+-\d+")
_PR_MENTION_PATTERN = re.compile(r"PR\s*#(\d+)", re.IGNORECASE)


def _hyperlink_prs(text: str, referenced_prs: list[PullRequest]) -> str:
    by_number = {pr.number: pr for pr in referenced_prs}

    def replace(match: re.Match[str]) -> str:
        pr = by_number.get(int(match.group(1)))
        if pr is None:
            return match.group(0)
        return _terminal_link(pr.url, f"PR #{pr.number}")

    return apply_outside_links(
        text, _TERMINAL_LINK_PATTERN, lambda s: _PR_MENTION_PATTERN.sub(replace, s)
    )


# Mirrors app/slack/formatting.py:_hyperlink_and_flag_issues' first-mention
# behavior (key + title both linked on first mention, bare key on every mention
# after) but without the priority-emoji flagging that function also does -
# that's Slack-specific compensation for Block Kit having no other way to
# signal priority inline; a terminal reader already sees the answer as
# ordinary prose, so there's no equivalent gap to fill here.
def _hyperlink_issues(text: str, referenced_issues: list[Issue]) -> str:
    by_key = {issue.key: issue for issue in referenced_issues}
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(0)
        issue = by_key.get(key)
        if issue is None:
            return key
        url = jira_issue_url(key)
        if key in seen:
            return _terminal_link(url, key)
        seen.add(key)
        return f"{_terminal_link(url, key)} {_terminal_link(url, issue.summary)}"

    return apply_outside_links(
        text, _TERMINAL_LINK_PATTERN, lambda s: _ISSUE_KEY_PATTERN.sub(replace, s)
    )


def _issue_line(issue: Issue, *, hyperlink: bool) -> str:
    emoji = priority_emoji(issue.priority, issue.due_date)
    key_text = (
        _terminal_link(jira_issue_url(issue.key), issue.key)
        if hyperlink
        else issue.key
    )
    line = f"{emoji} {key_text} — {issue.summary}"
    if issue.linked_pr is not None:
        pr = issue.linked_pr
        pr_text = f"PR #{pr.number}"
        if hyperlink:
            pr_text = _terminal_link(pr.url, pr_text)
        line += f" ({pr_text})"
    return line


def _pr_line(pr: PullRequest, *, hyperlink: bool) -> str:
    label = f"{pr.repo}#{pr.number}"
    if hyperlink:
        label = _terminal_link(pr.url, label)
    return f"• {label} — {pr.title}"


def _print_standup_summary(summary: StandupSummary, *, hyperlink: bool) -> None:
    if not summary.doing and not summary.reviewing and not summary.next_up:
        print("Nothing high-priority or awaiting your review right now.")
        return
    if summary.doing:
        print("Doing:")
        for issue in summary.doing:
            print(f"  {_issue_line(issue, hyperlink=hyperlink)}")
    if summary.reviewing:
        print("Reviewing:")
        for pr in summary.reviewing:
            print(f"  {_pr_line(pr, hyperlink=hyperlink)}")
    if summary.next_up:
        print("Next Up:")
        for issue in summary.next_up:
            print(f"  {_issue_line(issue, hyperlink=hyperlink)}")


# Prints the drafted note, asks for explicit confirmation, and only then
# calls jira_tools.post_comment() - the actual Jira write. EOFError is
# treated as "declined," not as the query prompt's blank-input case, since a
# missing confirmation for an already-successful answer isn't a usage error
# (exit code stays 0 either way, unlike the query prompt's EOFError -> 2).
async def _confirm_and_post_comment(draft: CommentDraft) -> None:
    print()
    print(f"Draft comment for {draft.issue_key}:")
    print(f'  "{draft.note_text}"')
    try:
        raw_answer = await asyncio.to_thread(input, "Post this to Jira? [y/N]: ")
        answer = raw_answer.strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Comment not posted.")
        return
    result = await jira_tools.post_comment(draft.issue_key, draft.note_text)
    if result.ok:
        print(f"Comment posted to {draft.issue_key}.")
    else:
        print(f"warning: could not post comment: {result.error}", file=sys.stderr)


# Mirrors app/slack/bolt_app.py:handle_standup_summary_request - bypasses the
# LLM entirely and calls the same two tools directly, same as the Slack button.
async def _run_standup(*, hyperlink: bool) -> int:
    in_flight_issues = unwrap_tool_result(
        "get_my_issues_with_linked_prs",
        await jira_tools.get_my_issues_with_linked_prs(),
    )
    prs_awaiting_review = unwrap_tool_result(
        "get_prs_awaiting_my_review", await github_tools.get_prs_awaiting_my_review()
    )
    summary = build_standup_summary(
        in_flight_issues=in_flight_issues, prs_awaiting_review=prs_awaiting_review
    )
    _print_standup_summary(summary, hyperlink=hyperlink)
    return 0


# Shared by the single-shot (direct-args) and persistent-loop (no-args)
# paths, so answer/warning/comment-draft-confirm handling can't drift between
# them - folding the draft-confirm step in here too (not left as a separate
# call each caller has to remember) is what keeps that guarantee complete.
async def _render_answer(response: AskResponse, *, hyperlink: bool) -> None:
    answer = response.answer
    if hyperlink:
        answer = _hyperlink_prs(answer, response.referenced_prs)
        answer = _hyperlink_issues(answer, response.referenced_issues)
    print(answer)
    for warning in response.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if response.pending_comment_draft is not None:
        await _confirm_and_post_comment(response.pending_comment_draft)


_SESSION_END_WORDS = {"exit", "quit"}


# Only reached from main() when devhelp is run with no arguments at all -
# `devhelp "some query"` stays single-shot (see main()) so a script piping in
# one query never hangs waiting for more input. Session-scoped `history` is
# plain in-memory Python state (mirrors app/slack/bolt_app.py's
# _thread_histories) - lost once the process exits, by design ("until it is
# closed").
async def _prompt_loop(*, hyperlink: bool) -> int:
    history: list[dict] | None = None
    while True:
        try:
            raw_query = await asyncio.to_thread(input, "Ask: ")
        except EOFError:
            return 0
        query = raw_query.strip()
        if not query or query.lower() in _SESSION_END_WORDS:
            return 0
        print()  # blank line between the "Ask: ..." prompt and the answer

        try:
            response, history = await handle_conversational_query(query, history)
        except OrchestratorUnavailable:
            # A transient backend failure shouldn't end an otherwise-good
            # session - only blank/EOF/exit/quit do that (see loop exits
            # above), so prompt again rather than returning.
            print("assistant temporarily unavailable", file=sys.stderr)
            continue

        await _render_answer(response, hyperlink=hyperlink)


async def main(argv: list[str]) -> int:
    # Gated on stdout, not stdin: hyperlinking is purely an output-rendering
    # choice, and must stay off when stdout is piped/redirected (e.g. `devhelp
    # standup > log.txt` or `devhelp ... | grep ...`) so escape codes and the
    # first-mention title insertion don't corrupt output a script might parse.
    hyperlink = sys.stdout.isatty()

    # Checked ahead of argparse rather than as a subparser: argparse's
    # subparsers positional greedily claims the first token as the subcommand
    # name and errors on anything else, which would break plain free-form
    # queries like "devhelp what should I work on today?" - a query that
    # happens to be the single literal word "standup" isn't reachable this
    # way, but that's an acceptable trade for keeping free-form queries
    # unconstrained (prompt mode remains available for that edge case).
    if argv == ["standup"]:
        return await _run_standup(hyperlink=hyperlink)

    args = _parse_args(argv)
    if not args.query:
        return await _prompt_loop(hyperlink=hyperlink)

    query = " ".join(args.query).strip()
    if not query:
        print("no query given", file=sys.stderr)
        return 2

    try:
        response = await handle_query(query)
    except OrchestratorUnavailable:
        print("assistant temporarily unavailable", file=sys.stderr)
        return 1

    await _render_answer(response, hyperlink=hyperlink)
    return 0


def run() -> None:
    sys.exit(asyncio.run(main(sys.argv[1:])))


if __name__ == "__main__":
    run()
