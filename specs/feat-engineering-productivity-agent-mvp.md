# Engineering Productivity Agent — MVP

**Status:** Draft
**Authors:** Claude Code, 2026-08-11
**Related:** `docs/ideation/engineering-productivity-agent-mvp.md`

---

## 1) Overview

A local FastAPI service that answers natural-language questions about a single engineer's current work ("what should I work on today?", "which high-priority Jira tickets don't have PRs?", "what's awaiting my review?") by orchestrating an LLM tool-calling loop over Jira and GitHub REST APIs. The service fetches and deterministically ranks structured data in Python, then uses Claude only to select which tools to call and to narrate the final answer — not to decide what's important.

## 2) Background / Problem Statement

Checking "what do I need to do today" currently means manually opening Jira, filtering by assignee/priority, opening GitHub, checking review requests, and mentally cross-referencing which tickets already have PRs. This is a 2-week portfolio project for a backend/platform engineer transitioning into interviews: it needs to demonstrate LLM tool-calling, API orchestration, clean service architecture, and structured data processing — not just wrap an LLM around API calls.

## 3) Goals

- Answer natural-language queries by dynamically selecting and calling Jira and/or GitHub tools
- Aggregate and deterministically rank results (priority, due date, review-request age) before any LLM narration
- Return concise, actionable natural-language answers — never raw API payloads
- Keep tool implementations independently testable, decoupled from the LLM orchestration layer
- Handle partial integration failure gracefully (e.g., GitHub down → still answer from Jira)
- Ship as a locally runnable FastAPI service with clear setup instructions

## 4) Non-Goals

- Multi-agent orchestration or agent-to-agent handoff
- RAG / vector database / long-term conversation memory
- Autonomous/scheduled background execution
- Production authentication (OAuth flows, multi-tenant auth, token refresh services)
- Web dashboard or any frontend beyond the API itself
- Slack, Calendar, CI/CD monitoring, standup generation, deployment/observability tooling (post-MVP roadmap only)
- Write/mutating operations against Jira or GitHub (MVP is strictly read-only)

## 5) Technical Dependencies

| Library | Purpose | Notes |
|---|---|---|
| `fastapi` | HTTP service framework | ASGI, async-native |
| `uvicorn` | ASGI server | dev/local run |
| `anthropic` | Claude API client | tool-calling (`messages.create(tools=...)`) |
| `httpx` | Async HTTP client | used directly against Jira REST v3 and GitHub REST Search API — no `jira`/`PyGithub` wrapper packages, per architecture decision |
| `pydantic` | Data models & settings | request/response models, `.env` settings via `pydantic-settings` |
| `python-dotenv` (via `pydantic-settings`) | `.env` loading | local credential storage |
| `pytest`, `pytest-asyncio` | Test runner | async test support |
| `respx` | HTTP mocking | mocks `httpx` calls in tool/client tests |

No exotic or fast-moving library APIs are involved; Context7 was intentionally skipped for this spec per user decision — all libraries above are stable, mainstream, and well-documented.

## 6) Detailed Design

### 6.1 Architecture

```
Client (curl / thin CLI)
        │  POST /ask {"query": "..."}
        ▼
FastAPI app (api/main.py)
        │
        ▼
Orchestrator (agent/orchestrator.py)
  - hand-rolled loop against anthropic SDK
  - dispatches tool_use blocks via ToolRegistry
        │
        ├──► tools/jira.py    ──► clients/jira_client.py    ──► Jira REST v3
        └──► tools/github.py  ──► clients/github_client.py  ──► GitHub REST Search API
        │
        ▼
core/ranking.py (deterministic scoring, pure functions)
        │
        ▼
Claude narrates final answer from ranked, structured tool results
        │
        ▼
FastAPI returns {"answer": "..."}
```

### 6.2 File / Module Structure

```
app/
├── main.py                  # FastAPI app, POST /ask route
├── config.py                # pydantic-settings: JIRA_*, GITHUB_*, ANTHROPIC_API_KEY
├── agent/
│   ├── orchestrator.py      # hand-rolled tool-calling loop
│   ├── registry.py          # ToolRegistry: name -> (callable, json_schema)
│   └── schemas.py           # JSON schema tool definitions passed to Claude
├── clients/
│   ├── jira_client.py       # thin httpx wrapper: auth, base URL, JQL search
│   └── github_client.py     # thin httpx wrapper: auth, base URL, shared search_prs(query) method
├── tools/
│   ├── jira_tools.py        # get_my_high_priority_issues, get_issues_without_prs
│   └── github_tools.py      # get_my_open_prs, get_prs_awaiting_my_review
├── core/
│   ├── models.py            # Issue, PullRequest, ToolResult[T] (Result-style)
│   ├── ranking.py           # score_issue(), score_pr(), rank() — pure functions
│   └── cache.py             # in-memory TTL cache (dict + expiry, no new dependency)
└── tests/
    ├── test_ranking.py
    ├── test_jira_tools.py
    ├── test_github_tools.py
    ├── test_orchestrator.py
    └── test_api.py
```

### 6.3 API

**`POST /ask`**

Request:
```json
{ "query": "what should I work on today?" }
```

Response (200):
```json
{
  "answer": "You have 2 high-priority tickets without PRs: PROJ-123 (2 days overdue) and PROJ-140. You also have 1 PR awaiting your review, opened 3 days ago: org/repo#42.",
  "tool_calls": ["jira.get_my_high_priority_issues", "jira.get_issues_without_prs", "github.get_prs_awaiting_my_review"]
}
```
`tool_calls` is included for transparency/debuggability (and is a natural interview talking point — "here's the audit trail of what the agent decided to call").

Response (207 / partial degradation — modeled as 200 with a `warnings` field, not an HTTP error, since a partial answer is still a valid answer):
```json
{
  "answer": "GitHub data is currently unavailable, so this only reflects Jira: ...",
  "tool_calls": ["jira.get_my_high_priority_issues"],
  "warnings": ["github: request failed after 2 retries (503)"]
}
```

**`GET /health`** — trivial liveness check (no auth, no external calls), standard for any service.

### 6.4 Data Models (`core/models.py`)

```python
class Issue(BaseModel):
    key: str
    summary: str
    priority: str
    status: str
    due_date: date | None
    has_linked_pr: bool
    priority_score: float  # set by core/ranking.py, not the API layer

class PullRequest(BaseModel):
    repo: str
    number: int
    title: str
    url: str
    opened_at: datetime
    is_review_requested: bool
    is_authored_by_me: bool
    age_score: float  # set by core/ranking.py

class ToolResult[T](BaseModel):
    ok: bool
    data: T | None
    error: str | None
```

`ToolResult` is the "Result-style success/failure" wrapper already established as this repo's error-handling convention (see `CLAUDE.md`'s `Result<T>` pattern) — every tool function returns one, so the orchestrator can degrade gracefully per-tool instead of raising.

### 6.5 Tool Interface Contract

Every tool is a plain, independently callable async function with **no** knowledge of the LLM:

```python
async def get_my_high_priority_issues() -> ToolResult[list[Issue]]:
    ...
```

Each tool has a matching JSON-schema entry in `agent/schemas.py` used only when constructing the `tools=[...]` param for Claude — the schema layer is the *only* place that knows about the LLM; `tools/*.py` never imports `anthropic`. This is the core "separation between orchestration and tool implementation" architectural decision from the ideation doc, and is directly unit-testable: call `get_my_high_priority_issues()` in a test with a mocked `httpx` response, assert on the returned `Issue` list — no LLM involved.

All four MVP tools (`get_my_high_priority_issues`, `get_issues_without_prs`, `get_my_open_prs`, `get_prs_awaiting_my_review`) take **zero arguments** — scope is entirely implicit, driven by the configured identity in `.env` (`JIRA_EMAIL` → `assignee = currentUser()`, `GITHUB_USERNAME` → `author:@me`/`review-requested:@me`). Each tool's JSON schema in `agent/schemas.py` therefore has an empty `"properties": {}` object. No MVP tool takes a filter/repo/project parameter.

### 6.6 Orchestration Loop (`agent/orchestrator.py`)

```python
MAX_ITERATIONS = 6

async def handle_query(query: str) -> AskResponse:
    messages = [{"role": "user", "content": query}]
    warnings: list[str] = []
    called_tools: list[str] = []

    for iteration in range(MAX_ITERATIONS):
        force_final = iteration == MAX_ITERATIONS - 1
        try:
            response = await anthropic_client.messages.create(
                model="claude-sonnet-5",
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                tool_choice={"type": "none"} if force_final else {"type": "auto"},
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise OrchestratorUnavailable(str(exc)) from exc

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return AskResponse(
                answer=extract_text(response.content),
                tool_calls=called_tools,
                warnings=warnings,
            )

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(*[
            dispatch(block, called_tools, warnings) for block in tool_use_blocks
        ])
        messages.append({"role": "user", "content": results})

    # Unreachable in practice: the forced tool_choice="none" turn above
    # always returns stop_reason != "tool_use". Kept as a defensive guard.
    raise OrchestratorUnavailable("model did not produce a final answer")
```

**Loop termination:** capped at `MAX_ITERATIONS` (6) round trips. On the final allowed iteration, `tool_choice` is forced to `"none"`, which requires Claude to produce a text answer from whatever tool results have already been gathered rather than requesting another tool call — this guarantees the loop terminates without an arbitrary hard error on a query that legitimately needs several tool calls.

**Anthropic API failures:** any `anthropic.APIError` (auth failure, network error, rate limit, etc.) raised by `messages.create()` is caught and re-raised as `OrchestratorUnavailable`, a distinct exception from the per-tool `ToolResult(ok=False, ...)` path below. `app/main.py` catches `OrchestratorUnavailable` at the route boundary and returns `503` with a generic error body (`{"detail": "assistant temporarily unavailable"}`) — the raw exception message is logged server-side only, never returned to the client, consistent with §10. This is distinct from a *tool* failing (which degrades gracefully into `warnings`, §6.3): the *model call itself* failing is a hard 503, since there's no partial answer to give without at least one successful model turn.

`dispatch()` looks up the tool in `ToolRegistry`, invokes it, catches `ToolResult(ok=False, ...)`, appends the tool's error to `warnings` instead of raising, and returns a `tool_result` content block either way — this is what makes partial-failure degradation work end-to-end (§6.3).

Concurrent tool_use blocks in a single Claude response (e.g. it asks for Jira and GitHub data in the same turn) are dispatched via `asyncio.gather`, satisfying the concurrency requirement from the research findings.

### 6.7 System Prompt Requirements

`SYSTEM_PROMPT` (a constant in `agent/orchestrator.py`) must instruct the model to:
- Act as an engineering-status assistant with access to read-only Jira/GitHub tools — call whichever tools are relevant to the question, including multiple tools in one turn when the question spans both systems (e.g. "what should I work on today" → high-priority issues + issues without PRs + PRs awaiting review)
- Base the final answer only on returned tool data — never fabricate ticket keys, PR numbers, or counts
- Produce a concise, prioritized natural-language summary, not a restatement of raw tool output
- Acknowledge partial results in the answer when `warnings` is non-empty (e.g. "GitHub data unavailable, showing Jira only") rather than silently omitting the gap
- For queries unrelated to Jira/GitHub work status, answer directly without calling any tool

This defines what the prompt must accomplish, not literal wording — the exact phrasing is an implementation detail to iterate on during Phase 1.

### 6.8 Ranking (`core/ranking.py`)

Pure, LLM-free functions run inside each tool before the result is returned:

```python
def score_issue(issue: RawIssue) -> float:
    score = PRIORITY_WEIGHTS[issue.priority]  # e.g. Highest=4, High=3, Medium=2, Low=1
    if issue.due_date and issue.due_date < today():
        score += OVERDUE_BONUS
    return score

def score_pr(pr: RawPR) -> float:
    age_days = (now() - pr.opened_at).days
    return age_days * (2 if pr.is_review_requested else 1)
```

Sorting/thresholding (e.g. "high priority" = score above a constant) happens here, in plain Python, independent of any LLM call — directly testable with fixed inputs/outputs.

### 6.9 GitHub PR Fetch Design

`get_my_open_prs()` and `get_prs_awaiting_my_review()` share a single low-level client method rather than each owning a separate HTTP call:

```python
class GitHubClient:
    async def search_prs(self, query: str) -> list[RawPR]:
        """Wraps GET /search/issues (Search API, PR-scoped)."""
        ...
```

Both tools call this same method with a different query string, then map the raw results into `PullRequest`, setting the appropriate flag:

```python
async def get_my_open_prs() -> ToolResult[list[PullRequest]]:
    raw = await github_client.search_prs(f"is:pr is:open author:{settings.github_username}")
    return ToolResult(ok=True, data=[to_pull_request(r, is_authored_by_me=True) for r in raw])

async def get_prs_awaiting_my_review() -> ToolResult[list[PullRequest]]:
    raw = await github_client.search_prs(f"is:pr is:open review-requested:{settings.github_username}")
    return ToolResult(ok=True, data=[to_pull_request(r, is_review_requested=True) for r in raw])
```

This keeps the HTTP/auth/pagination logic in exactly one place (`GitHubClient.search_prs`), while each tool stays a thin, independently testable mapping over a distinct query — consistent with §6.5's tool-independence goal. It also means the TTL cache in §6.11 only needs to key on the query string once, at the client layer, to benefit both tools.

### 6.10 Jira ↔ PR linking

`get_issues_without_prs()` fetches open high-priority issues via JQL, fetches the user's recent PRs (from the GitHub tool or a shared cache), and marks `has_linked_pr = True` if a regex match of the issue key (`[A-Z]+-\d+`) is found in any PR title or branch name. This avoids depending on Jira's `dev-status` endpoint per the research recommendation.

### 6.11 Caching (`core/cache.py`)

A minimal in-memory TTL cache (dict of `key -> (value, expires_at)`), no new dependency required. Wraps the two client-level fetch functions (`JiraClient.search()`, `GitHubClient.search()`) with a short TTL (e.g. 60s) to avoid repeat-request rate-limit exhaustion when a user asks multiple questions in a row against the running FastAPI process — this is safe specifically because the service is long-running (per the FastAPI-over-CLI decision), so the cache persists across requests within a session.

### 6.12 Config (`app/config.py`)

```python
class Settings(BaseSettings):
    anthropic_api_key: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    github_token: str
    github_username: str
    model_config = SettingsConfigDict(env_file=".env")
```

`.env.example` checked into the repo documents all required variables; `.env` itself is gitignored (already covered by the existing `.gitignore`'s `.env*` pattern).

## 7) User Experience

Local-only, single user, no UI. Interaction is:
```bash
uvicorn app.main:app --reload
curl -X POST localhost:8000/ask -d '{"query": "what should I work on today?"}'
```
A thin `scripts/ask.sh` or one-line `curl` wrapper may be added for demo convenience, but is not a formal deliverable (frontend is explicitly out of scope).

## 8) Testing Strategy

- **`test_ranking.py`** (unit): pure-function tests for `score_issue`/`score_pr` against fixed inputs — e.g. "overdue Highest-priority issue scores higher than non-overdue High-priority issue." Each test asserts a specific numeric/ordering outcome, not just "no exception raised."
- **`test_jira_tools.py` / `test_github_tools.py`** (unit): `respx`-mocked HTTP responses feed each tool function; assert the returned `Issue`/`PullRequest` objects and `has_linked_pr`/scoring are correct. Includes an edge case per tool where the mocked HTTP call returns a 5xx — assert `ToolResult(ok=False, error=...)` is returned, not an exception.
- **`test_orchestrator.py`** (unit/integration): mock `anthropic_client.messages.create` to return a scripted `tool_use` response followed by a scripted final `end_turn` response; assert the loop dispatches to the right registry entry and assembles `tool_calls`/`warnings` correctly. A dedicated test simulates one tool returning `ok=False` and asserts the loop continues and produces a `warnings` entry instead of crashing (validates §6.3/§6.6's partial-degradation behavior). Two additional tests validate the §6.6 robustness guards directly: (1) mock `messages.create` to always return `stop_reason == "tool_use"` and assert the loop stops at `MAX_ITERATIONS` with `tool_choice={"type": "none"}` forced on the final call, rather than looping forever; (2) mock `messages.create` to raise `anthropic.APIError` and assert `OrchestratorUnavailable` is raised rather than propagating the raw SDK exception.
- **`test_api.py`** (integration): FastAPI `TestClient` against `POST /ask` with the orchestrator's Anthropic client mocked at the boundary; asserts response shape and status code. A `GET /health` test confirms the endpoint requires no external calls or credentials to succeed. A dedicated test raises `OrchestratorUnavailable` from the mocked orchestrator and asserts `/ask` returns `503` with a generic (non-leaking) error body.
- **`test_github_tools.py`** additionally asserts `get_my_open_prs` and `get_prs_awaiting_my_review` both route through the same mocked `GitHubClient.search_prs` method (§6.9) with different query strings, and that each maps the shared raw response shape into the correct `PullRequest` flag (`is_authored_by_me` vs. `is_review_requested`).
- All external HTTP (Jira, GitHub, Anthropic) is mocked in every automated test — no test hits a real network call, so the suite is deterministic and runnable without live credentials.

## 9) Performance Considerations

- Concurrent `asyncio.gather` dispatch for multi-tool turns avoids sequential network latency stacking.
- In-memory TTL cache reduces redundant Jira/GitHub calls within a session, directly mitigating Jira's 2026 points-based rate limiting on repeated JQL queries.
- Typical request involves 1–3 Claude round trips (tool selection → tool results → narration); no streaming needed for MVP given short expected responses.

## 10) Security Considerations

- Credentials only in `.env` (gitignored), loaded via `pydantic-settings`; never logged, including in error messages returned to the client (`warnings` entries are sanitized to status codes/generic messages, not raw response bodies).
- GitHub PAT is fine-grained and scoped to the specific repos being monitored (documented in setup instructions), not a classic broad-scope token.
- Jira API token auth (Basic Auth over HTTPS) rather than storing a password.
- No user-supplied input is passed into JQL or GitHub search queries unescaped beyond the fixed query templates in `tools/*.py` — the natural-language query only ever selects *which* tool runs, never raw query string content, avoiding injection into JQL/search syntax.
- `/ask` has no authentication in MVP (localhost-only, single user) — explicitly noted as an out-of-scope gap, not an oversight, consistent with the "no production auth systems" project boundary.

## 11) Documentation

- `README.md`: setup (`.env.example`, install, run `uvicorn`), example `curl` requests, architecture diagram (reuse §6.1)
- `CLAUDE.md`: full rewrite of the remaining `[CUSTOMIZE]` placeholders — directory structure, actual dev commands (`uvicorn`, `pytest`), real env vars
- `.env.example` documenting all `Settings` fields

**Hooks (already resolved):** `.claude/settings.json`'s PostToolUse/Stop hooks were reconfigured to `ruff`/`mypy`/`pytest` (per-file guarded by extension so non-Python file edits, like this spec, no longer trigger them; `pytest` per-file is further guarded to only `test_*.py`/`*_test.py` filenames to avoid false failures on non-test source files). These commands aren't installed yet — Phase 1 must add `ruff`, `mypy`, and `pytest` as dev dependencies in `pyproject.toml`, or the hooks will fail with "command not found" on the first edit.

## 12) Implementation Phases

**Phase 1 — MVP core**
- `app/config.py`, `.env.example`
- `core/models.py`, `core/ranking.py` (+ tests)
- `clients/jira_client.py`, `clients/github_client.py`
- `tools/jira_tools.py` (`get_my_high_priority_issues`, `get_issues_without_prs`), `tools/github_tools.py` (`get_my_open_prs`, `get_prs_awaiting_my_review`) (+ tests)
- `agent/schemas.py`, `agent/registry.py`, `agent/orchestrator.py` (+ tests)
- `app/main.py` (`POST /ask`, `GET /health`) (+ integration tests)

**Phase 2 — Reliability**
- `core/cache.py` TTL caching wired into both clients
- Partial-failure `warnings` surfaced end-to-end (client → tool → orchestrator → API response)
- README + `CLAUDE.md` rewrite

**Phase 3 — Polish**
- `tool_calls` audit trail in response (if not already done in Phase 1)
- Optional: demo script comparing sequential vs. concurrent tool dispatch latency (mentioned in research as a strong, easy interview demo)
- Documented (not built) future-improvement notes: GraphQL for GitHub, OS keychain for credentials, GitHub App auth

## 13) Open Questions

- Exact Claude model id/version to pin in `orchestrator.py` (e.g. `claude-sonnet-5`) — confirm against current Anthropic API model availability at implementation time.
- Whether `get_issues_without_prs()` needs the user's PR list as an implicit dependency (requiring an internal call to the GitHub tool from within a Jira tool) or whether that cross-referencing belongs in the orchestrator/a dedicated aggregation step instead of inside `tools/jira_tools.py` — worth deciding during implementation to avoid tools silently depending on each other.
- Cache TTL value (60s suggested) — arbitrary starting point, not load-tested.

## 14) References

- `docs/ideation/engineering-productivity-agent-mvp.md` — ideation doc with full research citations (Anthropic tool-calling patterns, Jira/GitHub API research, architecture pitfalls)
- Anthropic Messages API tool use documentation
- Jira Cloud REST API v3 (`/rest/api/3/`) documentation
- GitHub REST Search API (`is:pr review-requested:@me`, `is:pr author:@me`) documentation
