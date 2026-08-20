# Slack Integration

**Status:** Draft
**Authors:** Claude Code, 2026-08-13
**Related:** `docs/ideation/slack-integration.md`, `specs/feat-engineering-productivity-agent-mvp.md` §4 (Non-Goals originally deferred Slack to post-MVP)

---

## 1) Overview

Add Slack as a second interface to DevHelpTool, alongside the existing `POST /ask` HTTP
API. Two capabilities:

1. **@-mention reply** — tagging the bot in a Slack channel runs the same natural-language
   query the app already answers via `/ask`, replying in-thread.
2. **Scheduled digest** — a daily, unattended Slack message summarizing what the user
   should focus on: a **High Priority** section and an **Upcoming** section, each with
   fallback behavior when there's nothing urgent.

Both are additive to the existing FastAPI service; neither changes `/ask`'s existing
behavior or orchestrator.

## 2) Background / Problem Statement

DevHelpTool today only answers questions when someone manually calls `POST /ask`. That's
fine for on-demand use, but it means getting a status update still requires remembering to
ask. Slack is where the user already works, so meeting them there — both for ad-hoc
questions and for a standing daily digest — removes the "remember to check" step entirely.

The project has been deliberately local-only and undeployed to date (`CLAUDE.md`
Deployment section, this spec's own §4/§10 in the original MVP spec). This feature has to
add Slack without reversing that stance: no public HTTPS endpoint, no cloud hosting, no new
always-on daemon requirement. `docs/ideation/slack-integration.md` worked through this
tension in detail; this spec carries forward its resolved decisions.

## 3) Goals

- Answer an @-mention in Slack using the existing orchestrator, with no change to how
  `/ask` behaves today.
- Post a daily digest to a configured Slack channel with two sections — **High Priority**
  and **Upcoming** — using deterministic, LLM-free logic consistent with the rest of this
  codebase's ranking approach.
- Do this without exposing a public HTTP endpoint or turning the app into an always-on
  service (see §9.9 on Socket Mode / the standalone digest script).
- Keep Slack fully optional: the app must run exactly as it does today if no Slack
  credentials are configured.
- Keep the digest's section-selection logic identity-agnostic (a plain function of "does
  this identity have open high-priority work"), so a future per-role digest (junior
  engineer/intern, who typically has no high-priority work assigned) is an extension, not
  a rewrite — **not implemented in this pass**, just not architecturally foreclosed.

## 4) Non-Goals

- Slack slash commands (e.g. `/standup`).
- Multi-user / multi-workspace support, per-Slack-user identity mapping, or a role/persona
  parameter for the digest (explicitly deferred — see Goals above and Open Questions #1).
- ~~Interactive Block Kit actions (buttons, "mark done", etc.) — this feature only sends
  messages, it never receives structured interactions back.~~ **Superseded 2026-08-14:**
  implemented post-MVP — digest Quick Links buttons (`Open in Jira`/`View PR`) and a
  standup-summary prompt on every `/ask` reply. See `CLAUDE.md`'s Slack Integration section
  for the buttons/`_ack_only`/standup-summary design detail.
- Any change to the project's local-only, no-authentication, no-CI/CD posture beyond what's
  strictly required to receive Slack events (Socket Mode) and run one scheduled script
  (`launchd`) — both chosen specifically because neither requires a public endpoint or a
  permanently-running process (see §9.9).
- Write/mutating operations against Jira or GitHub triggered from Slack.

## 5) Technical Dependencies

| Library | Purpose | Notes |
|---|---|---|
| `slack-bolt` | Slack app framework — `AsyncApp`, Socket Mode handler, `app_mention` event listener | Pulls in `slack-sdk` (provides `AsyncWebClient`, used for both the mention reply and the standalone digest script) and `aiohttp` (Socket Mode's transport) as transitive dependencies. Pin the latest stable release at implementation time (`pip index versions slack-bolt`); no version-specific API in this spec relies on a feature narrower than what's been stable for several releases. |

No new dependency is needed for scheduling — the chosen design (Option C, §9.9) uses the
OS-level scheduler (`launchd` on macOS) rather than an in-process scheduler like
APScheduler, so nothing new runs inside the FastAPI process for the digest.

## 6) Detailed Design

### 6.1 Architecture

```
                    ┌─────────────────────────────┐
                    │   FastAPI process (uvicorn)  │
                    │                               │
Slack @-mention ───▶│  Socket Mode (slack-bolt)     │
                    │        │                      │
                    │        ▼                      │
                    │  handle_query() (existing)     │
                    │        │                      │
                    │        ▼                      │
                    │  Slack reply (in-thread)       │
                    └─────────────────────────────┘

                    ┌─────────────────────────────┐
launchd (9am) ─────▶│  scripts/post_digest.py       │  (standalone, short-lived)
                    │   - imports app.tools.* directly
                    │   - app.slack.digest (section logic)
                    │   - app.slack.formatting (Block Kit)
                    │   - AsyncWebClient.chat_postMessage
                    └─────────────────────────────┘
```

The two entry points share `app/tools/*`, `app/core/ranking.py`, and the new
`app/slack/digest.py` / `app/slack/formatting.py` modules, but run independently — the
digest script has no dependency on the FastAPI process being alive, and the mention
handler has no dependency on the digest script.

### 6.2 File / Module Structure

```
app/
├── main.py                     # + lifespan handler starts/stops Socket Mode (if configured)
├── config.py                   # + optional slack_bot_token, slack_app_token, slack_channel_id
├── clients/
│   └── jira_client.py          # + "description" added to _ISSUE_FIELDS
├── core/
│   └── models.py               # + Issue.description: str | None = None
├── tools/
│   └── jira_tools.py           # + get_current_sprint_issues, get_lower_priority_issues_due_soon,
│                                #   get_backlog_issues_needing_details
├── slack/
│   ├── __init__.py
│   ├── bolt_app.py             # AsyncApp, @app.event("app_mention") handler
│   ├── digest.py                # pure section-selection logic (no Slack/HTTP knowledge)
│   └── formatting.py            # Issue/PullRequest -> Block Kit builders
└── tests/
    ├── test_slack_digest.py
    ├── test_slack_formatting.py
    └── test_slack_bolt_app.py

scripts/
└── post_digest.py               # launchd entry point; imports app.tools.* + app.slack.*
```

`app/slack/digest.py` deliberately has **no** import of `slack_bolt`/`slack_sdk` — it's a
plain-data-in, plain-data-out module (mirrors the existing "tools never import anthropic"
separation from `CLAUDE.md`), independently unit-testable and reusable if the delivery
mechanism ever changes.

### 6.3 Config (`app/config.py`)

```python
class Settings(BaseSettings):
    # ... existing fields unchanged ...
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_channel_id: str | None = None
```

All three are optional so the app boots identically to today when Slack isn't configured.
`app/main.py`'s lifespan handler only starts the Socket Mode connection when both
`slack_bot_token` and `slack_app_token` are set; `scripts/post_digest.py` requires all
three (bot token + channel id at minimum) and exits with a clear error if they're missing,
rather than silently no-op-ing on a scheduled job.

### 6.4 Data Model Changes (`app/core/models.py`)

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

`app/clients/jira_client.py`'s `_ISSUE_FIELDS` gains `description`:

```python
_ISSUE_FIELDS = "summary,priority,status,duedate,description"
```

Jira's `/rest/api/3/search/jql` returns `description` in Atlassian Document Format (ADF, a
structured JSON object), not a plain string, when populated — `_map_issue()` needs to treat
`fields.get("description")` as "empty" whenever it's `None` **or** an ADF object with no
meaningful text content (an empty doc still round-trips as a non-`None`, structurally
"empty" object). Extracting plain text isn't needed for the empty/non-empty check this
feature relies on — only presence/absence matters, not content — so no ADF parser is
required, just an "is this doc empty" check.

### 6.5 New Jira Tools (`app/tools/jira_tools.py`)

All three follow the existing tool contract (`ToolResult[list[Issue]]`, zero arguments,
own JQL, no LLM knowledge):

**`get_lower_priority_issues_due_soon(days: int = 7)`** — Section 1's fallback source.

```python
jql = (
    "assignee = currentUser() AND priority in (Medium, Low, Lowest) "
    'AND duedate >= startOfDay() AND duedate <= startOfDay("+7d") '
    "AND resolution = Unresolved ORDER BY duedate ASC"
)
```

**`get_current_sprint_issues()`** — built the same way as
`get_incomplete_issues_from_last_sprint`, but against `get_active_sprints()` instead of
`get_closed_sprints()`: gather active sprints across all boards, then

```python
jql = f"sprint in ({','.join(str(s['id']) for s in active_sprints)}) " \
      "AND assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
```

If no board has an active sprint, return `ToolResult(ok=True, data=[], error=None)` — an
empty sprint list here isn't ambiguous the way the *closed*-sprint case was (§ existing
`note` feature), since "no active sprint" has only one meaning.

**`get_backlog_issues_needing_details()`** — issues with a summary but an empty
description, not yet in any sprint. Identity scope decided 2026-08-13: tickets the user
either created or is assigned to, not project-wide — catches both "I filed this stub and
need to flesh it out" and "this was assigned to me half-written":

```python
jql = (
    "(reporter = currentUser() OR assignee = currentUser()) "
    "AND sprint is EMPTY AND resolution = Unresolved "
    "ORDER BY created ASC"
)
```

with the empty-description filter applied client-side in `_map_issue`'s caller (JQL has no
reliable "field is empty" operator for rich-text fields across all Jira configurations, so
this is a post-fetch Python filter, not a JQL clause).

### 6.6 Digest Section-Selection Logic (`app/slack/digest.py`)

Pure function, no Slack/HTTP/Jira-client imports — takes already-fetched `list[Issue]`s in,
returns a small result type out. This is the piece designed to stay identity-agnostic per
Goals (a future role/persona parameter would wrap this, not rewrite it):

```python
@dataclass
class DigestSection:
    heading: str
    items: list[Issue]

@dataclass
class Digest:
    high_priority: DigestSection
    upcoming: DigestSection

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

    # Nothing high priority, nothing due soon either.
    upcoming = _dedupe_by_key(current_sprint_remainder + backlog_needing_details)
    return Digest(
        high_priority=DigestSection("Nothing due soon", []),
        upcoming=DigestSection("Upcoming", upcoming),
    )
```

`_dedupe_by_key` is a small helper (dict-by-`key`, preserving first-seen order) so an issue
that's both "rest of current sprint" and "needs details" doesn't appear twice.

This function is where the "next-7-days" window and the branching rules the user specified
are encoded — it's the single place all of that logic lives, and the primary target for
unit tests (§11).

### 6.7 Message Formatting (`app/slack/formatting.py`)

Block Kit `section` blocks whose `text` is `mrkdwn` containing bullet lines (not one block
per issue) — matches the "formatted in bullets" requirement while staying within Block
Kit's structure (fallback `text` at the top level for notifications, per Slack's own best
practice):

```
*High Priority*
• <https://yoursite.atlassian.net/browse/AL-4|AL-4> — Fix auth token refresh (overdue)
• <https://yoursite.atlassian.net/browse/AL-7|AL-7> — Add rate limiting

*Upcoming*
• <https://yoursite.atlassian.net/browse/AL-9|AL-9> — Refactor cache layer
• <https://yoursite.atlassian.net/browse/AL-12|AL-12> — needs a description before work can start
```

When a section's `items` is empty and `heading == "Nothing due soon"`, render that line
verbatim instead of an empty bullet list or a missing section — this is the same "don't let
an empty result read as ambiguous" principle behind `ToolResult.note` elsewhere in this
codebase (`CLAUDE.md` Error Handling), applied to presentation instead of tool output.

`format_digest(digest: Digest) -> list[dict]` returns the Block Kit `blocks` list;
`format_ask_response(response: AskResponse) -> list[dict]` (for the @-mention path) wraps
the narrated `answer` string in a single section block — no bullet restructuring needed
there, since that's still LLM-narrated prose, not structured section data.

**Superseded 2026-08-14:** `format_ask_response` no longer just wraps the raw string as
written above. It now runs a deterministic enrichment pass — Markdown-to-mrkdwn
sanitization, plus real hyperlinking/priority-flagging of any Jira issue key or `PR #<n>`
mention using `AskResponse.referenced_issues`/`referenced_prs` (tool data the orchestrator
collected this turn, not parsed from the text) — and appends a standup-summary prompt with
buttons. See `CLAUDE.md`'s Slack Integration section for the full design and a real gotcha
(nested-link mangling) discovered building it.

### 6.8 @-Mention Handling (`app/slack/bolt_app.py`) + FastAPI Lifespan

```python
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

slack_app = AsyncApp(token=settings.slack_bot_token)

@slack_app.event("app_mention")
async def handle_mention(event: dict, say: AsyncSay) -> None:
    query = _strip_bot_mention(event["text"])
    try:
        response = await handle_query(query)
    except OrchestratorUnavailable:
        await say(text="Sorry, I couldn't reach the assistant just now.", thread_ts=event["ts"])
        return
    await say(blocks=format_ask_response(response), thread_ts=event["ts"])
```

`app/main.py` gains a `lifespan` context manager (FastAPI's current recommended pattern,
replacing the deprecated `@app.on_event`) that starts `AsyncSocketModeHandler` on startup
and disconnects it on shutdown, **only if** `slack_bot_token` and `slack_app_token` are
both set — otherwise the app boots exactly as it does today, with a log line noting Slack
is disabled.

### 6.9 Scheduled Digest — Standalone Script + `launchd`

Per `docs/ideation/slack-integration.md` Clarification #3 (decided: Option C): the digest
is **not** triggered by anything running inside the long-lived FastAPI process. Instead,
`scripts/post_digest.py` is a short-lived script that:

1. Imports `app.tools.jira_tools`, `app.tools.github_tools`, `app.slack.digest`,
   `app.slack.formatting` directly (no HTTP call to the running app, no dependency on it
   being up).
2. Calls the five source tools (`get_my_high_priority_issues`,
   `get_lower_priority_issues_due_soon`, `get_current_sprint_issues` twice — once
   unfiltered for the "high priority present" branch's Upcoming section, once as the
   "remainder" input — and `get_backlog_issues_needing_details`).
3. Passes their `.data` into `build_digest()`.
4. Formats the result via `format_digest()` and posts it with
   `AsyncWebClient(token=settings.slack_bot_token).chat_postMessage(channel=settings.slack_channel_id, blocks=..., text=...)`.
5. Exits. Any tool failure degrades the same way it does in `/ask` today — a failed source
   is logged and its contribution to the digest is simply empty, rather than the whole
   script crashing (reusing the `ToolResult.ok` check per call site, not a single top-level
   `try`).

Scheduling itself is an OS-level concern, documented (not code) — a `launchd` `.plist` with
a `StartCalendarInterval` entry for the desired hour, calling
`.venv/bin/python scripts/post_digest.py` with the repo root as working directory. This is
covered under §14 Documentation rather than as application code, since it's local machine
configuration, not something the Python package should manage.

### 6.10 Error Handling Summary

| Failure | Behavior |
|---|---|
| A Jira/GitHub tool call fails during digest assembly | That tool's contribution to the relevant section is empty (per existing `ToolResult(ok=False, ...)` degradation); script logs the sanitized error and continues — matches `/ask`'s partial-failure philosophy. |
| Slack `chat.postMessage` itself fails (bad token, network) | Script logs the error and exits non-zero; `launchd` will simply retry the next scheduled day — no in-script retry loop, since a missed digest isn't worth building retry infrastructure for. |
| `handle_query()` raises `OrchestratorUnavailable` during an @-mention | Bot replies in-thread with a short apology, mirroring `/ask`'s 503 behavior instead of leaving the mention unanswered. |
| Socket Mode connection drops | `slack-bolt`'s own reconnect logic handles this; no custom reconnect code needed. |

## 7) User Experience

**@-mention:** user types `@DevHelpTool what should I work on today?` in any channel the
bot's been invited to; bot replies in a thread under that message within a few seconds,
using the same narration style as `/ask` today.

**Digest:** once daily (time set via `launchd`, not app config), a message appears in the
configured channel/DM with two bulleted sections, always present even when empty
(`"Nothing due soon"` rather than a blank gap) — see §6.7 for the exact shape.

## 8) Testing Strategy

Following this repo's existing conventions (respx for HTTP-boundary mocking,
`monkeypatch`/`AsyncMock` for SDK-client mocking, one test file per source module):

- **`test_slack_digest.py`** — the highest-value tests in this feature, since
  `build_digest()` is pure and has no I/O to mock:
  - high-priority present → High Priority section gets those issues; Upcoming gets
    `current_sprint_lower_priority` only (validates: a real-work day looks the way the user
    described)
  - high-priority absent, due-soon-lower-priority present → High Priority section shows the
    fallback items; Upcoming is the deduped union of sprint remainder + backlog-needing-
    details (validates the full fallback branch, including dedup — an issue appearing in
    both `current_sprint_remainder` and `backlog_needing_details` must appear once)
  - high-priority absent, due-soon absent → High Priority heading is exactly
    `"Nothing due soon"` with an empty item list, not a missing/omitted section (validates
    the explicit-empty-state requirement from the ideation doc, the one place this feature
    could silently regress into an ambiguous empty result the way the sprint-note feature
    was built to avoid)
  - empty `current_sprint_remainder` and `backlog_needing_details` together → Upcoming
    section is legitimately empty (distinguishes "nothing to dedupe" from "dedupe swallowed
    everything," a real failure mode a naive test could miss)

- **`test_slack_formatting.py`** — `format_digest()` renders the `"Nothing due soon"` state
  as literal text rather than an empty bullet block; bullet lines contain the expected
  `<url|KEY>` link syntax for a known `Issue` fixture (not just "some string appears
  somewhere" — assert on the actual block structure, since a formatting regression that
  still contains the substring but breaks Slack's link syntax would slip past a weaker
  assertion).

- **`test_jira_tools.py` additions** — `get_current_sprint_issues`, `get_lower_priority_issues_due_soon`,
  `get_backlog_issues_needing_details`: mocked `jira_client.search`/`get_active_sprints`
  per the existing pattern in this file, asserting on the JQL string built (e.g. the 7-day
  window's date-function usage) the same way `test_get_incomplete_issues_from_last_sprint_picks_most_recent_sprint`
  already asserts on `mock_search.call_args`.

- **`test_slack_bolt_app.py`** — `handle_mention` calls `handle_query()` with the mention
  text stripped of the bot's own `<@BOTID>` prefix, replies in-thread (`thread_ts` matches
  the triggering event), and replies with the apology text (not a stack trace or silence)
  when `OrchestratorUnavailable` is raised — mirrors `test_orchestrator.py`'s
  `test_anthropic_api_error_raises_orchestrator_unavailable` pattern but asserts on the
  Slack-side response instead of the raised exception, since this layer's job is to catch
  it, not propagate it.

- **`scripts/post_digest.py`** — one integration-style test (parallel to
  `test_integration.py`'s existing end-to-end test) that mocks Jira/GitHub via respx and
  the Slack `AsyncWebClient.chat_postMessage` call via `AsyncMock`, running the script's
  `main()` and asserting the posted `blocks` payload reflects the mocked data — this is the
  test that would have caught the tool-name and Jira-`fields`-param bugs from the original
  MVP build if an equivalent had existed then, so it's included deliberately as the
  "wiring, not just units" check for this feature.

No test hits a real network call or a real Slack workspace, consistent with the rest of
this repo.

## 9) Performance Considerations

Negligible. Socket Mode is a single persistent WebSocket for one user's mentions — no
polling, no meaningful CPU/memory cost. The digest script runs once daily for a few
seconds and exits; it doesn't share the running app's in-memory TTL cache (each run is
cold), which is the correct behavior for a once-a-day job (see ideation doc Clarification
#3 resolution) rather than a performance concern.

## 10) Security Considerations

- `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` are added to `.env` (already gitignored) and
  `.env.example` (documented, not populated) — same pattern as existing Jira/GitHub
  credentials, no new secret-handling mechanism introduced.
- Socket Mode's outbound-only connection model means no inbound port is opened on the
  user's machine — smaller attack surface than the Events API + tunnel alternative
  considered and rejected in the ideation doc.
- Tool-call failures surfaced through Slack (either in an @-mention reply or the digest)
  must go through the same `sanitize_error()` path already used for `/ask` — raw exception
  text must never reach a Slack message, matching this repo's existing security stance
  (`CLAUDE.md` Error Handling, spec §10 in the original MVP spec).
- The bot should be scoped to the minimum Slack OAuth scopes needed:
  `app_mentions:read`, `chat:write`, and (only if DM delivery is ever added later)
  `im:write` — no scopes beyond what §6 actually uses.

## 11) Documentation

- `CLAUDE.md`: new "Slack Integration" subsection under Common Patterns (mirroring the
  existing "Sprint Lookups" / "Issue ↔ PR Linking" subsections) once implemented; new env
  vars added to the Environment Variables list; a new Troubleshooting entry for the
  `launchd` setup (plist location, how to check whether the digest ran, where its logs go).
- `README.md`: setup instructions for creating a Slack app (Socket Mode enabled, bot token
  + app-level token generated, invited to a channel), plus the `launchd` plist example for
  the digest schedule.
- `.env.example`: `SLACK_BOT_TOKEN=`, `SLACK_APP_TOKEN=`, `SLACK_CHANNEL_ID=`, each with a
  short comment on where to find it in Slack's app management UI.

## 12) Implementation Phases

**Phase 1 — Digest (no Socket Mode required):** config fields, `Issue.description` +
`_ISSUE_FIELDS` change, the three new Jira tools, `app/slack/digest.py`,
`app/slack/formatting.py`, `scripts/post_digest.py`, and their tests. This phase is fully
testable and deliverable without touching `app/main.py` or adding the Socket Mode
dependency surface at all.

**Phase 2 — @-mention:** `app/slack/bolt_app.py`, the FastAPI lifespan integration, and its
tests. Depends on Phase 1 only for `format_ask_response`/shared formatting conventions, not
for any digest-specific logic — could in principle ship independently if reordered.

**Phase 3 — Polish:** `launchd` plist documented and verified against a real daily run;
CLAUDE.md/README updates (§11); resolve Open Question #1 if it surfaced ambiguity during
Phase 1 implementation rather than deferring it further.

## 13) Open Questions

1. ~~**`get_backlog_issues_needing_details` identity scope.**~~ **Resolved 2026-08-13:**
   `reporter = currentUser() OR assignee = currentUser()` — see §6.5. Catches tickets the
   user created *and* tickets they're personally on the hook for, not project-wide.
2. **Multiple simultaneous active sprints.** `get_current_sprint_issues()` unions issues
   across all active sprints found across all boards (§6.5) — correct for the common case
   of one board/one active sprint, but unverified against a real multi-board or
   multi-active-sprint Jira setup, since (per prior session context) the user's current
   Jira project has never had a sprint at all yet. Flag as a live-testing gap, same
   category as the existing "did you mean Sprint X" note feature.
3. **Digest destination default.** §6.3 assumes a single `SLACK_CHANNEL_ID`; if the user
   ultimately wants delivery to a DM instead of a channel, that's a one-line config change
   (any Slack DM has a channel ID), not a design change — noted here only so it isn't
   mistaken for unsupported.

## 14) References

- `docs/ideation/slack-integration.md` — full ideation history, research findings, and the
  decision trail this spec formalizes.
- [Using Socket Mode | Slack Developer Docs](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [Using async (asyncio) | Slack Developer Docs](https://docs.slack.dev/tools/bolt-python/concepts/async/)
- [slack-bolt Python SDK (GitHub)](https://github.com/slackapi/bolt-python)
- [Formatting message text | Slack Developer Docs](https://docs.slack.dev/messaging/formatting-message-text/)
- `specs/feat-engineering-productivity-agent-mvp.md` — parent spec; `ToolResult`,
  `sanitize_error()`, and the ranking/tool-contract conventions this feature reuses
  unchanged.
