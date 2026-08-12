# Engineering Productivity Agent — 2-Week MVP

**Slug:** engineering-productivity-agent-mvp
**Author:** Claude Code
**Date:** 2026-08-11
**Branch:** preflight/engineering-productivity-agent-mvp
**Related:** CLAUDE.md project instructions ("Engineering Productivity Agent — Project Instructions")

---

## 1) Intent & Assumptions

- **Task brief:** Build an AI-powered engineering productivity agent that takes natural-language requests (e.g. "what should I work on today?", "which high-priority Jira tickets don't have PRs?", "what's awaiting my review?") and orchestrates calls to Jira and GitHub APIs (Slack/Calendar optional stretch) via LLM tool-calling, aggregating results into concise, actionable answers rather than raw API dumps. This is a 2-week portfolio project meant to read as a backend/platform engineering build, not a chatbot wrapper.
- **Assumptions:**
  - Python is the implementation language (explicitly named in the project's interview-framing goals: "Python application development"), even though this repo's starter-kit template defaults reference npm/TypeScript tooling.
  - Single user, personal-use tool — no multi-tenant auth, no deployment target beyond local/dev.
  - LLM provider is Anthropic's Claude API (this is a Claude Code project; no other provider was specified), used for tool-calling orchestration and final response synthesis — not for the aggregation/ranking logic itself.
  - "Today" scope for repo/tickets is user-provided or defaults to the authenticated Jira/GitHub identity — no team-wide aggregation in MVP.
- **Out of scope:** multi-agent systems, RAG/vector DBs, autonomous background agents, production-grade auth (OAuth flows, token refresh services), web dashboard/frontend, CI/CD monitoring, standup generation/scheduling, observability stack, Slack/Calendar (stretch only, not required for MVP completion).

---

## 2) Pre-reading Log

- `README.md`: This repo is the generic "Claude Code Starter Kit" — ships `.claude/commands`, `.claude/agents`, `.claude/skills`, and hooks (lint/typecheck/test-on-write via `claudekit-hooks`), but assumes a Node/TS-shaped project by default (hooks reference `lint-changed`, `typecheck-changed`, `test-changed`). No app-specific code yet.
- `CLAUDE.md`: Recently customized with the actual project purpose (this task) but the rest of the template (directory structure, dev commands, env vars, dependencies) is still unfilled `[CUSTOMIZE]` placeholder content — needs a full rewrite once the stack is chosen.
- `.claude/settings.json`: PostToolUse hooks run `lint-changed`/`typecheck-changed`/`test-changed` on every Write/Edit — these are JS/TS-oriented `claudekit-hooks` commands and will no-op or error against a Python project until reconfigured (or replaced with `ruff`/`mypy`/`pytest` equivalents).
- No `pyproject.toml`, `requirements.txt`, `package.json`, or `go.mod` exists — the repo has no source code yet; this is a greenfield build.

---

## 3) Codebase Map

- **Primary components/modules:** None yet — to be created. Anticipated shape based on the stated priorities (clean separation between orchestration and tools):
  - `agent/` — LLM orchestration loop (tool-calling loop, provider client)
  - `tools/` — one module per integration (`tools/jira.py`, `tools/github.py`), each exposing plain, independently testable functions plus a JSON-schema tool definition
  - `core/` or `models/` — shared data types (e.g. `Issue`, `PullRequest`) so tools return structured data, not prose
  - `cli.py` or `api.py` — the entrypoint (see Clarification #1)
- **Shared dependencies:** none yet (see Research §1 for the recommended `anthropic` SDK + `httpx`/`requests` stack)
- **Data flow:** natural-language request → orchestration loop → LLM selects tool(s) → tool functions call Jira/GitHub APIs concurrently → structured results returned to loop → LLM (or deterministic formatter) synthesizes concise response
- **Feature flags/config:** API credentials (Jira API token + email, GitHub PAT, Anthropic API key) — storage mechanism is an open clarification (#4)
- **Potential blast radius:** none — greenfield, no existing functionality to regress. Main risk is scope creep past the 2-week MVP boundary (see Out of scope above) and hook misconfiguration (JS-oriented hooks running against Python code) causing noisy false failures once implementation starts.

---

## 4) Root Cause Analysis

N/A — this is a new feature build, not a bug fix.

---

## 5) Research

Full findings from the research-expert agent (Aug 2026 sources) are summarized below by area; see individual clarifications for the recommendation tied to each.

**1. LLM tool-calling architecture**
- *Raw Anthropic SDK + hand-rolled loop* (~50–80 lines: call `messages.create()` with `tools`, dispatch on `stop_reason == "tool_use"`, loop). Matches Anthropic's own "workflows over agents" reliability guidance for this use case; fully legible in review, trivially unit-testable.
- *Claude Agent SDK* — ships session/context management, MCP tool registration, subagent spawning. Oriented around Claude Code's own tool surface (files/bash) rather than arbitrary custom tools; pulls in multi-agent capability that's explicitly out of scope here.
- *LangChain / OpenAI Agents SDK* — heavier abstraction over the same loop; LangChain specifically is reported as what "most production teams spent 2025 migrating off." Would undercut the "I understand the orchestration" narrative.
- **Recommendation:** raw SDK + hand-rolled loop (Option A) — the strongest fit for "well-defined tool interfaces" and "separation between orchestration and tools."

**2. Jira REST API (Cloud) integration**
- `jira` package (PyPI) — high-level object model, less raw-API visibility.
- `atlassian-python-api` — broader coverage, thinner wrapper, less Pythonic.
- Raw `requests`/`httpx` against `/rest/api/3/` — full control, easiest to mock in tests, best demonstrates direct API fluency.
- **Auth:** API token (Basic Auth) over OAuth 2.0 3LO — simpler, no redirect flow, and explicitly exempt from Atlassian's new points-based rate limits (effective March 2026), unlike OAuth/Connect app traffic.
- **Query pattern:** `assignee = currentUser() AND priority in (High, Highest) AND resolution = Unresolved ORDER BY updated DESC`. "Tickets without linked PRs" has no native JQL field — either call the `dev-status` endpoint or (simpler, recommended) cross-reference Jira issue keys against GitHub PR titles/branches yourself.

**3. GitHub REST/GraphQL API integration**
- PyGithub — mature, good ergonomics, but flagged as seeking maintainers (minor risk, still shipping releases as of April 2026).
- GraphQL (`gql`/raw `requests`) — most efficient for multi-repo review/PR queries in one round trip; more verbose to write.
- Raw `requests` against the REST Search API (`is:pr review-requested:@me`, `is:pr author:@me is:open`) — directly answers both target questions with minimal code.
- **Recommendation:** raw REST search for MVP simplicity; note GraphQL as a documented future optimization.
- **Auth:** fine-grained PAT scoped to specific repos — simplest for personal use, demonstrates least-privilege awareness without building a GitHub App (correctly out of scope).
- **Jira↔PR linking:** regex-match Jira issue keys (`PROJ-123`) against PR titles/branch names — mirrors how Atlassian's own Smart Commits work, no extra API dependency.

**4. Architecture pitfalls to design around**
- Run Jira/GitHub/optional Slack calls concurrently (`asyncio.gather` + `httpx.AsyncClient`), not sequentially — visible, demoable latency win.
- Each tool call should return a `Result`-style success/failure (matches this repo's own CLAUDE.md error-handling convention) so one integration failing degrades gracefully instead of crashing the whole response.
- Add a simple TTL cache in front of each tool (Jira's new rate-limit tiers specifically flag repeated large JQL queries as a fast way to exhaust quota).
- Keep tools returning structured data (not LLM-formatted prose) so they stay independently unit-testable, with a thin adapter layer describing them to the LLM.
- All MVP tools are read-only — sidesteps idempotent-retry complexity entirely.

---

## 6) Clarifications Needed — RESOLVED 2026-08-11

All five decisions below were confirmed by the user, following the stated recommendation in each case:

1. Interface → **FastAPI local service**
2. LLM orchestration → **Hand-rolled loop on raw `anthropic` SDK**
3. API clients → **Raw `httpx`** for both Jira and GitHub
4. Credential storage → **`.env` + `python-dotenv`**
5. Ranking logic → **Deterministic Python scoring**, LLM narrates only


### 1. Application interface: one-shot CLI vs. lightweight local service

**Question:** Should the agent be invoked as a one-shot CLI command (e.g. `agent ask "what should I work on today?"`) or run as a small local service (e.g. FastAPI on localhost) that a CLI/curl hits repeatedly?

| Option | Description |
|--------|-------------|
| **A) One-shot CLI (Typer/Click)** | Each invocation is a fresh process: parse args → run orchestration loop → print result → exit. Simplest to build and demo; no persistent cache between invocations (each run re-fetches from Jira/GitHub). |
| **B) Lightweight local FastAPI service** | Long-running process exposing `/ask`; a thin CLI or curl calls it. Enables an in-memory TTL cache that persists across requests, and doubles as a "backend service design" portfolio artifact (explicitly named in your interview-framing goals). |
| **C) CLI with a local SQLite/file cache** | One-shot CLI like (A), but persists cache entries to a local SQLite file between runs, giving caching benefits without a long-running process. |

**Recommendation:** **Option B** — a small FastAPI service is barely more code than a CLI (a few route handlers around the same orchestration loop) and directly strengthens the "backend service design" and "system integration" resume framing over a plain script, while still being trivial to run locally with no deployment story needed.

### 2. LLM orchestration pattern

**Question:** Should the tool-calling loop be hand-rolled against the raw `anthropic` SDK, or built on the Claude Agent SDK?

| Option | Description |
|--------|-------------|
| **A) Hand-rolled loop on raw `anthropic` SDK** | ~50-80 line loop you own end-to-end: call `messages.create(tools=...)`, dispatch `tool_use` blocks to your `ToolRegistry`, append `tool_result`, repeat until `stop_reason != "tool_use"`. |
| **B) Claude Agent SDK** | Use the packaged agent loop with MCP-based tool registration and built-in session/retry handling. |

**Recommendation:** **Option A** — per Research §1, this is what Anthropic's own docs demonstrate for reliability-sensitive single-agent use cases, keeps the "orchestration logic vs. tool implementation" separation fully visible in your own code (the core architectural story of this project), and avoids pulling in the Agent SDK's multi-agent/file-tool surface you don't need.

### 3. API client libraries for Jira and GitHub

**Question:** Use raw HTTP (`httpx`/`requests`) against each API directly, or adopt existing Python client packages (`jira`, `PyGithub`)?

| Option | Description |
|--------|-------------|
| **A) Raw HTTP via `httpx`** | Thin `JiraClient`/`GitHubClient` wrappers you write yourself around `/rest/api/3/` and GitHub's REST Search API. Full control, easiest to mock in tests with `respx`, consistent async story across both integrations. |
| **B) `jira` + `PyGithub` packages** | Faster initial development via higher-level object models; less raw-API visibility, and mixing a sync-only client library (both are sync) complicates the async/concurrent-fetch design from Research §4. |

**Recommendation:** **Option A** — consistent with the async concurrency pattern recommended in Research §4, gives you full control over request/response shape for testing, and is a stronger "I integrated directly against documented REST APIs" talking point than "I imported a wrapper."

### 4. Credential storage for API keys/tokens

**Question:** How should the Jira API token, GitHub PAT, and Anthropic API key be stored and loaded?

| Option | Description |
|--------|-------------|
| **A) `.env` file + `python-dotenv`** | Standard local-dev pattern; `.env` gitignored, `.env.example` checked in documenting required vars. |
| **B) OS keychain (`keyring` package)** | More "production-grade" credential handling, avoids plaintext-on-disk secrets. |
| **C) Config file (YAML/TOML) with env var overrides** | More structured for multi-value config (e.g. which repos to watch), secrets still via env vars. |

**Recommendation:** **Option A** — matches your explicit "no production auth systems" scope boundary, is the standard pattern this repo's own CLAUDE.md error-handling/env-var conventions already assume, and is the fastest to wire up within the 2-week window. Note the keychain approach as a documented future improvement rather than building it now.

### 5. Split between deterministic ranking logic and LLM reasoning

**Question:** When answering "what should I work on today?", should prioritization/ranking (e.g., sort by Jira priority + due date + review-request age) be deterministic Python logic that the LLM then narrates, or should the LLM itself reason over the raw aggregated data to decide priority?

| Option | Description |
|--------|-------------|
| **A) Deterministic ranking in Python, LLM narrates** | Tools return structured, pre-sorted/pre-scored data (e.g. a `priority_score` field computed in Python); the LLM's job is purely to synthesize a natural-language summary from already-ranked structured input. |
| **B) LLM reasons freely over raw aggregated data** | Tools return raw structured data; the LLM decides what's "important" and how to rank/summarize it with no pre-computed ordering. |
| **C) Hybrid** | Deterministic hard filters (e.g. exclude resolved/closed items) in Python, but relative prioritization among the remaining items is left to the LLM's judgment. |

**Recommendation:** **Option A** — deterministic, testable ranking logic is a stronger backend-engineering demonstration than "trust the LLM's judgment," keeps the system's behavior reproducible and unit-testable independent of model behavior, and directly matches the stated priority of "structured data processing." Reserve the LLM for tool selection and natural-language synthesis, not decision-making over what counts as urgent.
