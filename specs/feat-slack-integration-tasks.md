# Task Breakdown: Slack Integration
Generated: 2026-08-13
Source: specs/feat-slack-integration.md

## Overview

Add Slack as a second interface to DevHelpTool: an @-mention handler (Socket Mode,
reuses the existing orchestrator) and a daily digest (a standalone script, triggered by
`launchd`, with no dependency on the FastAPI process being alive). Ten tasks across three
phases; Phase 1 (the digest) is fully buildable and testable without touching
`app/main.py` or adding Socket Mode at all.

Per this project's task-management convention (`CLAUDE.md` "Task Management" — STM isn't
an installable package, confirmed again via `claudekit status stm` → `Not installed`), this
breakdown is tracked via the session's built-in task tool rather than STM.

---

## Phase 1: Digest (no Socket Mode required)

### Task 1.1: Add Slack config fields and dependency
**Description**: Add `slack-bolt` to `pyproject.toml` dependencies; add optional Slack settings to `app/config.py`; document new env vars in `.env.example`.
**Size**: Small
**Priority**: High
**Dependencies**: None
**Can run parallel with**: none (foundation for everything else)

**Source**: specs/feat-slack-integration.md §5, §6.3

**Technical Requirements**:
- `pyproject.toml` `[project] dependencies`: add `"slack-bolt"` (pulls in `slack-sdk` and `aiohttp` transitively — no separate pin needed for those).
- `app/config.py`: extend `Settings` with three **optional** fields (must default to `None` so the app boots identically to today when Slack isn't configured):
  ```python
  class Settings(BaseSettings):
      # ... existing fields unchanged ...
      slack_bot_token: str | None = None
      slack_app_token: str | None = None
      slack_channel_id: str | None = None
  ```
- `.env.example`: add, each with a short comment on where to find it in Slack's app management UI:
  ```
  SLACK_BOT_TOKEN=       # xoxb-... from OAuth & Permissions
  SLACK_APP_TOKEN=       # xapp-... from Basic Information > App-Level Tokens (Socket Mode)
  SLACK_CHANNEL_ID=      # channel or DM id the digest posts to
  ```

**Acceptance Criteria**:
- [ ] App boots with no Slack env vars set (existing `test_config.py` tests still pass unmodified).
- [ ] `Settings` accepts all three Slack fields as optional strings.
- [ ] `test_config.py` gets a new test asserting `Settings(...)` without Slack fields still constructs successfully, and one asserting they're picked up from env when present.
- [ ] `.env.example` documents all three vars.

---

### Task 1.2: Add `Issue.description` field and Jira `description` fetch
**Description**: Add a `description` field to the `Issue` model and fetch it from Jira, plus a helper to detect an empty/ADF-empty description.
**Size**: Small
**Priority**: High
**Dependencies**: None
**Can run parallel with**: Task 1.1

**Source**: specs/feat-slack-integration.md §6.4

**Technical Requirements**:
- `app/core/models.py`:
  ```python
  class Issue(BaseModel):
      key: str
      summary: str
      priority: str
      status: str
      due_date: date | None
      description: str | None = None   # new — used to detect "needs detail" backlog tickets
      has_linked_pr: bool
      linked_pr: "PullRequest | None" = None
      priority_score: float
  ```
- `app/clients/jira_client.py`: add `description` to the fetched fields:
  ```python
  _ISSUE_FIELDS = "summary,priority,status,duedate,description"
  ```
- Jira's `/rest/api/3/search/jql` returns `description` in Atlassian Document Format (ADF,
  a structured JSON object) when populated, not a plain string. `_map_issue()` in
  `app/tools/jira_tools.py` needs an "is this description empty" check that treats both
  `None` and a structurally-empty ADF doc (e.g. `{"type": "doc", "content": []}` or a doc
  whose only content is empty paragraphs) as empty. No ADF-to-plain-text parser is needed —
  only presence/absence matters for this feature, not the description's actual content.
  Store the raw description on `Issue.description` as `None` when empty per this check,
  otherwise as the raw ADF structure serialized however is simplest (a `str`/`dict` is fine
  since nothing downstream renders it — only `is None` is checked in Task 1.5).

**Acceptance Criteria**:
- [ ] `Issue.description` field exists and is optional.
- [ ] `_ISSUE_FIELDS` includes `description`.
- [ ] A helper function (e.g. `_is_empty_description(raw_description) -> bool`) correctly
      classifies: `None` → empty; `{}` → empty; a genuinely empty ADF doc → empty; an ADF
      doc with real paragraph text → not empty. Test all four cases explicitly — this is
      exactly the kind of Jira-response-shape assumption that CLAUDE.md's Troubleshooting
      section warns is invisible to a fully-mocked test suite if the fixture shape is wrong,
      so the test fixtures for "empty ADF doc" must match Jira's actual real shape, not a
      guessed one.
- [ ] Existing `test_jira_tools.py`/`test_jira_client.py` fixtures updated to include
      `description` in their raw issue dicts (even if unused by existing tests) so no
      existing test silently breaks on a `KeyError`.

---

### Task 1.3: Implement `get_lower_priority_issues_due_soon`
**Description**: New Jira tool: medium/low priority issues assigned to the user due within the next 7 days.
**Size**: Small
**Priority**: High
**Dependencies**: Task 1.2 (needs the same `_map_issue`/`ToolResult` conventions, no code dependency but logically follows)
**Can run parallel with**: Task 1.4, Task 1.5

**Source**: specs/feat-slack-integration.md §6.5

**Technical Requirements**:
```python
async def get_lower_priority_issues_due_soon(days: int = 7) -> ToolResult[list[Issue]]:
    jql = (
        "assignee = currentUser() AND priority in (Medium, Low, Lowest) "
        'AND duedate >= startOfDay() AND duedate <= startOfDay("+7d") '
        "AND resolution = Unresolved ORDER BY duedate ASC"
    )
    try:
        raw_issues = await jira_client.search(jql)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [_map_issue(raw) for raw in raw_issues]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)
```
Follow the exact existing pattern of `get_my_high_priority_issues` in
`app/tools/jira_tools.py` (same file) — own JQL, `try`/`except Exception` →
`ToolResult(ok=False, ...)`, zero arguments in the public tool-callable form (the `days`
parameter defaults to 7 and is not exposed as an LLM tool parameter — this function is
called directly by the digest script, not registered in `agent/schemas.py`/`registry.py`,
since the digest doesn't go through the LLM tool-calling loop at all).

**Acceptance Criteria**:
- [ ] Function added to `app/tools/jira_tools.py`.
- [ ] Test mirrors `test_get_my_high_priority_issues_maps_and_ranks`: mock `jira_client.search`, assert the JQL string contains `priority in (Medium, Low, Lowest)` and the `startOfDay()`/`"+7d"` date-window clauses, and assert returned issues are ranked.
- [ ] Test for the failure path (mirrors `test_get_my_high_priority_issues_returns_error_result_on_failure`).
- [ ] **Not** added to `app/agent/schemas.py` or `app/agent/registry.py` — this tool is digest-only, not LLM-callable (confirm no `TOOL_SCHEMAS` count change in `test_agent_registry.py` from this task).

---

### Task 1.4: Implement `get_current_sprint_issues`
**Description**: New Jira tool: all unresolved issues assigned to the user in any currently-active sprint, across all boards.
**Size**: Medium
**Priority**: High
**Dependencies**: None (uses existing `JiraClient.get_active_sprints`/`get_boards`)
**Can run parallel with**: Task 1.3, Task 1.5

**Source**: specs/feat-slack-integration.md §6.5

**Technical Requirements**:
Built the same way as the existing `get_incomplete_issues_from_last_sprint` in
`app/tools/jira_tools.py`, but against `get_active_sprints()` instead of
`get_closed_sprints()`:
```python
async def get_current_sprint_issues() -> ToolResult[list[Issue]]:
    try:
        boards = await jira_client.get_boards()
        active_sprints: list[dict] = []
        for board in boards:
            active_sprints.extend(await jira_client.get_active_sprints(board["id"]))

        if not active_sprints:
            return ToolResult(ok=True, data=[], error=None)

        sprint_ids = ",".join(str(s["id"]) for s in active_sprints)
        raw_issues = await jira_client.search(
            f"sprint in ({sprint_ids}) AND {_UNRESOLVED_JQL}"
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [_map_issue(raw) for raw in raw_issues]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)
```
Note: unlike `get_incomplete_issues_from_last_sprint`, an empty active-sprint list here is
**not** ambiguous the way "no closed sprint" was (that triggered the `note`/"did you mean"
feature) — "no active sprint right now" has exactly one meaning, so this returns a plain
empty `ToolResult(ok=True, data=[], error=None)` with no `note`. Reuses the module-level
`_UNRESOLVED_JQL` constant already defined in `jira_tools.py`.

Known live-testing gap (per spec §13 Open Question #2, not a blocker for this task): the
multi-board/multi-active-sprint union path (`sprint_ids` with more than one id) is
unverified against a real Jira instance, since the user's project has never had a sprint at
all yet. Mocked tests below still need to cover this path explicitly since it's real code
that must be correct even though it can't be live-verified yet.

**Acceptance Criteria**:
- [ ] Function added to `app/tools/jira_tools.py`.
- [ ] Test: single board, one active sprint → issues returned, JQL contains `sprint in (<id>)`.
- [ ] Test: no active sprint on any board → `ok=True, data=[], error=None`, no `note`.
- [ ] Test: two boards each with an active sprint → JQL's `sprint in (...)` contains both ids (covers the union path called out as a live-testing gap above).
- [ ] Test for the failure path (boards/sprints/search all independently can raise).

---

### Task 1.5: Implement `get_backlog_issues_needing_details`
**Description**: New Jira tool: unresolved, not-yet-sprinted issues the user created or is assigned to, that have a summary but an empty description.
**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.2 (needs the empty-description helper)
**Can run parallel with**: Task 1.3, Task 1.4

**Source**: specs/feat-slack-integration.md §6.5, §13 (Open Question #1, resolved 2026-08-13)

**Technical Requirements**:
Identity scope decided 2026-08-13: tickets the user either created or is assigned to, not
project-wide — catches both "I filed this stub and need to flesh it out" and "this was
assigned to me half-written":
```python
async def get_backlog_issues_needing_details() -> ToolResult[list[Issue]]:
    jql = (
        "(reporter = currentUser() OR assignee = currentUser()) "
        "AND sprint is EMPTY AND resolution = Unresolved "
        "ORDER BY created ASC"
    )
    try:
        raw_issues = await jira_client.search(jql)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    issues = [
        _map_issue(raw)
        for raw in raw_issues
        if _is_empty_description(raw["fields"].get("description"))
    ]
    ranked = rank(issues, lambda i: i.priority_score)
    return ToolResult(ok=True, data=ranked, error=None)
```
The empty-description filter is applied **client-side in Python**, not as a JQL clause —
JQL has no reliable "field is empty" operator for rich-text fields across all Jira
configurations, so fetch broadly (all backlog issues matching the identity clause) and
filter with the `_is_empty_description` helper from Task 1.2.

**Acceptance Criteria**:
- [ ] Function added to `app/tools/jira_tools.py`.
- [ ] Test: JQL asserted to contain `reporter = currentUser() OR assignee = currentUser()` and `sprint is EMPTY`.
- [ ] Test: given a mix of raw issues with empty and non-empty descriptions, only the empty-description ones appear in the result.
- [ ] Test for the failure path.

---

### Task 1.6: Implement `app/slack/digest.py` — pure section-selection logic
**Description**: Build `build_digest()`, the pure function that turns the five source lists into a `Digest` with `high_priority`/`upcoming` sections, including the "Nothing due soon" explicit-empty state and de-duplication.
**Size**: Medium
**Priority**: High
**Dependencies**: None (takes plain `list[Issue]` in — no dependency on Tasks 1.3–1.5 being wired up yet, just on `Issue` existing)
**Can run parallel with**: Task 1.7

**Source**: specs/feat-slack-integration.md §6.6

**Technical Requirements**:
New module `app/slack/digest.py`. **No import of `slack_bolt`, `slack_sdk`, or any HTTP
client** — mirrors the existing "tools never import anthropic" separation
(`CLAUDE.md`), independently unit-testable, plain-data-in/plain-data-out:
```python
from dataclasses import dataclass

from app.core.models import Issue


@dataclass
class DigestSection:
    heading: str
    items: list[Issue]


@dataclass
class Digest:
    high_priority: DigestSection
    upcoming: DigestSection


def _dedupe_by_key(issues: list[Issue], exclude_keys: set[str] | None = None) -> list[Issue]:
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
) -> Digest:
    if high_priority_issues:
        return Digest(
            high_priority=DigestSection("High Priority", high_priority_issues),
            upcoming=DigestSection("Upcoming", current_sprint_lower_priority),
        )

    if due_soon_lower_priority:
        upcoming = _dedupe_by_key(
            current_sprint_remainder + backlog_needing_details,
            exclude_keys={i.key for i in due_soon_lower_priority},
        )
        return Digest(
            high_priority=DigestSection("High Priority", due_soon_lower_priority),
            upcoming=DigestSection("Upcoming", upcoming),
        )

    upcoming = _dedupe_by_key(current_sprint_remainder + backlog_needing_details)
    return Digest(
        high_priority=DigestSection("Nothing due soon", []),
        upcoming=DigestSection("Upcoming", upcoming),
    )
```

This function is where the branching logic the user specified lives, encoded exactly:
- High-priority present → High Priority = those issues; Upcoming = current-sprint
  lower-priority issues only.
- High-priority absent, due-soon-lower-priority present → High Priority = the due-soon
  fallback items; Upcoming = deduped union of sprint remainder + backlog-needing-details,
  excluding anything already shown in High Priority.
- Both absent → High Priority heading is literally `"Nothing due soon"` with an empty item
  list (never an omitted/missing section); Upcoming = deduped union of the remaining two
  sources.

**Acceptance Criteria**:
- [ ] `build_digest` implemented exactly as above (or behaviorally equivalent).
- [ ] Test: high-priority present → High Priority gets those issues; Upcoming gets exactly `current_sprint_lower_priority`.
- [ ] Test: high-priority absent, due-soon present → High Priority gets the due-soon items; Upcoming is the deduped union of the other two sources, excluding due-soon keys.
- [ ] Test: both absent → `high_priority.heading == "Nothing due soon"` and `high_priority.items == []` — this is the specific case flagged in the spec as "the one place this feature could silently regress into an ambiguous empty result."
- [ ] Test: an issue present in both `current_sprint_remainder` and `backlog_needing_details` appears exactly once in `upcoming.items` (dedup correctness).
- [ ] Test: both `current_sprint_remainder` and `backlog_needing_details` empty → `upcoming.items == []` (distinguishes "nothing to dedupe" from "dedupe swallowed everything").

---

### Task 1.7: Implement `app/slack/formatting.py` — Block Kit rendering
**Description**: Render a `Digest` (from Task 1.6) and an `AskResponse` (existing model) as Slack Block Kit `blocks`.
**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.6 (needs `Digest`/`DigestSection` types)
**Can run parallel with**: Task 1.6 is a hard dependency, but this can start once 1.6's types are defined even before 1.6's logic is fully tested

**Source**: specs/feat-slack-integration.md §6.7

**Technical Requirements**:
New module `app/slack/formatting.py`. Block Kit `section` blocks whose `text` is `mrkdwn`
containing bullet lines (not one block per issue) — bullets within a section satisfies the
"formatted in bullets" requirement while staying within Block Kit structure. Include a
top-level fallback `text` for notifications, per Slack's own best practice for
accessibility.

Target rendering (example, for illustration — the actual Jira base URL comes from
`settings.jira_base_url`):
```
*High Priority*
• <https://yoursite.atlassian.net/browse/AL-4|AL-4> — Fix auth token refresh (overdue)
• <https://yoursite.atlassian.net/browse/AL-7|AL-7> — Add rate limiting

*Upcoming*
• <https://yoursite.atlassian.net/browse/AL-9|AL-9> — Refactor cache layer
• <https://yoursite.atlassian.net/browse/AL-12|AL-12> — needs a description before work can start
```

Two functions:
- `format_digest(digest: Digest) -> list[dict]` — returns the Block Kit `blocks` list. When
  a section's `items` is empty and its `heading == "Nothing due soon"`, render that line
  verbatim as the section's text instead of an empty bullet list or an omitted section —
  same "don't let an empty result read as ambiguous" principle behind `ToolResult.note`
  elsewhere in this codebase, applied to presentation rather than tool output. A
  legitimately empty "Upcoming" section (no heading override) should render as a short
  explicit "Nothing else on deck" line, not silently vanish either — for consistency with
  the same anti-ambiguity principle.
- `format_ask_response(response: AskResponse) -> list[dict]` — wraps the narrated
  `response.answer` string in a single Block Kit section block. No bullet restructuring
  here, since this is still LLM-narrated prose from the existing `/ask` path, not
  structured section data.

Each bullet line for an `Issue` should be `<{jira_base_url}/browse/{key}|{key}> — {summary}`,
optionally with an `(overdue)` suffix when `due_date` is in the past (reuse the same
overdue concept `core/ranking.py:score_issue` already uses, but only for display text here
— do not re-derive scoring logic in this module).

**Acceptance Criteria**:
- [ ] `format_digest` renders both sections as bullet lists using real `<url|KEY>` links.
- [ ] Test: `"Nothing due soon"` heading with empty items renders that literal text, not an empty bullet block — assert on actual block structure/text content, not just "some string appears somewhere," since a formatting regression that keeps a substring but breaks Slack's `<url|text>` link syntax should fail this test.
- [ ] Test: a legitimately empty (non-"Nothing due soon") Upcoming section renders an explicit "nothing else on deck"-style line rather than an empty/missing block.
- [ ] Test: `format_ask_response` wraps a known `AskResponse.answer` string in a single section block.
- [ ] Test: an overdue issue's bullet line includes the overdue indicator; a non-overdue issue's doesn't.

---

### Task 1.8: Implement `scripts/post_digest.py` — standalone script
**Description**: The `launchd`-triggered entry point that assembles and posts the daily digest, independent of the FastAPI process.
**Size**: Large
**Priority**: High
**Dependencies**: Tasks 1.1, 1.3, 1.4, 1.5, 1.6, 1.7 (needs config, all three new tools, digest logic, and formatting)
**Can run parallel with**: none (integration point for Phase 1)

**Source**: specs/feat-slack-integration.md §6.9, §6.10, §11

**Technical Requirements**:
`scripts/post_digest.py` — a short-lived script, not part of the `app` package's runtime
import graph in the reverse direction (it imports `app.*`, nothing in `app/` imports it):

1. Imports `app.tools.jira_tools` (for `get_my_high_priority_issues`,
   `get_lower_priority_issues_due_soon`, `get_current_sprint_issues`,
   `get_backlog_issues_needing_details`), `app.slack.digest.build_digest`,
   `app.slack.formatting.format_digest`, `app.config.settings`. No HTTP call to the
   running FastAPI app — direct function calls only.
2. Requires `slack_bot_token` and `slack_channel_id` to be set; exits with a clear
   non-zero-status error (not a silent no-op) if either is missing, since this runs
   unattended via `launchd` and a silent no-op would look identical to "ran fine, nothing
   to report."
3. Calls the four source tools. Per §6.10's error-handling table: each tool call's
   `ToolResult.ok` is checked independently — a failed tool contributes an empty list to
   `build_digest()` and its sanitized error is logged, rather than the whole script
   crashing. This mirrors `/ask`'s existing partial-failure philosophy (`dispatch()` in
   `app/agent/orchestrator.py`), applied here without an orchestrator in the loop.
4. For the "current sprint, lower priority" input to `build_digest()`
   (`current_sprint_lower_priority`) and the "current sprint, remainder" input
   (`current_sprint_remainder`): both derive from the single `get_current_sprint_issues()`
   call — `current_sprint_lower_priority` is that result filtered to
   `priority in {"Medium", "Low", "Lowest"}`, and `current_sprint_remainder` is the same
   filtered result again (the two `build_digest` branches are mutually exclusive per
   Task 1.6's logic, so computing this filter once and reusing it for whichever branch
   fires is correct — no need to call the tool twice).
5. Passes the five lists into `build_digest()`, formats the result via `format_digest()`,
   and posts via:
   ```python
   from slack_sdk.web.async_client import AsyncWebClient

   client = AsyncWebClient(token=settings.slack_bot_token)
   await client.chat_postMessage(
       channel=settings.slack_channel_id,
       blocks=blocks,
       text=fallback_text,  # short summary for notifications, per §6.7
   )
   ```
6. Per §6.10: if `chat_postMessage` itself fails (bad token, network), log the error and
   exit non-zero — no in-script retry loop; `launchd` will simply run again the next
   scheduled day.
7. Script should be runnable as `.venv/bin/python scripts/post_digest.py` with the repo
   root as working directory (this is the exact invocation `launchd`'s plist will use, per
   Task 3.1).

**Acceptance Criteria**:
- [ ] Script defined with a `main()` (or equivalent) callable from tests without executing at import time (`if __name__ == "__main__":` guard).
- [ ] Integration test (parallel to `app/tests/test_integration.py`'s existing pattern): mocks Jira/GitHub via `respx`, mocks `AsyncWebClient.chat_postMessage` via `AsyncMock`, runs the script's `main()`, and asserts the posted `blocks` payload reflects the mocked data end-to-end. This is explicitly the test the spec calls out as "the one that would have caught the tool-name and Jira-`fields`-param bugs from the original MVP build if an equivalent had existed then" — it must exercise the real `app.tools.*` functions, not mocks of them, the same way `test_integration.py` exercises the real orchestrator/tools/clients together.
- [ ] Test: missing `slack_channel_id` → script exits non-zero with a clear message, doesn't silently no-op.
- [ ] Test: one tool call failing (e.g. `get_current_sprint_issues` raises) → digest still posts using the other three sources' data, with the failure logged (mirrors `/ask`'s partial-degradation behavior).
- [ ] Test: `chat_postMessage` itself failing → script exits non-zero, no retry loop.

---

## Phase 2: @-mention

### Task 2.1: Implement `app/slack/bolt_app.py`
**Description**: Slack Bolt `AsyncApp` with an `app_mention` event handler that calls the existing orchestrator and replies in-thread.
**Size**: Medium
**Priority**: Medium
**Dependencies**: Task 1.1 (config), Task 1.7 (`format_ask_response`)
**Can run parallel with**: Task 2.2 is a light dependency (bolt_app needs to be importable before main.py wires it up, but can be developed/tested independently)

**Source**: specs/feat-slack-integration.md §6.8

**Technical Requirements**:
```python
from slack_bolt.async_app import AsyncApp

from app.agent.orchestrator import OrchestratorUnavailable, handle_query
from app.config import settings
from app.slack.formatting import format_ask_response

slack_app = AsyncApp(token=settings.slack_bot_token)


def _strip_bot_mention(text: str) -> str:
    """Strip the leading <@BOTID> mention token Slack includes in app_mention text."""
    return re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text)


@slack_app.event("app_mention")
async def handle_mention(event: dict, say) -> None:
    query = _strip_bot_mention(event["text"])
    try:
        response = await handle_query(query)
    except OrchestratorUnavailable:
        await say(text="Sorry, I couldn't reach the assistant just now.", thread_ts=event["ts"])
        return
    await say(blocks=format_ask_response(response), thread_ts=event["ts"])
```
Note: `slack_app` is only constructed (and only imported by `app/main.py`'s lifespan
handler in Task 2.2) when Slack is configured — guard the `AsyncApp(token=...)`
construction so it isn't attempted with `token=None` when Slack env vars are absent.

**Acceptance Criteria**:
- [ ] `handle_mention` strips the bot's own `<@BOTID>` mention prefix from `event["text"]` before passing it to `handle_query`.
- [ ] Test: a successful `handle_query` result → `say` called with `blocks=format_ask_response(response)` and `thread_ts` matching the triggering event's `ts` (reply lands in-thread, not as a new top-level message).
- [ ] Test: `handle_query` raising `OrchestratorUnavailable` → `say` called with the apology text and the correct `thread_ts`, not a stack trace or silent failure — mirrors `test_orchestrator.py`'s `test_anthropic_api_error_raises_orchestrator_unavailable` pattern, but asserts on the Slack-side response instead of the raised exception, since this layer's job is to catch it, not propagate it.
- [ ] `_strip_bot_mention` tested directly against a realistic `<@U012ABCDEF> what should I work on today?` input.

---

### Task 2.2: Wire Socket Mode into FastAPI's lifespan
**Description**: Start/stop the Slack Socket Mode connection alongside `uvicorn`, only when Slack is configured.
**Size**: Small
**Priority**: Medium
**Dependencies**: Task 2.1
**Can run parallel with**: none (final integration point for Phase 2)

**Source**: specs/feat-slack-integration.md §6.8

**Technical Requirements**:
`app/main.py` gains a `lifespan` context manager (FastAPI's current recommended pattern,
replacing the deprecated `@app.on_event`) that starts `AsyncSocketModeHandler` on startup
and disconnects it on shutdown — **only if** `settings.slack_bot_token` and
`settings.slack_app_token` are both set; otherwise the app boots exactly as it does today,
with a log line noting Slack is disabled:
```python
from contextlib import asynccontextmanager

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from app.slack.bolt_app import slack_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    handler: AsyncSocketModeHandler | None = None
    if settings.slack_bot_token and settings.slack_app_token:
        handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
        await handler.connect_async()
    else:
        logger.info("Slack integration disabled (SLACK_BOT_TOKEN/SLACK_APP_TOKEN not set)")

    yield

    if handler is not None:
        await handler.disconnect_async()


app = FastAPI(lifespan=lifespan)
```

**Acceptance Criteria**:
- [ ] With no Slack env vars set, the app starts and `/health` responds exactly as it does today — no behavior change (regression check against existing `test_api.py`).
- [ ] Test: with Slack env vars set (mocked `AsyncSocketModeHandler`), the lifespan handler calls `connect_async()` on startup and `disconnect_async()` on shutdown.
- [ ] Test: without Slack env vars set, `AsyncSocketModeHandler` is never constructed at all (not constructed-then-skipped — never touched).
- [ ] Existing full test suite (`pytest app/tests/`) still passes unmodified for every test that doesn't touch Slack.

---

## Phase 3: Polish

### Task 3.1: `launchd` scheduling documentation + README/CLAUDE.md updates
**Description**: Document how to schedule `scripts/post_digest.py` via `launchd`, and update project docs with the new Slack setup steps.
**Size**: Small
**Priority**: Low
**Dependencies**: Task 1.8 (script must exist to document its invocation)
**Can run parallel with**: Task 3.2

**Source**: specs/feat-slack-integration.md §11

**Technical Requirements**:
- `README.md`: setup instructions for creating a Slack app (Socket Mode enabled, bot token
  + app-level token generated, invited to a channel), plus a `launchd` `.plist` example
  with a `StartCalendarInterval` entry for the desired hour, invoking
  `.venv/bin/python scripts/post_digest.py` with the repo root as working directory
  (`WorkingDirectory` key in the plist).
- `CLAUDE.md`: new "Slack Integration" subsection under Common Patterns, mirroring the
  existing "Sprint Lookups" / "Issue ↔ PR Linking" subsections in style; new env vars added
  to the Environment Variables list; a new Troubleshooting entry covering: where `launchd`
  plists live (`~/Library/LaunchAgents/`), how to check whether the digest ran
  (`launchctl list | grep <label>`, log file location if the plist redirects
  stdout/stderr), and what a missing/incorrect `SLACK_CHANNEL_ID` looks like when the
  script fails.

**Acceptance Criteria**:
- [ ] README has a complete, followable Slack app setup section.
- [ ] README has a working example `.plist` file (correct keys: `Label`, `ProgramArguments`, `StartCalendarInterval`, `WorkingDirectory`, `StandardOutPath`/`StandardErrorPath`).
- [ ] CLAUDE.md's Common Patterns, Environment Variables, and Troubleshooting sections all updated.

---

### Task 3.2: Verify and document the multi-active-sprint live-testing gap
**Description**: Close out spec §13 Open Question #2 — confirm `get_current_sprint_issues()`'s multi-board union behavior is correctly covered by mocked tests (done in Task 1.4), and document that it's unverified live, in the same style as the existing "did you mean Sprint X" note feature's documented gap.
**Size**: Small
**Priority**: Low
**Dependencies**: Task 1.4
**Can run parallel with**: Task 3.1

**Source**: specs/feat-slack-integration.md §13 (Open Question #2)

**Technical Requirements**:
No new code — this is a documentation/verification task. Confirm Task 1.4's test suite
actually exercises the two-board/two-active-sprint union path (not just the single-board
case), and add a short note to `CLAUDE.md`'s Sprint Lookups subsection (or the new Slack
Integration subsection from Task 3.1) explicitly flagging that this path is verified only
via mocks, mirroring how the existing sprint-note feature's live-testing limitation was
communicated to the user rather than silently assumed correct.

**Acceptance Criteria**:
- [ ] Confirmed (not just asserted) that Task 1.4 includes a real multi-board test case.
- [ ] A short note added to the relevant CLAUDE.md subsection documenting the live-testing gap.

---

## Dependency Graph

```
1.1 (config) ──┬──────────────────────────────────────────► 1.8 (script) ──► 3.1
1.2 (description field) ──┬──► 1.3 (due-soon tool) ─────────► 1.8
                           ├──► 1.5 (backlog tool) ──────────► 1.8
                           │
1.4 (sprint tool) ─────────┴──────────────────────────────────► 1.8 ──► 3.2
                                                                  │
1.6 (digest logic) ──► 1.7 (formatting) ─────────────────────────┘

1.1 ──► 2.1 (bolt_app) ──► 2.2 (lifespan wiring)
1.7 ──► 2.1
```

## Execution Strategy

- **Parallelizable now**: 1.1, 1.2 can start immediately and in parallel; once 1.2 lands,
  1.3/1.4/1.5 can all proceed in parallel; 1.6 has no dependencies and can be built
  alongside 1.1–1.5.
- **Critical path**: 1.2 → (1.3, 1.4, 1.5) → 1.6 → 1.7 → 1.8 → 3.1/3.2.
- **Phase 2 is independently deferrable**: nothing in Phase 1 depends on Phase 2, so the
  digest can ship and run daily before the @-mention handler exists at all, per the spec's
  own phasing rationale.
- **Highest-value tests to get right first**: Task 1.6's `build_digest()` tests (pure
  function, no mocking needed, and the one place the spec explicitly calls out as a risk
  for silently regressing into an ambiguous empty state) and Task 1.8's end-to-end
  integration test (the one the spec says would have caught the original MVP's tool-name
  and Jira-`fields`-param bugs).
