# CLAUDE.md — DevHelpTool

## Project Purpose

Developer help tool. Summarize the information needed for daily standup updates and give an
engineer a clear idea of what work they have and should prioritize.

Concretely: a local FastAPI service that answers natural-language questions ("what should I
work on today?") by orchestrating a hand-rolled Claude tool-calling loop over the Jira and
GitHub REST APIs, with deterministic Python ranking (not LLM-driven) and graceful
partial-failure degradation. See `specs/feat-engineering-productivity-agent-mvp.md` for the
full technical spec and `docs/ideation/engineering-productivity-agent-mvp.md` for the research
and design decisions behind it.

---

## Directory Structure

```
/
├── CLAUDE.md
├── README.md
├── pyproject.toml                # dependencies, ruff/mypy/pytest config, devhelp console script
├── .env.example                  # documents required env vars; .env itself is gitignored
│
├── app/
│   ├── main.py                   # FastAPI app: POST /ask, GET /health; Slack lifespan hook
│   ├── cli.py                    # `devhelp` console script - terminal wrapper over handle_query()
│   ├── config.py                 # pydantic-settings Settings, loaded from .env
│   ├── agent/
│   │   ├── orchestrator.py       # hand-rolled Claude tool-calling loop
│   │   ├── registry.py           # TOOL_REGISTRY: tool name -> callable
│   │   └── schemas.py            # JSON-schema tool definitions passed to Claude
│   ├── clients/
│   │   ├── jira_client.py        # thin httpx wrapper: auth, JQL search, TTL cache
│   │   └── github_client.py      # thin httpx wrapper: auth, search_prs(), TTL cache
│   ├── tools/
│   │   ├── jira_tools.py         # get_my_high_priority_issues, get_issues_without_prs,
│   │   │                         #   get_my_issues_with_linked_prs,
│   │   │                         #   get_incomplete_issues_from_last_sprint,
│   │   │                         #   get_current_sprint_issues,
│   │   │                         #   get_lower_priority_issues_due_soon,
│   │   │                         #   get_backlog_issues_needing_details,
│   │   │                         #   get_persons_open_issues, draft_comment,
│   │   │                         #   post_comment, get_issues_awaiting_my_response
│   │   └── github_tools.py       # get_my_open_prs, get_prs_awaiting_my_review
│   ├── core/
│   │   ├── models.py             # Issue, PullRequest, ToolResult[T], AskResponse
│   │   ├── ranking.py            # score_issue(), score_pr() - pure, LLM-free
│   │   ├── cache.py              # TTLCache (in-memory, injectable clock for tests)
│   │   ├── errors.py             # sanitize_error() - never leak raw exception text
│   │   └── text.py               # apply_outside_links(), jira_issue_url() - shared Slack + CLI hyperlinking
│   ├── slack/
│   │   ├── bolt_app.py           # AsyncApp, app_mention + message (thread-continuation) handlers
│   │   ├── digest.py             # build_digest() - pure High Priority/Upcoming section logic
│   │   └── formatting.py         # Issue/AskResponse -> Slack Block Kit
│   └── tests/                    # pytest; all external HTTP mocked via respx
│
├── scripts/
│   └── post_digest.py            # launchd-scheduled digest script - standalone, no FastAPI dependency
│
├── specs/                        # technical spec + task breakdown for this project
└── docs/ideation/                # research and design-decision history
```

Tool implementations (`app/tools/`) never import `anthropic` — `app/agent/schemas.py` and
`app/agent/registry.py` are the only modules aware of the LLM. This separation is the core
architectural decision of the project; keep it when adding new tools.

---

## Local Development

```bash
# One-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in real credentials

# Run the service
uvicorn app.main:app --reload --reload-dir app

# Or skip the server entirely and ask from the terminal (prompts for the query,
# so shell metacharacters like ? or * in your question are never an issue)
devhelp
# ^ if this fails with ModuleNotFoundError: No module named 'app', see Troubleshooting below
#   (iCloud/Desktop sync) - `python -m app.cli` always works as a substitute

# Lint / type-check / test
ruff check app/
mypy app/
pytest app/tests/
```

**Local URL:** http://localhost:8000 (`POST /ask`, `GET /health`)

### Environment Variables

```bash
cp .env.example .env
```

Required variables (see `.env.example` for descriptions):
- `ANTHROPIC_API_KEY`
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`
- `GITHUB_TOKEN`, `GITHUB_USERNAME`

Optional (Slack integration — the app runs identically to today if these are unset):
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`
- `GITHUB_REPO` (`"owner/repo"`) — scopes the digest's "PRs You Could Review" search; that
  section is simply skipped (not an error) if unset.

---

## Conventions

### Naming

- **Modules/files:** snake_case (`jira_tools.py`, `ttl_cache.py`)
- **Classes:** PascalCase (`JiraClient`, `ToolResult`)
- **Functions/variables:** snake_case (`get_my_open_prs`, `priority_score`)
- **Constants:** SCREAMING_SNAKE_CASE (`MAX_ITERATIONS`, `TOOL_SCHEMAS`)

### File Organization

- One module per responsibility, grouped by layer (`agent/`, `clients/`, `tools/`, `core/`) —
  see Directory Structure above.
- Tests live in `app/tests/`, one test file per source module (`test_jira_tools.py` tests
  `app/tools/jira_tools.py`), plus `test_integration.py` for end-to-end flows.
- Keep tools independent of the orchestration layer: a tool function must be callable and
  testable with zero knowledge of Claude/the LLM.

### Code Style

- `ruff check app/` and `mypy app/` must both pass — enforced by this repo's `.claude/settings.json`
  hooks (per-file on Write/Edit, project-wide on Stop).
- `ruff`'s rule selection and the one deliberate ignore (`BLE001`, for the tool-boundary
  try/except pattern) are documented in `pyproject.toml`.
- Prefer plain functions and dataclasses/Pydantic models over classes with behavior, except
  where a class genuinely holds connection state (`JiraClient`, `GitHubClient`, `TTLCache`).

---

## Common Patterns

### Error Handling

Every tool function returns a `ToolResult` rather than raising, so one integration failing
degrades the response gracefully instead of crashing the whole `/ask` request:

```python
# app/core/models.py
class ToolResult[T](BaseModel):
    ok: bool
    data: T | None
    error: str | None
    note: str | None = None  # optional, ok=True-only context - see below
```

```python
# app/tools/*.py — the pattern every tool follows
try:
    raw = await some_client.fetch(...)
    mapped = [_map_issue(item) for item in raw]  # mapping stays inside the try
except Exception as exc:
    return ToolResult(ok=False, data=None, error=sanitize_error(exc))
return ToolResult(ok=True, data=mapped, error=None)
```

The mapping step belongs *inside* the `try`, not after it — `_map_issue()` calls
`score_issue()`, which does a dict lookup against a fixed set of priority names
(`PRIORITY_WEIGHTS` in `app/core/ranking.py`). A Jira project using a non-standard priority
scheme (`P0`–`P4`, custom names, etc.) raises `KeyError` from *inside* the mapping step, not
from the HTTP call — if mapping happened after the `try`/`except`, that exception would
propagate uncaught instead of degrading to `ToolResult(ok=False, ...)`. This was a real bug
(caught in code review, not by the test suite, since every test mocks `search()` with
well-formed fixture priorities) — the orchestrator's own `dispatch()` has a second layer of
try/except that happened to mask it for `/ask`, but `scripts/post_digest.py` calls tool
functions directly with no such safety net, so the bug was only exposed once a caller
without that extra layer existed.

`sanitize_error()` (`app/core/errors.py`) ensures only a status code/exception type reaches the
client or the LLM — never a raw response body, which could contain sensitive data.

`unwrap_tool_result()` (also `app/core/errors.py`) is for callers that invoke a tool function
directly instead of through the orchestrator's `dispatch()` — `scripts/post_digest.py` and
`app/slack/bolt_app.py`'s standup-summary action handler, both of which have no LLM to hand a
warning to the way `dispatch()` does. It logs the failure and degrades to `[]` instead of
raising, generic over the list element type (`ToolResult[list[T]] -> list[T]`) since both
callers use it for both `Issue` and `PullRequest` results.

`note` is for a different case than `error`: still `ok=True` (not a failure), but the tool has
extra context the raw `data` can't express — e.g. "empty list because X, not because everything's
done." `app/agent/orchestrator.py:dispatch()` only forwards `result.data` to Claude by default; if
`result.note` is set, it wraps the payload as `{"data": ..., "note": ...}` instead of sending the
bare array/object, specifically so the model doesn't have to guess at an ambiguous empty result.
Only set this when `data` alone is genuinely ambiguous — don't use it as a general "extra info"
channel.

### Async Operations

Tool calls within a single Claude turn are dispatched concurrently, not sequentially:

```python
# app/agent/orchestrator.py
tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
results = await asyncio.gather(
    *[dispatch(block, called_tools, warnings) for block in tool_use_blocks]
)
```

### Caching

`JiraClient.search()` and `GitHubClient.search_prs()` both wrap their HTTP call in a
`TTLCache` (default 60s), keyed on the query string. `GitHubClient` is a single shared
singleton (`app/tools/github_tools.py:github_client`, imported by `jira_tools.py` too) so the
cache is actually shared across both tool modules rather than duplicated.

### Issue ↔ PR Linking

Jira issues are matched to GitHub PRs by literal issue-key text, not by any structured field:
`_ISSUE_KEY_PATTERN` (`app/tools/jira_tools.py`) is `[A-Z]+-\d+`, run against each PR's
title+body via `_fetch_raw_prs_by_issue_key()`. A PR is considered linked to `AL-3` if the
substring `AL-3` appears anywhere in its title or body — parens, brackets, or bare, doesn't
matter.

Because of this, matching only works if your Jira project's *actual issue key* matches what
you write in PR titles. If you want PRs labeled `(AL-<n>) ...` to link up, your Jira project key
needs to literally be `AL` (Project settings → Details → Key, in Jira Cloud) — there's no
separate summary-text-parsing path. `get_my_issues_with_linked_prs` surfaces the matched PR
(`Issue.linked_pr`); `get_issues_without_prs` uses the same matching to filter, but only ever
returns `has_linked_pr=False` issues (it never populates `linked_pr`).

### Sprint Lookups

`get_incomplete_issues_from_last_sprint` uses Jira's Agile REST API (`/rest/agile/1.0/...`),
not the plain issue-search API used everywhere else: `JiraClient.get_boards()` (scoped by
`JIRA_PROJECT_KEY`) → `get_closed_sprints(board_id)` for each board → picks the sprint with the
latest `completeDate`/`endDate` → JQL `sprint = <id> AND assignee = currentUser() AND
resolution = Unresolved`. This only works for Scrum boards — Kanban boards have no sprint
concept, so `get_closed_sprints` returns an empty list for them and the tool falls back to an
empty (not error) result. Board/sprint responses are cached in the same `TTLCache` instance as
`search()`, keyed by `"boards:<project_key>"` / `"sprints:<state>:<board_id>"`.

When there's no closed sprint at all (nothing to formally report on), the tool doesn't just
return an empty list silently — an empty result is genuinely ambiguous (no sprint ever closed,
vs. a sprint closed with nothing left incomplete). It checks `get_active_sprints()` for a sprint
whose `endDate` has already passed but was never closed, and if found, surfaces it via
`ToolResult.note` (e.g. `Did you mean "Sprint 1"?`) — see the `note` field discussion under Error
Handling above for how that reaches the model.

`get_current_sprint_issues` is the active-sprint counterpart used by the Slack digest (below):
same board-scan pattern, but against `get_active_sprints()` instead of `get_closed_sprints()`,
unioning issues across every board with an active sprint (`sprint in (<id1>,<id2>,...)`). Unlike
the closed-sprint case, an empty active-sprint list has exactly one meaning ("no sprint running
right now"), so it returns a plain empty `ToolResult` with no `note`. **Live-testing gap:** the
multi-board/multi-active-sprint union path is covered by mocked tests
(`test_get_current_sprint_issues_unions_across_multiple_boards`) but unverified against a real
Jira instance — this project's own Jira project has never had a sprint (closed or active) at
all yet, the same live-testing limitation already noted for the "did you mean Sprint X" feature
above.

### Cross-User Lookups and the Draft/Confirm Write Path

Three tools extend the original read-only design:

- **`get_persons_open_issues(person: str)`** (`app/tools/jira_tools.py`) is the first
  *parameterized* tool in `TOOL_REGISTRY` — every tool before it took zero arguments.
  `dispatch()` (`app/agent/orchestrator.py`) now calls `tool_fn(**block.input)` rather than
  `tool_fn()`; since every existing tool's schema still declares `input_schema.properties: {}`,
  `block.input` is `{}` for them and `tool_fn(**{})` is exactly `tool_fn()` — fully backward
  compatible. `TOOL_REGISTRY`'s value type loosened from `Callable[[], Awaitable[ToolResult]]`
  to `Callable[..., Awaitable[ToolResult[Any]]]` to allow this, which does give up mypy's
  ability to check a tool's arity/parameter names against its schema — that check now lives in
  `app/tests/test_agent_registry.py::test_registry_function_signatures_match_schema_properties`
  instead, a runtime replacement for the lost static guarantee. `get_persons_open_issues` itself
  resolves a free-text name/email against Jira's `/rest/api/3/user/search` endpoint (note: this
  endpoint returns a **bare JSON array**, unlike every other Jira endpoint this codebase calls,
  which wrap results in `{"values": [...]}`) and, on a single unambiguous match, queries
  `assignee = "<accountId>"` the same way `_UNRESOLVED_JQL` queries `currentUser()`. Zero or
  multiple matches don't fail the tool call — they return `ok=True, data=[], note="..."` (the
  same ambiguous-empty-result convention as the "did you mean Sprint X?" case above), so Claude
  can ask a clarifying follow-up instead of surfacing a `warnings`-list failure for what's
  really "be more specific." PR-linking is deliberately not wired in here — the existing linking
  helper (`_fetch_raw_prs_by_issue_key`) is hardcoded to `settings.github_username`, i.e. "my"
  authored PRs only.

- **`get_issues_awaiting_my_response()`** is the "someone's waiting on your reply" signal. The
  rule is specifically **a comment that @-mentions me, with no comment by me since** — not the
  broader "the last comment on the ticket isn't mine," which was considered and rejected because
  someone else's comment is very often the resolution to a thread, not a question aimed at the
  user. `_adf_contains_mention()` walks a comment's Atlassian Document Format body the same way
  the existing `_adf_text()` walks a description; `_awaiting_response()` then walks a comment
  list in chronological order (`JiraClient.get_comments()` passes `orderBy=created` precisely so
  this ordering can be trusted) and keeps a single `awaiting` flag: a comment authored by me
  clears it, a comment mentioning me sets it — regardless of who comments after, which is the
  exact distinction from the rejected rule. This is deliberately its own dedicated tool rather
  than a new field on `RawIssue`/an input to `score_issue()` — folding it into ranking would mean
  fetching every issue's comments on *every* existing tool call (`get_my_high_priority_issues`,
  `get_issues_without_prs`, etc.), not just when this specific signal is asked for. Cross-tool
  synthesis (weaving "AL-13 needs your reply" into the `Focus:` line) is left to
  `SYSTEM_PROMPT`, the same way the model already combines high-priority issues, overdue items,
  and PR review requests into one recommendation today — no new Python ranking code combines
  this signal with anything else.

- **`draft_comment(issue_key, note_text)` / `post_comment(issue_key, note_text)`** implement the
  codebase's first Jira **write**. They are split on purpose: `draft_comment` is registered in
  `TOOL_REGISTRY`/`TOOL_SCHEMAS` and is the only way the orchestrator's tool-calling loop ever
  touches this feature — it performs **zero Jira I/O**, just packaging the issue key and the
  user's own note text into a `CommentDraft` (`app/core/models.py`), which
  `orchestrator.dispatch()` captures into `AskResponse.pending_comment_draft` (mirroring the
  existing `known_issues`/`known_prs` mutable-accumulator pattern already used for
  `referenced_issues`/`referenced_prs`). `post_comment` is **not** registered as a tool at all —
  it wraps the actual `POST /rest/api/3/issue/{key}/comment` call
  (`JiraClient.add_comment()`, the client's first mutating method and its only uncached one) and
  is only ever invoked by `app/cli.py`'s `_confirm_and_post_comment()` (a y/n prompt, same
  `asyncio.to_thread(input, ...)` pattern as the free-form query prompt) or
  `app/slack/bolt_app.py`'s `ask_confirm_comment` action handler, both *after* an explicit human
  confirmation — the LLM can draft a note but can never post one. This mirrors the existing
  `devhelp standup` / Slack standup-summary-button precedent of bypassing the LLM entirely for a
  deterministic action, just applied to a write instead of a read. The Slack confirm button
  (`app/slack/formatting.py:_comment_draft_blocks`) is the first button in this codebase to carry
  a `value` payload at all (`json.dumps({"issue_key": ..., "note_text": ...})`, decoded via
  `body["actions"][0]["value"]`) — every earlier button either opened a `url` client-side or had
  its handler re-derive everything from `body["message"]`. `SYSTEM_PROMPT` tells Claude to never
  claim the comment has been posted after calling `draft_comment` — the actual write happens
  entirely outside the model's control. `_plain_text_to_adf()` splits the note on `"\n"` into one
  ADF paragraph node per line - ADF has no notion of a raw newline inside a single text node, so
  packing a multi-line note into one text leaf would silently render as one run-on line in Jira.
  After a successful (or failed) post, `handle_confirm_comment` calls Bolt's injected `respond()`
  with `replace_original=True` to strip the buttons and show the outcome in place - closing the
  double-click-to-double-post window without needing `chat.update`'s extra scope. This can't be a
  bare `respond(text=...)`, though: the draft's 3 blocks (divider/section/actions, each tagged
  with a `comment_draft_*` `block_id` in `_comment_draft_blocks`) live inside the *same* message
  as the main answer and the standup-followup prompt - `format_ask_response` posts everything in
  one combined `say()` call. `replace_comment_draft_blocks()` (`app/slack/formatting.py`) swaps
  out only the 3 tagged blocks for a single outcome line, so `respond()` can safely
  `replace_original` without wiping the answer text or the standup buttons sitting in the same
  message. (An earlier version of this fix called `respond(replace_original=True, text=...)` with
  no `blocks` at all, which would have replaced the *entire* message - caught in a later
  self-review pass before being committed.)

  Known accepted gaps, not fixed: a real-but-wrong issue key is only caught by the human reading
  the confirmation prompt/button, not by code (same class of prompt-level-only rule as the
  system prompt's "no PR started" phrasing rule); `GET .../comment` is paginated
  (`maxResults=50` default), so an issue with more than 50 comments would only have its first
  page inspected by `get_issues_awaiting_my_response`.

### CLI

`app/cli.py` is a third interface onto the same orchestrator used by `/ask` and the Slack
@-mention path (`app/slack/bolt_app.py:handle_mention`) — deliberately not a fourth
reimplementation of the tool-calling loop. Installed as the `devhelp` console script
(`[project.scripts]` in `pyproject.toml`, `app.cli:run`), so it runs without `uvicorn` and
without any Slack config. `main(argv)` joins the positional `query` args into one string if any
were given (the single-shot path, `handle_query()`, unchanged since the CLI's first version);
if not, it hands off to `_prompt_loop()` instead of running one prompt-and-exit. Direct-argument
invocation exists precisely so typed input never gets parsed by the shell the way argv is, so a
query containing `?`/`*` (zsh/bash glob characters) never breaks either way you invoke it. Every
`input()` call (in both paths) goes through `asyncio.to_thread` rather than a bare call, since
`main()`/`_prompt_loop()` are coroutines and a blocking call would stall the event loop (`ruff`'s
`ASYNC250` catches a bare one). `_render_answer()` (shared by both paths, so their printing can't
drift apart) prints `response.answer` to stdout and each `response.warnings` entry to stderr —
mirroring `/ask`'s partial-degradation behavior (a failed tool call degrades the answer rather
than failing the whole request) rather than treating a warning as fatal. The single-shot path's
two non-zero exit codes are unchanged: `2` for a blank/whitespace-only query passed as args
(mirroring argparse's own convention for a usage error), `1` for `OrchestratorUnavailable` (the
Anthropic call itself failing) — a sanitized message goes to stderr rather than the raw
exception, since there's no partial answer to fall back to either way, but the two stay distinct
since one is a user-input problem and the other is a backend failure. For free-form questions,
`cli.py` goes through the LLM orchestrator the same way `/ask` does (never calls `app/tools/*`
directly for that path), so it gets the same tool selection and prioritized-summary behavior,
not just a raw dump of one tool's data.

**Persistent no-args session** (`_prompt_loop()`): running `devhelp` with no arguments at all no
longer answers one question and exits — it opens a session that keeps
`orchestrator.handle_conversational_query()`'s returned `history` in a plain local variable and
threads it into the next call, so a follow-up like "does that one have a due date?" resolves
against the prior answer without repeating the ticket key (verified live against the real
Anthropic API, not just mocked tests). This was a deliberate, explicit reversal of this project's
earlier stateless-per-call design (see "Continuing a conversation without a mention" under Slack
Integration below for the matching Slack-side change and the shared history mechanism both rely
on). A blank line, EOF (Ctrl-D), or typing `exit`/`quit` (case-insensitive) ends the session
cleanly with exit code `0` — a deliberate behavior change from the single-shot path's `2`/blank
handling, since a missing/quit answer to an already-open, otherwise-good session isn't a usage
error the way a blank query passed as an argument is. An `OrchestratorUnavailable` mid-session
prints the same sanitized warning as the single-shot path but **prompts again** rather than
exiting `1` — a transient backend hiccup shouldn't force the user to restart the whole session
and lose its history. `devhelp "<query>"` (direct arguments) is untouched by any of this — still
exactly one `handle_query()` call, no history, same exit codes as before; this was an explicit
locked decision so a script piping in one query never hangs waiting for more input. `devhelp
standup` (below) is likewise unaffected — no LLM, no history, no loop, ever.

`devhelp standup` is the one exception: passing exactly the single literal argument `"standup"`
(checked before `_parse_args` even runs, since argparse's own subparsers mechanism would
otherwise greedily claim the first positional token as a subcommand name and error on any other
free-form query starting with a different word) routes to `_run_standup()`, which mirrors
`app/slack/bolt_app.py:handle_standup_summary_request` almost exactly: same two direct tool
calls (`jira_tools.get_my_issues_with_linked_prs`, `github_tools.get_prs_awaiting_my_review`),
same `unwrap_tool_result()` degradation, same `app/slack/digest.py:build_standup_summary()` —
just rendered for a terminal instead of Block Kit, since `_issue_bullet`/`_pr_bullet` in
`app/slack/formatting.py` bake in Slack's `<url|text>` link syntax, which would print as literal
noise in a terminal. `_issue_line`/`_pr_line` in `cli.py` are the terminal equivalents; the
actual priority-emoji predicate they both key off, `priority_emoji()`, was promoted out of
`app/slack/formatting.py` into `app/core/ranking.py` (alongside `is_urgent`, which it wraps)
specifically so the CLI didn't have to duplicate a private Slack-module function to get the same
red/yellow/green priority language — check `app/core/ranking.py` before re-adding any
priority→emoji logic elsewhere. `devhelp standup` never calls the Anthropic API at all, matching
the Slack button's design intent: instant, deterministic, no LLM latency or cost for a summary
that's pure data selection.

**Terminal hyperlinking**: both the free-form-question path and `devhelp standup` turn Jira issue
keys and PR mentions into clickable links when `sys.stdout.isatty()` — checked once per `main()`
call and threaded through as an explicit `hyperlink: bool` parameter rather than read ad hoc
deep in each renderer, so tests can exercise both branches without monkeypatching `sys.stdout`
in most of them. `cli.py:_terminal_link()` emits the OSC 8 escape sequence
(`\033]8;;{url}\033\\{text}\033]8;;\033\\`), the terminal-emulator equivalent of Slack's
`<url|text>` — unsupported terminals just print the escape bytes as inert noise around plain
text, so this is only ever attempted when stdout is confirmed to be a real tty (piped/redirected
output, e.g. `devhelp standup > log.txt`, stays plain text automatically). `_hyperlink_prs`/
`_hyperlink_issues` mirror `app/slack/formatting.py`'s `_hyperlink_prs`/
`_hyperlink_and_flag_issues` almost exactly (including the same first-mention-links-key-and-title,
later-mentions-link-bare-key behavior for issues) but deliberately skip the priority-emoji
insertion those Slack functions also do — that's compensation for Block Kit having no other way
to signal priority inline; a terminal answer is already ordinary prose, so there's no equivalent
gap to fill. Both Slack's and the CLI's hyperlinking share two things out of `app/core/text.py`
now, not just the escape-sequence-vs-markdown difference: `apply_outside_links()` (extracted from
what used to be `formatting.py`'s private `_apply_outside_existing_links`) splits text on spans
matching an already-built-link pattern and only substitutes in the text *between* them — guarding
against a substitution pass reaching inside a link it or an earlier pass already built and
corrupting it (e.g. a PR's own URL containing a `?ref=AL-1`-shaped query string, which is a real,
tested case in `test_cli.py`, not a hypothetical). Only the link-pattern regex differs per caller
(`_SLACK_LINK_PATTERN` = `<[^<>]+>` vs `cli.py`'s `_TERMINAL_LINK_PATTERN` matching a full OSC 8
span) — the splitting/reassembly logic is identical, so a bug fixed in one no longer needs a
matching fix in the other found separately. `jira_issue_url()` is the other shared piece — both
modules build the exact same `{JIRA_BASE_URL}/browse/{key}` string, so it lives in `text.py`
rather than as two private copies; this was caught and fixed in a self-review pass after the
initial CLI implementation (it started as a private copy in `cli.py`, duplicating
`formatting.py`'s), so check `app/core/text.py` before re-adding a `_jira_issue_url`-shaped
helper anywhere new.

### Slack Integration

Two independent entry points, neither of which changes `/ask`'s behavior:

- **`app/slack/bolt_app.py`** (@-mention): a `slack_bolt.AsyncApp` running over Socket Mode
  (no public HTTPS endpoint or tunnel needed) inside the same process as `uvicorn`, started from
  `app/main.py`'s `lifespan` handler. `handle_mention` calls
  `orchestrator.handle_conversational_query()` — no HTTP round-trip to the app's own `/ask`
  route — and replies in-thread. This whole module constructs a real `AsyncApp` at import time
  using `settings.slack_bot_token`, which raises if the token is falsy; `app/main.py` only
  imports it from inside the `if settings.slack_bot_token and settings.slack_app_token:` branch,
  specifically so the app boots identically to today when Slack isn't configured (the import is
  never reached).
- **Continuing a conversation without a mention** (`app/slack/bolt_app.py:handle_thread_reply`,
  `_thread_histories`): a deliberate, explicit reversal of this project's earlier
  stateless-per-call design (see the CLI's matching `_prompt_loop()` under the CLI section
  above). `_thread_histories: dict[str, list[dict]]` is a module-level, in-memory-only dict
  keyed by thread root `thread_ts`, holding exactly the same `history` shape
  `orchestrator.handle_conversational_query()` consumes/returns. `handle_mention` now keys on
  `event.get("thread_ts") or event["ts"]` rather than the bare `event["ts"]` it used before this
  feature — **this also fixed a real, previously-untested bug**: replying to a second @-mention
  inside an already-open thread used to reply with `thread_ts=event["ts"]` (the mention's own
  timestamp), forking a visually-new thread instead of continuing the existing one;
  `handle_standup_summary_request` (below) already had the correct `.get("thread_ts") or
  ...["ts"]` pattern, this handler just hadn't matched it. A new `@slack_app.event("message")`
  handler, `handle_thread_reply`, lets a plain reply continue a tracked thread with no re-mention
  needed (the locked design choice over the more conservative "must @-mention every time"
  alternative) — it's gated on four checks, each closing a real gap, not defensive padding:
  (1) `event.get("subtype") not in (None, "thread_broadcast")` excludes edits/deletes/joins while
  *keeping* `thread_broadcast` ("also send to channel") replies, which are legitimate and would
  otherwise be silently dropped by a blanket subtype exclusion; (2) `event.get("user") ==
  context.get("bot_user_id")` (Bolt injects `context["bot_user_id"]` automatically for any
  listener naming a `context` parameter, same as this file's existing `ack`/`body`/`respond`
  injection) skips the bot's own posted messages; (3) `thread_ts not in _thread_histories` skips
  any thread this bot didn't itself start via a mention - a plain reply to an unrelated thread is
  never answered; (4) `_mentions_bot()` skips a message that mentions the bot, because **Slack
  delivers both `app_mention` and `message.channels` events for the same mentioning message** -
  without this check, a mention inside a tracked thread would get two replies, one from each
  handler. Requires the `message.channels` (+ `.groups`/`.im`/`.mpim` variants) Event Subscription
  and `channels:history` (+ matching variants) OAuth scope, neither of which this project needed
  before - see README's Slack setup steps, including the "reinstall after adding a scope"
  requirement Slack itself enforces. `_thread_histories` is unbounded in the *number* of threads
  tracked over the process's lifetime (each thread's own history is separately capped by
  `orchestrator.MAX_HISTORY_TURNS`) and is lost on process restart - both accepted limitations,
  consistent with this being a local, single-user tool with no eviction machinery anywhere else
  either. Both handlers funnel through a shared `_answer_and_reply()` guarded by a per-thread
  `asyncio.Lock` (`_thread_locks`, keyed the same way as `_thread_histories`) - **not** just a
  defensive measure. Socket Mode dispatches every incoming event as its own concurrent
  `asyncio` task (confirmed directly in the installed `slack_sdk` package, not assumed), so two
  messages landing in the same thread close together - a quick follow-up correction, or the same
  message's `app_mention` and `message` events both reaching their handlers - would otherwise
  both read the same pre-update history and the slower write would silently clobber the faster
  one, dropping a whole exchange from what the thread remembers from then on. This was caught in
  a self-review pass, not the original test suite (a test that awaits handlers one at a time
  can't reproduce two genuinely concurrent tasks); `test_answer_and_reply_serializes_concurrent_calls_to_the_same_thread`
  in `test_slack_bolt_app.py` exercises the two real handlers concurrently via `asyncio.gather`
  specifically to catch a regression here, and was confirmed to actually fail without the lock
  (not just pass trivially) before being kept.
- **`scripts/post_digest.py`** (scheduled digest): a standalone script, *not* triggered by
  anything inside the long-running FastAPI process — it imports `app.tools.jira_tools` and
  `app.slack.digest`/`formatting` directly and is meant to be run by `launchd` (see README's
  Slack Integration section for the plist). This was a deliberate choice over an in-process
  scheduler (e.g. APScheduler): the app isn't always-running today (`uvicorn --reload`, started
  manually), so an in-process cron job would only fire the digest if the dev server happened to
  be up at that hour — a standalone OS-scheduled script sidesteps that without turning the app
  into an always-on daemon.
- **`app/slack/digest.py:build_digest()`** is a pure function (no Slack/HTTP imports) that
  decides the two-section (`High Priority` / `Upcoming`) content from five already-fetched issue
  lists, with a fallback chain: real high-priority work → due-soon lower-priority work → an
  explicit `"Nothing due soon"` empty state (never a silently-missing section, same
  anti-ambiguity principle as `ToolResult.note` above). `app/slack/formatting.py` renders that
  into Slack Block Kit bullets.
- **Digest Quick Links buttons** (`app/slack/formatting.py:_issue_actions_block`): each
  High Priority issue gets its own `section` + `actions` block pair (`Open in Jira`, plus
  `View PR #<n>` when linked), not one shared block for the whole section — Slack has no way
  to attach a button row to a single line inside a combined text block, which is why
  `_high_priority_blocks` emits per-issue pairs while `_upcoming_blocks` (lower emphasis,
  no buttons) still emits one combined block. `url`-type buttons make Slack open the link
  client-side regardless of the app's response, but Slack still POSTs the interaction and
  expects an ack within 3s or the user sees an error toast — `bolt_app.py` registers a
  shared `_ack_only` handler for `digest_open_jira`/`digest_view_pr` (and
  `ask_standup_dismiss`, below) purely to satisfy that.
- **Standup summary** (`app/slack/digest.py:build_standup_summary`,
  `app/slack/formatting.py:format_standup_summary`): every `/ask`/@-mention reply
  (`format_ask_response`) unconditionally appends a "want a succinct standup summary?"
  prompt with two buttons — never inferred from the question text (asking is the point;
  "what should I work on today" must not be assumed to mean "give me a standup summary").
  Clicking `ask_standup_summary` (handled in `bolt_app.py`) bypasses the LLM/orchestrator
  entirely — it calls `jira_tools.get_my_issues_with_linked_prs()` (not
  `get_my_high_priority_issues()` — the PR-annotated tool, specifically so a "Doing" issue's
  still-open PR comes along with it) and `github_tools.get_prs_awaiting_my_review()`
  directly, then posts a reply in the same thread. Buttons over free text was a deliberate
  choice made when this feature shipped: a free-text follow-up ("yes, give me the short
  version") would need thread-scoped conversation memory (`thread_ts` → prior messages), which
  nothing in this codebase had at the time — a button click carried enough intent on its own, no
  memory required. **That premise changed** once `_thread_histories` (below, under "Continuing a
  conversation without a mention") was added — thread-scoped memory now exists — but the button
  choice here still stands on its own merits (an explicit action is unambiguous in a way a
  short reply like "yes" isn't, and costs nothing extra now that memory exists anyway), so this
  wasn't revisited. Three sections:
  `Doing` (issues from that fetch filtered to High/Highest priority — uncapped, so someone
  with 6 high-priority issues in flight sees all 6, not a truncated 4), `Reviewing` (PRs
  awaiting review, unchanged), `Next Up` (backfilled from the same ranked fetch, excluding
  whatever's already in `Doing`, only when `Doing + Reviewing` together total fewer than 2 -
  keeps a thin day from reading as a single bare bullet without padding a busy one). No tool
  currently tracks "completed since yesterday," so there's no `Done` section.
- **Draft-comment confirm buttons** (`app/slack/formatting.py:_comment_draft_blocks`): when
  `AskResponse.pending_comment_draft` is set, `format_ask_response` inserts a section showing the
  drafted issue key + note text plus two buttons — `"Post to Jira"` (`ask_confirm_comment`,
  carrying the issue key/note text as a JSON `value` payload — the first button in this codebase
  to carry one at all) and `"Discard"` (`ask_discard_comment_draft`, bound to the shared
  `_ack_only` handler like the other purely-acknowledged buttons). `bolt_app.py`'s
  `handle_confirm_comment` is the only code path in the whole app that ever calls
  `jira_tools.post_comment()` from Slack — see "Cross-User Lookups and the Draft/Confirm Write
  Path" above for the full design.
- **`/ask` answer enrichment** (`app/slack/formatting.py:format_ask_response`): the
  orchestrator's `SYSTEM_PROMPT` instructs Claude to write Slack mrkdwn directly (single-
  asterisk bold, `•` bullets, no markdown links) and to lead with a bolded `*Focus: ...*`
  line when there's one clear next action — but LLM instruction-following isn't a
  guarantee, so `format_ask_response` runs a deterministic pass over `response.answer`
  regardless of what the model wrote: `_sanitize_markdown_for_slack` normalizes stray
  standard-Markdown (`**bold**`, `# headers`, `- bullets`, `[text](url)`) to Slack mrkdwn,
  then `_hyperlink_prs`/`_hyperlink_and_flag_issues` turn `"PR #514"`/`"AL-12"` mentions into
  real links using `AskResponse.referenced_issues`/`referenced_prs` — every `Issue`/
  `PullRequest` actually returned by a tool call this turn, collected by
  `orchestrator.py:_record_referenced_data` inside `dispatch()` and threaded through
  `AskResponse`, **never** derived by parsing the answer text itself. A ticket/PR mentioned
  but not fetched this turn is left as plain text rather than linked to a guessed URL.
  **Gotcha:** an issue key can appear *inside* an already-built link's URL (e.g.
  `.../browse/AL-12`, or a markdown link the sanitizer just converted) — naively
  regex-substituting over the whole string produces broken nested `<...<...>...>` markup.
  `_apply_outside_existing_links` splits on existing `<...>` spans and only transforms the
  text between them; both hyperlinking passes go through it. This was caught by manual
  testing against a real answer shape, not the unit test suite — worth remembering when
  adding any future text-substitution pass here.
- **Priority emoji** (`app/slack/formatting.py:_priority_emoji`): three-tier, not a
  per-priority-name gradient — 🔴 for High/Highest priority *or* anything overdue, 🟡 for
  Medium, 🟢 for everything else (including any non-standard priority name, falling back
  rather than raising - same principle `PRIORITY_WEIGHTS` should follow but doesn't, per the
  KeyError gotcha above). Shared by the digest, the standup summary, and `/ask` issue
  mentions. One exception: `_high_priority_blocks` forces 🔴 unconditionally regardless of
  an issue's actual priority field, since membership in that digest section already means
  "high priority OR due soon" by `build_digest`'s own fallback chain (above) - a
  Medium-priority issue that landed there via the due-soon path should still read as urgent.
- Both `/ask`/@-mention and the digest's buttons require **Interactivity & Shortcuts** to be
  toggled on for the Slack app (separate from Socket Mode, no Request URL needed) - without
  it, every button silently does nothing when clicked, since Slack won't deliver
  `block_actions` payloads to the socket connection at all.
- Unnumbered issue bullets (`_upcoming_blocks`, standup summary's `Doing`/`Next Up`) use the
  priority emoji itself as the bullet, not a separate `"•"` alongside it — see
  `_issue_bullet`'s `prefix` logic. Numbered lists (digest `High Priority`) keep the number;
  PR bullets (`_pr_bullet`) always keep `"•"` since PRs never carry a priority emoji.
- **PRs You Could Review** (`app/tools/github_tools.py:get_prs_i_could_review`,
  digest-only — not in `TOOL_REGISTRY`/`TOOL_SCHEMAS`, same as the other digest-only Jira
  tools): open PRs in `settings.github_repo` (`"owner/repo"`, optional) not authored by the
  user and not already review-requested from them — a proactive-review discovery section,
  distinct from `get_prs_awaiting_my_review` (explicit review requests). Skips the search
  entirely (`ok=True, data=[]`, not an error) when `github_repo` is unset, same
  optional-feature pattern as Slack's own settings. `format_digest`'s
  `_prs_to_review_blocks` omits the section entirely when empty, unlike `High
  Priority`/`Upcoming` — this is supplementary content, not one of the two core sections the
  original anti-ambiguity design applies to.
- **`/ask` first-mention vs. subsequent-mention linking**
  (`formatting.py:_hyperlink_and_flag_issues`): the *first* time an answer mentions a given
  issue key, both the key and its title get linked (two separate `<url|text>` spans, e.g.
  `🔴 <url|AL-12> <url|Fix auth bug>`); every mention after that links just the bare key, no
  emoji or repeated title. Tracked via a `seen` set closed over by the regex callback, in
  order of appearance in the text (not `referenced_issues` order).
- The orchestrator's `SYSTEM_PROMPT` also forbids stating a ticket's priority level or
  section/bucket in words (the auto-inserted emoji already conveys it) and locks the
  no-linked-PR phrasing to one exact string, "no PR started", used consistently rather than
  varying per mention — both are prompt-level rules, not code-enforced, so LLM
  non-compliance is possible in principle even though nothing in `format_ask_response`
  currently guards against either.

---

## Troubleshooting

**A Slack thread reply gets answered twice — once from a bare reply, once seemingly duplicated:**
- Slack delivers *both* the `app_mention` event and a `message.channels` (or `.groups`/`.im`/
  `.mpim`) event for a single message that mentions the bot. `app/slack/bolt_app.py`'s
  `handle_thread_reply` guards against this via `_mentions_bot()` — if you see a double reply,
  check that guard hasn't been bypassed (e.g. a mention token format Slack sends that
  `_MENTION_TOKEN` doesn't match) rather than assuming it's an ack/retry issue.

**Slack conversation continuation appears to have "forgotten" everything after a redeploy or
restart, and the CLI's persistent session obviously doesn't survive between separate invocations:**
- Both are in-memory only, by design (`_thread_histories` in `bolt_app.py`, the CLI's local
  `history` variable in `_prompt_loop()`) — there is no database or file-backed persistence for
  conversation history anywhere in this project. This is an accepted limitation for a local,
  single-user tool, not a bug to chase.

**`ruff`/`mypy`/`pytest` hooks fail with "command not found":**
- The `.venv` hasn't been created yet, or dependencies aren't installed — run the one-time
  setup under Local Development above. Hooks in `.claude/settings.json` call `.venv/bin/ruff`
  etc. directly (not the bare command), so they need the venv to exist at the repo root.

**`devhelp` fails with `ModuleNotFoundError: No module named 'app'`, even though `pytest`/`uvicorn`
work fine and `.venv/bin/python3 -c "import app"` succeeds:**
- If this project lives under `~/Desktop` or `~/Documents` with iCloud Drive Desktop &
  Documents syncing enabled (`defaults read com.apple.finder FXICloudDriveDesktop` → `1`),
  this is almost certainly why: the console script's editable-install `.pth` file under
  `.venv/lib/.../site-packages/` is a small file macOS periodically dematerializes (evicts the
  local copy, fetches on demand). `site.py` reads that directory at interpreter startup to
  register the editable-install mechanism that makes `app` importable outside the project
  root — when the file isn't locally resident at that instant, the read silently no-ops (no
  exception, no warning) and `app` never lands on `sys.path`. This reproduces intermittently:
  works immediately after `pip install`, then starts failing again on its own with no further
  changes made — confirmed on this project by running `devhelp --help` in a loop and watching
  it flip from passing to consistently failing. Switching install modes
  (`--config-settings editable_mode=compat`) does *not* durably fix it — it's the same
  dematerialization issue hitting a differently-shaped `.pth` file, just less often.
  `pytest`/`uvicorn` are unaffected only because they're invoked from the project root, where
  the plain `''` (cwd) entry on `sys.path` finds `app/` directly without touching this
  mechanism at all — the installed `devhelp` script runs from `.venv/bin/`, which has no such
  luck.
  - **Reliable workaround, no reinstall needed:** `python -m app.cli <query>` (or
    `.venv/bin/python3 -m app.cli <query>`) — `python -m` always puts the current working
    directory on `sys.path` directly, sidestepping the `.pth`/site-packages mechanism
    entirely. This has not failed once in repeated testing, unlike the installed script.
  - **Durable fix:** exclude `.venv` from iCloud sync, e.g. `mv .venv .venv.nosync && ln -s
    .venv.nosync .venv` (the `.nosync` suffix is a convention iCloud/Finder respects), or
    create the venv somewhere outside `~/Desktop`/`~/Documents` entirely.

**`uvicorn --reload` crashes with `TimeoutError: [Errno 60] Operation timed out` reading a `.venv` file,
or reload-loops on package files like `fastapi/__init__.py`:**
- The reloader is watching the whole project root, including `.venv/lib/.../site-packages/` — thousands
  of installed-package files it has no reason to watch, which also adds to disk-I/O pressure (see next
  entry). Always run with `--reload-dir app` to scope watching to actual source files:
  `uvicorn app.main:app --reload --reload-dir app`.

**Tests are extremely slow (10+ minutes for a handful of tests):**
- Check for system-level memory/disk pressure (`vm_stat`, `sysctl vm.swapusage`) before assuming
  a code problem — this has been the actual cause during development, not a bug in the tests.
  A `.venv/.metadata_never_index` marker is already in place to keep Spotlight from indexing
  the virtualenv, which was a contributing factor.

**A test asserting on `mock.await_args_list[...].kwargs["messages"]` behaves unexpectedly:**
- `handle_query()`'s `messages` list is mutated in place across loop iterations. A plain
  `AsyncMock`'s recorded call args are references, not snapshots, so every recorded call ends
  up showing the *final* state of the list. Capture a shallow copy inside a custom
  `side_effect`/fake function instead (see `app/tests/test_integration.py` for the pattern).

**`/ask` returns 503 "assistant temporarily unavailable", with the real error logged as
`tools.0.custom.name: String should match pattern '^[a-zA-Z0-9_-]{1,128}$'`:**
- A tool name in `app/agent/schemas.py`/`registry.py` contains a character Claude's tool-calling
  API rejects — most notably `.`. Tool names here are `snake_case` (`jira_get_...`,
  `github_get_...`), not dotted namespaces, specifically because of this constraint. This class
  of bug is invisible in the test suite (`test_orchestrator.py`/`test_integration.py` mock the
  Anthropic client entirely) — it only surfaces on a real `/ask` call against the live API.

**A tool call fails with a warning like `jira_get_...: KeyError: request failed`, with no other
detail (by design — see `sanitize_error()`):**
- Likely a Jira/GitHub response shape mismatch, not a real network failure. In particular:
  Jira Cloud's `/rest/api/3/search/jql` endpoint (the current, non-deprecated search endpoint)
  returns bare `{"id": "..."}` issue objects — no `key`, no `fields` — unless the request
  explicitly passes a `fields` param (see `_ISSUE_FIELDS` in `app/clients/jira_client.py`). This
  is real Atlassian API behavior, not a bug in this codebase's assumptions about Jira — but
  since every test mocks `JiraClient.search()` at the boundary, no test would catch a mismatch
  between the mocked shape and what Jira actually returns. When adding new Jira fields to
  `Issue`, verify against a live call (`curl localhost:8000/ask ...` with real credentials), not
  just the mocked test suite.

**`get_persons_open_issues` (or anything else calling `JiraClient.search_users`) fails to parse
a response, or a `["values"]`-style unwrap raises `KeyError`/`TypeError`:**
- Unlike every other Jira endpoint this codebase calls (`search`, `get_boards`, `_get_sprints`,
  all of which wrap results in `{"values": [...]}` or `{"issues": [...]}`),
  `GET /rest/api/3/user/search` returns a **bare JSON array** at the top level. `search_users()`
  deliberately does not unwrap anything — if a future change to this method adds a `["values"]`
  lookup by analogy with the other client methods, that's the bug to look for.

**The daily Slack digest doesn't seem to be posting, or posted once and then stopped:**
- `launchd` plists (per the README's Slack Integration section) live in
  `~/Library/LaunchAgents/`. Confirm the job is actually loaded:
  `launchctl list | grep com.devhelptool.digest` — no output means it isn't loaded
  (`launchctl load ~/Library/LaunchAgents/com.devhelptool.digest.plist`).
- Check the log file the plist's `StandardOutPath`/`StandardErrorPath` point at — every run
  (success or failure) logs there, including `scripts/post_digest.py`'s own error logging for a
  missing `SLACK_CHANNEL_ID`/`SLACK_BOT_TOKEN` or a failed `chat.postMessage` call.
- A missing or incorrect `SLACK_CHANNEL_ID` doesn't crash silently — `post_digest.py` exits
  non-zero with a explicit "must both be set" error before ever calling Slack, specifically so
  an unattended, `launchd`-triggered run never looks identical to "ran fine, nothing to report."
- The script has no retry logic by design (see `CLAUDE.md`'s Slack Integration section) — a
  failed run just waits for `launchd`'s next scheduled trigger the following day.

**Slack buttons (digest Quick Links, the standup-summary prompt) do nothing when clicked, with
no error visible anywhere:**
- Check that **Interactivity & Shortcuts** is toggled on for the Slack app at
  [api.slack.com/apps](https://api.slack.com/apps) — this is separate from Socket Mode (no
  Request URL needs filling in) and is easy to miss since Socket Mode alone is enough for
  `app_mention` events to work. Without it, Slack never delivers `block_actions` payloads to
  the socket connection at all, so `bolt_app.py`'s `@slack_app.action(...)` handlers never fire
  and nothing in the app's own logs will show a failure.

---

## Dependencies

Runtime (see `pyproject.toml` for the authoritative list):
- `fastapi` + `uvicorn` — HTTP service
- `anthropic` — Claude API client, used only in `app/agent/`
- `httpx` — async HTTP client for Jira/GitHub, used only in `app/clients/`
- `pydantic` + `pydantic-settings` — data models and `.env`-backed settings
- `slack-bolt` + `aiohttp` — Slack Socket Mode app and its async transport, used only in
  `app/slack/` and `scripts/post_digest.py`

Dev only:
- `pytest` + `pytest-asyncio` — test runner
- `respx` — mocks `httpx` calls in tests; no test hits a real network call
- `ruff`, `mypy` — lint and type checking

No `jira`/`PyGithub`/`atlassian-python-api` wrapper packages are used, deliberately — see
spec §5 for the reasoning (direct API control, consistent async story, easier to mock).

---

## Task Management

This project does **not** use STM ("Simple Task Master") despite earlier scaffolding
referencing it — `stm` is not an installable package (`brew install anthropic/tap/stm` and
`npm install -g @anthropic/stm` both 404; the tap/package don't exist publicly). Task tracking
for this project's implementation used the session's built-in task tool instead.

The implementation task breakdown itself is durable and lives in
`specs/feat-engineering-productivity-agent-mvp-tasks.md` — treat that file as the source of
truth for what was built and why, independent of whatever session-local task tracker is active.

### Workflow Integration

1. **Specification Decomposition**: `/spec:decompose <spec-file>` to break a spec into tasks
2. **Task Execution**: `/spec:execute` to implement decomposed tasks
3. **Progress Tracking**: whatever task tool is actually available in the session (check before
   assuming STM)

---

## Deployment

Not applicable. This is a local-only, single-user tool by design — no authentication, no
deployment target, no CI/CD. See spec §4 (Non-Goals) and §10 (Security Considerations) for the
explicit reasoning; this is a stated scope boundary, not an oversight.

---

## Available Commands

This project includes Claude Code slash commands for common workflows:

### Specification Workflow
- `/spec:ideate <topic>` - Structured ideation with documentation
- `/spec:create <description>` - Generate a feature specification
- `/spec:validate <path>` - Validate spec completeness
- `/spec:decompose <path>` - Break spec into implementable tasks
- `/spec:execute` - Implement decomposed tasks

### Code Quality
- `/code-review` - Multi-aspect code review
- `/validate-and-fix` - Run quality checks and auto-fix issues

### Git Workflow
- `/git:commit` - Create commit following project conventions
- `/git:status` - Analyze current git state
- `/checkpoint:create` - Create a git stash checkpoint
- `/checkpoint:restore` - Restore from checkpoint

### Research & Context
- `/task-context <brief>` - Quick context discovery for a task
- `/preflight-discovery <brief>` - Comprehensive discovery workflow
- `/research <question>` - Deep research with citations

### Documentation
- `/create-dev-guide <area>` - Write a developer guide
- `/docs:sync` - Update documentation with recent changes

---

## Skills

No project-specific skills exist yet. Skills would live in `.claude/skills/` as structured
`.md` knowledge bases (e.g. Jira/GitHub API quirks discovered during development); add one if
a piece of domain knowledge needs to persist across sessions beyond what's already in this
file and the specs.

---

## Getting Help

- Check `specs/feat-engineering-productivity-agent-mvp.md` and the task breakdown for
  architectural intent before making design changes.
- This repo's `.claude/agents/` roster is mostly TypeScript/React/Node-oriented (inherited from
  the starter kit this project began from) and doesn't include a Python specialist — for
  Python-specific work, direct implementation or the general-purpose agent is more useful than
  reaching for a mismatched specialist.
- `code-review-expert` is language-agnostic and has been used effectively for reviewing this
  codebase.
