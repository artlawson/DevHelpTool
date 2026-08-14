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
├── pyproject.toml                # dependencies, ruff/mypy/pytest config
├── .env.example                  # documents required env vars; .env itself is gitignored
│
├── app/
│   ├── main.py                   # FastAPI app: POST /ask, GET /health; Slack lifespan hook
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
│   │   │                         #   get_backlog_issues_needing_details
│   │   └── github_tools.py       # get_my_open_prs, get_prs_awaiting_my_review
│   ├── core/
│   │   ├── models.py             # Issue, PullRequest, ToolResult[T], AskResponse
│   │   ├── ranking.py            # score_issue(), score_pr() - pure, LLM-free
│   │   ├── cache.py              # TTLCache (in-memory, injectable clock for tests)
│   │   └── errors.py             # sanitize_error() - never leak raw exception text
│   ├── slack/
│   │   ├── bolt_app.py           # AsyncApp, app_mention handler (Socket Mode, @-mention path)
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

### Slack Integration

Two independent entry points, neither of which changes `/ask`'s behavior:

- **`app/slack/bolt_app.py`** (@-mention): a `slack_bolt.AsyncApp` running over Socket Mode
  (no public HTTPS endpoint or tunnel needed) inside the same process as `uvicorn`, started from
  `app/main.py`'s `lifespan` handler. `handle_mention` calls the existing
  `orchestrator.handle_query()` directly — no HTTP round-trip to the app's own `/ask` route —
  and replies in-thread. This whole module constructs a real `AsyncApp` at import time using
  `settings.slack_bot_token`, which raises if the token is falsy; `app/main.py` only imports it
  from inside the `if settings.slack_bot_token and settings.slack_app_token:` branch, specifically
  so the app boots identically to today when Slack isn't configured (the import is never reached).
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
  choice: a free-text follow-up ("yes, give me the short version") would need thread-scoped
  conversation memory (`thread_ts` → prior messages), which nothing in this codebase has — a
  button click carries enough intent on its own, no memory required. Three sections:
  `Doing` (issues from that fetch filtered to High/Highest priority — uncapped, so someone
  with 6 high-priority issues in flight sees all 6, not a truncated 4), `Reviewing` (PRs
  awaiting review, unchanged), `Next Up` (backfilled from the same ranked fetch, excluding
  whatever's already in `Doing`, only when `Doing + Reviewing` together total fewer than 2 -
  keeps a thin day from reading as a single bare bullet without padding a busy one). No tool
  currently tracks "completed since yesterday," so there's no `Done` section.
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

**`ruff`/`mypy`/`pytest` hooks fail with "command not found":**
- The `.venv` hasn't been created yet, or dependencies aren't installed — run the one-time
  setup under Local Development above. Hooks in `.claude/settings.json` call `.venv/bin/ruff`
  etc. directly (not the bare command), so they need the venv to exist at the repo root.

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
