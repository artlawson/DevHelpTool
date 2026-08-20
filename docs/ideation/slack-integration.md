# Slack Integration

**Slug:** slack-integration
**Author:** Claude Code
**Date:** 2026-08-13
**Branch:** preflight/slack-integration
**Related:** `specs/feat-engineering-productivity-agent-mvp.md` (Non-Goals §4 lists Slack as
post-MVP roadmap only, not required for MVP completion), `CLAUDE.md` Deployment section
("local-only, single-user tool by design")

---

## 1) Intent & Assumptions

- **Task brief:** Add Slack as a second interface to DevHelpTool, alongside the existing
  `POST /ask` HTTP API. Two trigger modes, both wanted (decided via AskUserQuestion,
  2026-08-13): (1) an **@-mention** in a channel gets an ad-hoc response using the same
  underlying orchestrator as `/ask` today, and (2) a **scheduled digest** is proactively
  posted to a channel/DM at standup time, with no user action needed.
- **Assumptions:**
  - Single user, single Slack workspace — no multi-tenant app distribution, no OAuth
    install flow, no Slack App Directory listing.
  - The existing orchestrator (`app/agent/orchestrator.py:handle_query`) and its
    `AskResponse` shape (`answer`, `tool_calls`, `warnings`) are reused as-is for the
    @-mention path — Slack is a new *interface*, not a new *orchestration* layer.
  - The digest reuses the same Jira/GitHub tools already built (`get_my_high_priority_issues`,
    `get_incomplete_issues_from_last_sprint`, etc.) plus one new tool for active-sprint
    issues (see Clarification #4 resolution below).
  - This is explored as a spec-first feature (per user decision) rather than a quick
    prototype — this ideation doc precedes `/spec:create`.
  - **Forward-looking, not built now:** this is currently scoped for one identity (a
    senior engineer who creates tickets, per `.env`'s single `JIRA_EMAIL`/`GITHUB_USERNAME`).
    The user intends to later extend this to junior engineers/interns, whose digest should
    look different — they'll typically have no high-priority work assigned, so the
    "no high-priority work" fallback branch (Clarification #4) is actually their common
    case, not an edge case. Multi-identity/role support is explicitly **not** being built
    in this pass (it would also require revisiting the project's single-user non-goal in
    spec §4), but the digest section-selection logic should be written as a plain function
    of "does this identity have open high-priority work," not hardcoded to a senior-engineer
    persona, so a future `role`/persona parameter is an extension rather than a rewrite.
- **Out of scope (for this ideation pass):**
  - Slack slash commands (`/standup`) — not one of the two modes requested.
  - Multi-user/multi-workspace support, per-user Slack identity mapping.
  - Interactive Block Kit actions (buttons like "mark done") — formatting only, no
    write-back to Jira/GitHub triggered from Slack.
  - Production-grade always-on hosting (see Clarification #3) — this doc surfaces the
    tension but doesn't resolve it by deploying anything.

---

## 2) Pre-reading Log

- `CLAUDE.md` (Deployment section): explicitly states "local-only, single-user tool by
  design — no authentication, no deployment target, no CI/CD," citing spec §4 (Non-Goals)
  and §10 (Security Considerations) as a **stated scope boundary, not an oversight**. Any
  Slack integration that requires the app to be reachable or continuously running directly
  presses on this boundary.
- `specs/feat-engineering-productivity-agent-mvp.md` §4 (Non-Goals): "Slack, Calendar,
  CI/CD monitoring, standup generation, deployment/observability tooling (post-MVP roadmap
  only)" — Slack was anticipated from the start as the natural next step, just deferred.
- `specs/feat-engineering-productivity-agent-mvp.md` §6.6: the orchestration loop
  (`handle_query(query: str) -> AskResponse`) takes a plain string and returns a structured
  response — it has no knowledge of HTTP or FastAPI, so it's already callable from a
  non-HTTP entry point (like a Slack event handler) with zero changes.
- `app/main.py`: FastAPI app is thin — `POST /ask` just calls `handle_query`, catches
  `OrchestratorUnavailable` at the route boundary. No app-startup lifecycle hook exists yet
  (no `@app.on_event("startup")` / lifespan handler) — one would need to be added to boot a
  Socket Mode connection alongside the API.
- `app/config.py`: `Settings(BaseSettings)` loads required fields from `.env` via
  `pydantic-settings`, one flat class, no optional/nested config groups yet. New Slack
  credentials would extend this the same way `jira_project_key` was recently added.
- `app/core/models.py`: `AskResponse.answer` is a plain narrated string (not structured
  bullet data) — Block Kit formatting would need to either reformat this string or bypass
  narration and format the underlying ranked `Issue`/`PullRequest` lists directly (see
  Clarification #4).
- No existing `app/slack/` or equivalent — this is a new top-level package, matching the
  existing per-integration layering (`clients/`, `tools/`) rather than being bolted onto
  `agent/`.

---

## 3) Codebase Map

- **Primary components/modules (new):**
  - `app/slack/bolt_app.py` — `AsyncApp` instance, `@app.event("app_mention")` handler,
    calls `handle_query()` directly (in-process, not via HTTP self-call)
  - `scripts/post_digest.py` (or similar, outside `app/`) — the standalone, `launchd`-
    scheduled script for Clarification #3's Option C; imports `app.tools.*` directly, has
    no dependency on the FastAPI process being up
  - `app/slack/digest.py` — pure section-selection logic (see Clarification #4 resolution):
    takes the fetched issue/PR data in and returns "High Priority" + "Upcoming" sections
    out, with no Slack/formatting knowledge — this is the function that should stay
    identity-agnostic per the forward-looking role note in §1
  - `app/slack/formatting.py` — shared Block Kit/bullet builders (issue/PR → Slack text)
    used by both the @-mention reply and the digest
  - `app/tools/jira_tools.py` — new `get_current_sprint_issues()` (or similar), built on
    the existing `JiraClient.get_active_sprints()` used for the "did you mean Sprint X"
    note; needed for both branches of the Upcoming section
- **Modified components:**
  - `app/main.py` — needs a FastAPI lifespan handler to start/stop the Socket Mode
    connection (and the scheduler, if in-process) alongside `uvicorn`
  - `app/config.py` — new `slack_bot_token`, `slack_app_token`, `slack_channel_id` fields
  - `.env.example` — document the three new Slack vars
- **Shared dependencies:** `app/agent/orchestrator.py:handle_query` (mention path),
  `app/tools/jira_tools.py` + `app/tools/github_tools.py` (digest path, called directly
  rather than through the LLM tool-calling loop, since a scheduled digest has no natural-
  language query to interpret — it always wants "the standard set")
  - **New runtime dependencies:** `slack-bolt` (SDK), `apscheduler` (if in-process
    scheduling is chosen — see Clarification #3)
- **Data flow (mention path):** Slack `app_mention` event → Socket Mode → Bolt handler →
  `handle_query(query)` → `AskResponse` → format as Slack message → reply in-thread
- **Data flow (digest path):** scheduler fires → call Jira/GitHub tools directly → rank
  (existing `core/ranking.py`) → format as Block Kit → `chat.postMessage`
- **Feature flags/config:** three new env vars (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
  `SLACK_CHANNEL_ID`); Slack integration should probably be optional-to-enable (app runs
  fine without Slack configured) rather than a hard requirement — open question folded
  into Clarification #1
- **Potential blast radius:** low for the mention path (additive, reuses existing
  orchestrator untouched); the scheduler/always-on question (Clarification #3) is the one
  part of this feature that could ripple into `CLAUDE.md`'s documented deployment stance
  and should be called out explicitly in any resulting spec, not silently decided.

---

## 4) Root Cause Analysis

N/A — this is a new feature, not a bug fix.

---

## 5) Research

Findings from the research-expert agent (Aug 2026 sources), summarized by area — full
detail and citations available on request:

**1. Connection model (no public HTTPS endpoint today)**
- *Socket Mode* (`slack-bolt`) — outbound WebSocket, no inbound port or tunnel required.
  Officially supported for exactly this "can't expose a static HTTP endpoint" scenario.
  Needs one extra credential (`SLACK_APP_TOKEN`, `xapp-...`).
- *Events API over HTTP* — needs a real public HTTPS endpoint (ngrok/Cloudflare Tunnel for
  dev, real hosting for anything persistent) plus signing-secret verification.
- **Recommendation:** Socket Mode — the only option that doesn't reverse the project's
  local-only, no-deploy design decision.

**2. `slack-bolt` SDK integration with FastAPI**
- Use `slack_bolt.async_app.AsyncApp` + `AsyncSocketModeHandler`, both async-native and
  compatible with the existing `httpx`/FastAPI async stack.
- Socket Mode can run in the same process as FastAPI, started from a startup lifecycle
  hook so both share one event loop — no second process/manager needed at this scale.

**3. Scheduled/proactive digest**
- *In-process `AsyncIOScheduler`* (APScheduler) — one new dependency, registers a cron-
  style job in the same startup hook as Socket Mode; only fires if the process happens to
  be running at digest time.
- *Bare `asyncio.sleep` loop* — no new dependency, but reimplements cron scheduling
  (DST, missed runs) APScheduler already solves.
- *External cron/`launchd` hitting an internal endpoint* — decouples scheduling from
  in-process timers, but still requires the app to be up at that hour either way.
- **Real constraint, not a technical detail:** today the app is started manually
  (`uvicorn --reload`) — none of these options fire a 9am digest unless something keeps
  the process alive through the night. This is the crux of Clarification #3.

**4. Message formatting**
- *Block Kit* `section` blocks with `mrkdwn` fields, grouped by category, linked via
  `<url|KEY>` — idiomatic for a structured "what to work on" summary, matches the shape
  of ranked `Issue`/`PullRequest` data already produced by `core/ranking.py`.
- *Plain `mrkdwn` text* — simpler, fine for short mention replies, weaker for a
  multi-section digest.

**5. Auth/credentials**
- Socket Mode needs `SLACK_BOT_TOKEN` (`xoxb-`) + `SLACK_APP_TOKEN` (`xapp-`); a signing
  secret is only needed if HTTP mode is added later. Fits the existing flat
  `pydantic-settings` `Settings` class directly, same pattern as the Jira/GitHub fields.
- Internal, non-Marketplace bots are exempt from the newer Slack rate-limit tightening — no
  rate-limit risk at this scale.

---

## 6) Clarifications Needed

### 1. Connection model

**Question:** How should the app receive Slack events (for the @-mention trigger) without
standing up new public infrastructure?

| Option | Description |
|--------|-------------|
| **A) Socket Mode** | Outbound WebSocket via `slack-bolt`; no inbound port, no tunnel. One extra token. Preserves local-only posture. |
| **B) Events API + tunnel** | Public HTTPS endpoint via ngrok/Cloudflare Tunnel for dev. Extra moving part that must be running alongside the app. |
| **C) Events API + real hosting** | Deploy the FastAPI app somewhere reachable. Reverses the project's stated no-deployment scope boundary. |

**Recommendation:** **Option A** — Socket Mode is the only option that doesn't require
reopening the "no deployment target" decision already documented in `CLAUDE.md` and spec §4.

### 2. Where does the Socket Mode handler run?

**Question:** Should the Slack Bolt app run in the same process as the FastAPI `uvicorn`
server, or as a separate process?

| Option | Description |
|--------|-------------|
| **A) In-process, shared event loop** | Started from a FastAPI lifespan/startup hook; one `uvicorn` command still runs everything, mention handler calls `handle_query()` in-process (no HTTP round-trip to itself). |
| **B) Separate process** | A second entry point (e.g. `python -m app.slack.run`) run alongside `uvicorn` manually or via a process manager. Cleaner failure isolation, but two things to start/keep alive instead of one. |

**Recommendation:** **Option A** — for a single-user tool, one process to start and reason
about is simpler, and it directly reuses `handle_query()` without inventing an internal
HTTP client just to call your own API.

### 3. How does the scheduled digest actually fire, given the app isn't always running?

**Question:** The app is started manually today (`uvicorn --reload`). A 9am digest needs
*something* alive at 9am. What's the intended model?

| Option | Description |
|--------|-------------|
| **A) In-process APScheduler + accept best-effort delivery** | Add a cron job inside the app; digest only fires if you happen to have the dev server running at that hour. No infra change, but unreliable as an actual daily habit. |
| **B) Keep the app running via `launchd`/login item** | Turn the local service into something that starts on login and stays up (still fully local — no network exposure change, no cloud hosting). This is a real, if small, shift from "manually started dev server" toward "local daemon." |
| **C) External `cron`/`launchd` triggers a one-shot script** | A lightweight scheduled script (not the long-running app) wakes up, does the Jira/GitHub calls + Slack post itself, and exits — sidesteps needing the FastAPI process alive at all, at the cost of duplicating some tool-import wiring outside the app's process. |

**Recommendation:** **Option C** for reliability with the least change to the project's
current "not a long-running service" posture — a `launchd`-scheduled script that imports
`app.tools.*` directly and posts to Slack is closer in spirit to "still local-only,
manually invoked, just now invoked by the OS scheduler instead of a person" than turning
the whole FastAPI app into an always-on daemon (Option B) or accepting unreliable delivery
(Option A). Worth confirming with you directly since it's the one decision in this doc that
touches the project's deployment posture at all.

**Decided (2026-08-13): Option C.** Confirmed that this still gets correct, live data —
the script isn't a degraded fallback, it runs the identical `app/tools/*` functions the
main app uses, against live Jira/GitHub credentials from `.env`. The only behavioral
difference from a request through the running app is that the script starts cold each
run, so it never reuses the in-memory 60s TTL cache — every digest is a fresh fetch, which
is the right behavior for a once-a-day job anyway.

### 4. Digest content: narrated answer or direct structured data?

**Question:** Should the digest reuse `handle_query()` (LLM narrates a summary from a fixed
prompt like "what should I work on today") or call the ranking tools directly and format
their structured output as Block Kit, skipping LLM narration entirely?

| Option | Description |
|--------|-------------|
| **A) Reuse `handle_query()`** | One code path for both mention and digest; output is a narrated paragraph, then wrapped in a single Block Kit section. Simpler, but loses per-item structure (ticket links, grouping) since `AskResponse.answer` is just a string. |
| **B) Call tools directly, format structured data** | Bypasses the LLM for the digest; calls `get_my_high_priority_issues()` etc. directly, builds one Block Kit section per category with real `<url|KEY>` links. More code (a second orchestration path), but a more useful/scannable Slack message and zero LLM cost/latency for something that runs unattended every day. |

**Recommendation:** **Option B** — a daily unattended job benefits from determinism (no
chance of the model narrating something misleading with nobody watching) and from
structured links Slack can render directly; this also mirrors the project's existing
philosophy of "deterministic Python ranking, not LLM-driven" (`CLAUDE.md` Project Purpose)
extended to presentation, not just scoring.

**Decided (2026-08-13): Option B, formatted as bullets, with two sections and this
branching logic:**

- **Section 1 — "High Priority":**
  - If the identity has open high-priority issues (existing `get_my_high_priority_issues`)
    → show those.
  - Else → fall back to open medium/low-priority issues with a due date within the
    **next 7 days** (`due_date` between today and today+7, inclusive). If that fallback
    also comes up empty, Section 1 doesn't just disappear — it renders a short explicit
    line like *"Nothing due soon"* rather than an empty/missing section, for the same
    ambiguity reason `ToolResult.note` exists elsewhere in this codebase (an empty section
    should never be indistinguishable from "this wasn't checked").
- **Section 2 — "Upcoming":**
  - If Section 1 showed real high-priority work → show lower-priority issues still
    assigned to the identity within the **current (active) sprint** — requires the new
    `get_current_sprint_issues()` tool noted in §3.
  - If Section 1 was the fallback (no high-priority work existed) → show whatever's left
    in the current sprint, plus backlog tickets that have a title but an **empty
    description** (signals a ticket that needs to be fleshed out before anyone can start
    it — replaces an earlier "no points set" idea, which was dropped since story points
    aren't fetched from Jira today and would've needed a per-instance custom-field lookup;
    an empty `description` needs only adding that field to `JiraClient`'s existing
    `_ISSUE_FIELDS`).

**New work this resolution introduces**, beyond what §3 (Codebase Map) already listed:
add `description` to `_ISSUE_FIELDS` in `app/clients/jira_client.py`; build
`get_current_sprint_issues()` in `jira_tools.py`; the due-soon fallback filter for
Section 1. None of these touch existing tool behavior — all additive.

**Identity scope for the "backlog needing details" bucket, decided 2026-08-13** (surfaced
during `/spec:create`, not this ideation pass, but recorded here for the full decision
trail): `reporter = currentUser() OR assignee = currentUser()`, not project-wide — catches
tickets the user created *and* tickets they're personally on the hook for. See
`specs/feat-slack-integration.md` §6.5/§13 for the resulting JQL.

### 5. Digest destination

**Question:** Should the digest post to a shared channel, a DM to yourself, or be
configurable?

| Option | Description |
|--------|-------------|
| **A) Fixed channel (env var)** | `SLACK_CHANNEL_ID` in `.env`, same pattern as `JIRA_PROJECT_KEY`. Simple, matches single-user scope. |
| **B) DM to self** | Bot opens/uses a DM conversation with the configured user. Slightly more private, one extra Slack API call (`conversations.open`) to resolve the DM channel ID once. |
| **C) Configurable, defaulting to DM** | Support both via one env var that accepts either a channel ID or falls back to DM if unset. |

**Recommendation:** **Option A** — simplest, and consistent with this being a single-user
tool where "channel" can just be a private channel or DM's channel ID either way; no need
to special-case DM resolution in code when the env var can just hold whichever ID the user
points it at.

