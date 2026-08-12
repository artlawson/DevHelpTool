# Task Breakdown: Engineering Productivity Agent — MVP

Generated: 2026-08-11
Source: `specs/feat-engineering-productivity-agent-mvp.md`

## Note on task tracking

STM ("Simple Task Master") is documented in this repo's `CLAUDE.md` as the task-tracking convention, but `stm` is not an installable package (`brew install anthropic/tap/stm` and `npm install -g @anthropic/stm` both 404 — the tap/package don't exist publicly). Tasks below are tracked with the session's built-in task tool instead. This document is the durable, self-contained source of truth for implementation — every task below is written to be actionable without re-reading the spec.

## Overview

Building a local FastAPI service that answers natural-language engineering-status questions ("what should I work on today?") by orchestrating a hand-rolled Claude tool-calling loop over Jira and GitHub REST APIs, with deterministic Python ranking and graceful partial-failure degradation. 16 tasks across 4 phases (scaffolding → MVP core → reliability → polish).

## Design decision made during decomposition

**Spec §13's second open question is now resolved**: `get_issues_without_prs()` (Task 1.6) calls `GitHubClient.search_prs()` **directly** (the client, not `tools/github_tools.py`) to fetch the user's recent PRs for issue-key matching. This keeps the dependency at the client layer (`tools/jira_tools.py` → `clients/github_client.py`) rather than tool-to-tool (`tools/jira_tools.py` → `tools/github_tools.py`), preserving each tool's independent testability per spec §6.5.

---

## Phase 0: Foundation

### Task 0.1: Project scaffolding & dependencies

**Description**: Initialize the Python project structure, dependency manifest, and dev tooling so every later task has somewhere to land and the already-reconfigured `.claude/settings.json` hooks (`ruff`/`mypy`/`pytest`) stop failing with "command not found."

**Size**: Small
**Priority**: High
**Dependencies**: None
**Can run parallel with**: nothing (blocks everything else)

**Technical Requirements** (spec §5, §6.2, §11):

Create `pyproject.toml` with these dependencies:
```toml
[project]
name = "engineering-productivity-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "anthropic",
    "httpx",
    "pydantic",
    "pydantic-settings",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "respx",
    "ruff",
    "mypy",
]
```

Create the file/module skeleton exactly as specified in spec §6.2 (empty `__init__.py` files where needed for package discovery):
```
app/
├── __init__.py
├── main.py
├── config.py
├── agent/
│   ├── __init__.py
│   ├── orchestrator.py
│   ├── registry.py
│   └── schemas.py
├── clients/
│   ├── __init__.py
│   ├── jira_client.py
│   └── github_client.py
├── tools/
│   ├── __init__.py
│   ├── jira_tools.py
│   └── github_tools.py
├── core/
│   ├── __init__.py
│   ├── models.py
│   ├── ranking.py
│   └── cache.py
└── tests/
    ├── __init__.py
    ├── test_ranking.py
    ├── test_jira_tools.py
    ├── test_github_tools.py
    ├── test_orchestrator.py
    └── test_api.py
```

**Implementation Steps**:
1. Create `pyproject.toml` with the dependency lists above
2. Create the full directory/file skeleton (empty files are fine — later tasks fill them in)
3. Install: `pip install -e ".[dev]"` (or the project's chosen venv/uv equivalent)
4. Verify hooks no longer error: touch a trivial `.py` file and confirm `ruff`/`mypy`/`pytest` run (even if they report "no tests" — the point is the binaries resolve)

**Acceptance Criteria**:
- [ ] `pyproject.toml` exists with all dependencies from spec §5
- [ ] Full directory skeleton from spec §6.2 exists
- [ ] `ruff --version`, `mypy --version`, `pytest --version` all succeed in the project venv
- [ ] Editing any `.py` file no longer produces "command not found" from the PostToolUse hooks in `.claude/settings.json`

---

## Phase 1: MVP Core

### Task 1.1: Config & settings

**Description**: Implement `app/config.py` with a `pydantic-settings` `Settings` class loading all required credentials/identity from `.env`, plus `.env.example` documenting them.

**Size**: Small
**Priority**: High
**Dependencies**: Task 0.1
**Can run parallel with**: Task 1.2

**Technical Requirements** (spec §6.12):

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

`.env.example` must document every field above (spec §11), and `.env` itself must remain gitignored — already covered by the existing `.gitignore`'s `.env*` pattern (do not modify `.gitignore`).

**Security note** (spec §10): credentials are loaded only via this settings object; no other module should read `os.environ` directly for these values.

**Implementation Steps**:
1. Write `app/config.py` with the `Settings` class exactly as above
2. Write `.env.example` with placeholder values for all six fields
3. Instantiate a module-level `settings = Settings()` singleton for other modules to import

**Acceptance Criteria**:
- [ ] `Settings` loads all six fields from a `.env` file
- [ ] Missing required field raises a clear pydantic validation error at import time
- [ ] `.env.example` documents all six variables with placeholder values
- [ ] Test: instantiating `Settings` with a temp `.env` file (via `monkeypatch`/`tmp_path`) produces the expected values; instantiating with a required field missing raises `ValidationError`

---

### Task 1.2: Core data models

**Description**: Implement `core/models.py` with the `Issue`, `PullRequest`, and generic `ToolResult[T]` Pydantic models that every tool and client uses as its shared vocabulary.

**Size**: Small
**Priority**: High
**Dependencies**: Task 0.1
**Can run parallel with**: Task 1.1

**Technical Requirements** (spec §6.4):

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

`ToolResult` is this repo's established Result-style success/failure convention (see `CLAUDE.md`'s `Result<T>` pattern) — every tool function returns one, so the orchestrator degrades gracefully per-tool instead of raising (spec §6.4, §6.6).

Also define the API-layer response model referenced throughout spec §6.3/§6.6:
```python
class AskResponse(BaseModel):
    answer: str
    tool_calls: list[str]
    warnings: list[str] = []
```

**Implementation Steps**:
1. Write `core/models.py` with `Issue`, `PullRequest`, `ToolResult[T]`, and `AskResponse` exactly as above
2. Confirm Python 3.12 generic syntax (`class ToolResult[T](BaseModel)`) works with the pinned Pydantic version; if not, fall back to `Generic[T]`/`TypeVar` from `typing`

**Acceptance Criteria**:
- [ ] All four models defined with the exact fields above
- [ ] `ToolResult[list[Issue]]` and `ToolResult[list[PullRequest]]` both construct and validate correctly
- [ ] Test: constructing `ToolResult(ok=True, data=[...], error=None)` and `ToolResult(ok=False, data=None, error="...")` both validate; `AskResponse(answer="...", tool_calls=[])` defaults `warnings` to `[]`

---

### Task 1.3: Ranking logic

**Description**: Implement `core/ranking.py` with pure, LLM-free scoring functions for issues and PRs, per spec §6.8.

**Size**: Small
**Priority**: High
**Dependencies**: Task 1.2
**Can run parallel with**: Task 1.4, 1.5

**Technical Requirements** (spec §6.8):

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

Sorting/thresholding (e.g. "high priority" = score above a constant) happens here, in plain Python, independent of any LLM call — directly testable with fixed inputs/outputs (spec §6.8). `PRIORITY_WEIGHTS` should map Jira's standard priority names (`Highest`, `High`, `Medium`, `Low`, `Lowest`) to numeric weights; `OVERDUE_BONUS` is a module-level constant tunable without touching call sites.

**Implementation Steps**:
1. Define `PRIORITY_WEIGHTS: dict[str, float]` and `OVERDUE_BONUS: float` as module constants
2. Implement `score_issue(issue) -> float` and `score_pr(pr) -> float` exactly per the spec logic above
3. Implement a `HIGH_PRIORITY_THRESHOLD` constant and a small `is_high_priority(score: float) -> bool` helper used by Task 1.6's `get_my_high_priority_issues`

**Acceptance Criteria**:
- [ ] `score_issue`: an overdue `Highest`-priority issue scores strictly higher than a non-overdue `High`-priority issue
- [ ] `score_issue`: priority ordering is monotonic (`Highest` > `High` > `Medium` > `Low` > `Lowest`) holding due date constant
- [ ] `score_pr`: a review-requested PR open 5 days scores higher than an authored (non-review-requested) PR open 5 days
- [ ] `score_pr`: score increases monotonically with `age_days` for a fixed `is_review_requested` value
- [ ] Each test asserts a specific numeric/ordering outcome, not just "no exception raised" (spec §8)

---

### Task 1.4: Jira client

**Description**: Implement `clients/jira_client.py`, a thin `httpx`-based wrapper around Jira Cloud REST API v3 for JQL search — no `jira` package dependency, per the architecture decision in spec §5/§6.2.

**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.1
**Can run parallel with**: Task 1.3, 1.5

**Technical Requirements** (spec §6.2, §5, §10):

```python
class JiraClient:
    def __init__(self, settings: Settings):
        self._base_url = settings.jira_base_url
        self._auth = httpx.BasicAuth(settings.jira_email, settings.jira_api_token)

    async def search(self, jql: str) -> list[dict]:
        """Wraps GET /rest/api/3/search/jql. Returns raw issue dicts."""
        async with httpx.AsyncClient(base_url=self._base_url, auth=self._auth) as client:
            response = await client.get(
                "/rest/api/3/search/jql",
                params={"jql": jql},
            )
            response.raise_for_status()
            return response.json()["issues"]
```

Auth is **API token via Basic Auth** (`email` + `api_token`), not OAuth 2.0 3LO — simpler for a single-user tool and exempt from Atlassian's 2026 points-based rate-limit tiers (per ideation research). Use Jira's `/rest/api/3/search/jql` endpoint (the current search endpoint as of the 2026 migration, not the deprecated `/search`).

**Implementation Steps**:
1. Implement `JiraClient.__init__` taking a `Settings` instance, storing base URL and `httpx.BasicAuth`
2. Implement `async def search(self, jql: str) -> list[dict]` calling `/rest/api/3/search/jql`, raising on non-2xx via `response.raise_for_status()`
3. Do **not** catch HTTP errors here — that's Task 1.6's responsibility (converts to `ToolResult(ok=False, ...)`); this client raises, callers decide how to handle it

**Acceptance Criteria**:
- [ ] `search()` sends a `GET` to `/rest/api/3/search/jql` with `jql` as a query param, using Basic Auth with the configured email/token
- [ ] Test (via `respx`): mocked 200 response returns the parsed `issues` list
- [ ] Test (via `respx`): mocked 401/500 response causes `search()` to raise (via `raise_for_status()`), not silently return an empty list
- [ ] No `jira` or `atlassian-python-api` package dependency introduced

---

### Task 1.5: GitHub client

**Description**: Implement `clients/github_client.py` with a single shared `search_prs(query)` method used by both GitHub tools, per spec §6.9 — this is the resolution to the "shared underlying fetch" design decision.

**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.1
**Can run parallel with**: Task 1.3, 1.4

**Technical Requirements** (spec §6.9, §5, §10):

```python
class GitHubClient:
    def __init__(self, settings: Settings):
        self._token = settings.github_token

    async def search_prs(self, query: str) -> list[dict]:
        """Wraps GET /search/issues (Search API, PR-scoped). Returns raw item dicts."""
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {self._token}"},
        ) as client:
            response = await client.get("/search/issues", params={"q": query})
            response.raise_for_status()
            return response.json()["items"]
```

Both `get_my_open_prs()` and `get_prs_awaiting_my_review()` (Task 1.7) call this **same method** with different query strings — `is:pr is:open author:{username}` and `is:pr is:open review-requested:{username}` respectively (spec §6.9) — keeping all HTTP/auth/pagination logic in exactly one place. `get_issues_without_prs()` (Task 1.6) also calls this method directly (not via `tools/github_tools.py`) to fetch the user's recent PRs for issue-key cross-referencing — this is the decomposition-time design decision resolving spec §13.

Auth: fine-grained GitHub PAT via `Authorization: Bearer {token}` header — no GitHub App, no OAuth flow (spec §10).

**Implementation Steps**:
1. Implement `GitHubClient.__init__` taking a `Settings` instance, storing the PAT
2. Implement `async def search_prs(self, query: str) -> list[dict]` calling `GET /search/issues` with `q=query`, raising on non-2xx
3. Do not build separate methods per use case — `search_prs` is intentionally generic; callers (Task 1.6, 1.7) differentiate via the query string they pass in

**Acceptance Criteria**:
- [ ] `search_prs()` sends a `GET` to `/search/issues` with `q` as the query param and `Authorization: Bearer {token}` header
- [ ] Test (via `respx`): mocked 200 response returns the parsed `items` list
- [ ] Test (via `respx`): mocked 401/403/500 response causes `search_prs()` to raise, not silently return an empty list
- [ ] No `PyGithub` package dependency introduced
- [ ] Only one HTTP-calling method exists on `GitHubClient` (no separate methods for "my PRs" vs "review requested" vs "for linking")

---

### Task 1.6: Jira tools

**Description**: Implement `tools/jira_tools.py` with `get_my_high_priority_issues()` and `get_issues_without_prs()`, including the Jira↔PR linking logic from spec §6.10.

**Size**: Large
**Priority**: High
**Dependencies**: Task 1.2, 1.3, 1.4, 1.5
**Can run parallel with**: Task 1.7

**Technical Requirements** (spec §6.5, §6.10, §6.8):

Both tools are plain async functions with **zero arguments** — scope is entirely implicit via `.env` identity (spec §6.5: `JIRA_EMAIL` → `assignee = currentUser()`):

```python
async def get_my_high_priority_issues() -> ToolResult[list[Issue]]:
    ...

async def get_issues_without_prs() -> ToolResult[list[Issue]]:
    ...
```

`get_my_high_priority_issues()`:
- JQL: `assignee = currentUser() AND priority in (High, Highest) AND resolution = Unresolved ORDER BY updated DESC`
- Call `jira_client.search(jql)`, map each raw issue dict to an `Issue`, compute `priority_score` via `core.ranking.score_issue()`
- On `JiraClient.search()` raising: catch the exception, return `ToolResult(ok=False, data=None, error=<sanitized message>)` — never let the exception propagate to the orchestrator (spec §6.6's per-tool degradation contract)

`get_issues_without_prs()` (spec §6.10):
- Fetch open high-priority issues via JQL (same or broader filter than above — all unresolved issues, not just High/Highest, since "without PRs" is itself the filter of interest; use `assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC`)
- Fetch the user's recent PRs by calling `github_client.search_prs(f"is:pr author:{settings.github_username}")` **directly** (the client, not `tools/github_tools.py` — this is the Task 1.5/decomposition design decision)
- For each issue, regex-match the issue key (`[A-Z]+-\d+`) against every fetched PR's `title` and the PR's branch ref (`head.ref` in the GitHub Search API response) to set `has_linked_pr`
- Return only issues where `has_linked_pr is False`
- On either `JiraClient.search()` or `GitHubClient.search_prs()` raising: catch, return `ToolResult(ok=False, ...)` — a GitHub-side failure here should not crash the whole Jira tool; it should report the tool itself as failed so the orchestrator's `warnings` mechanism (spec §6.6) surfaces it

**Implementation Steps**:
1. Implement a private `_map_issue(raw: dict) -> Issue` translating a raw Jira issue dict into the `Issue` model, computing `priority_score` via `ranking.score_issue`
2. Implement `get_my_high_priority_issues()` using the JQL above, wrapping `jira_client.search()` in try/except to produce `ToolResult`
3. Implement a private `_extract_pr_branch_and_title(raw_pr: dict) -> tuple[str, str]` helper for the GitHub Search API response shape
4. Implement `get_issues_without_prs()`: fetch issues, fetch PRs via `github_client.search_prs()`, regex-match, filter, wrap in `ToolResult`
5. Regex: `re.compile(r"[A-Z]+-\d+")` — search issue key in PR title/branch, not the reverse

**Acceptance Criteria**:
- [ ] `get_my_high_priority_issues()` builds the exact JQL from spec §6.10's research recommendation and returns issues sorted with `priority_score` populated
- [ ] `get_issues_without_prs()` correctly excludes an issue when its key appears in a PR title (e.g. `PROJ-123` in "Fix PROJ-123: null pointer") or branch name (e.g. `feature/PROJ-123-fix`)
- [ ] `get_issues_without_prs()` correctly includes an issue when no PR references its key
- [ ] Both tools return `ToolResult(ok=False, error=...)` (not a raised exception) when the underlying client call fails — test with `respx` mocking a 500 from Jira, and separately a 500 from the GitHub call inside `get_issues_without_prs()`
- [ ] Neither tool imports `anthropic` or references the LLM in any way (spec §6.5)

---

### Task 1.7: GitHub tools

**Description**: Implement `tools/github_tools.py` with `get_my_open_prs()` and `get_prs_awaiting_my_review()`, both built on the shared `GitHubClient.search_prs()` method from Task 1.5.

**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.2, 1.3, 1.5
**Can run parallel with**: Task 1.6

**Technical Requirements** (spec §6.9):

```python
async def get_my_open_prs() -> ToolResult[list[PullRequest]]:
    raw = await github_client.search_prs(f"is:pr is:open author:{settings.github_username}")
    return ToolResult(ok=True, data=[to_pull_request(r, is_authored_by_me=True) for r in raw])

async def get_prs_awaiting_my_review() -> ToolResult[list[PullRequest]]:
    raw = await github_client.search_prs(f"is:pr is:open review-requested:{settings.github_username}")
    return ToolResult(ok=True, data=[to_pull_request(r, is_review_requested=True) for r in raw])
```

Both tools are **zero-argument** (spec §6.5). `to_pull_request()` is a shared mapping helper (private to this module) translating a raw GitHub Search API item dict into a `PullRequest`, computing `age_score` via `core.ranking.score_pr()`. Wrap both tools' `github_client.search_prs()` calls in try/except to produce `ToolResult(ok=False, ...)` on failure — same per-tool degradation contract as Task 1.6.

**Implementation Steps**:
1. Implement `to_pull_request(raw: dict, *, is_authored_by_me: bool = False, is_review_requested: bool = False) -> PullRequest`, computing `age_score` via `ranking.score_pr`
2. Implement `get_my_open_prs()` calling `search_prs()` with the `author:` query, wrapped in try/except → `ToolResult`
3. Implement `get_prs_awaiting_my_review()` calling `search_prs()` with the `review-requested:` query, wrapped in try/except → `ToolResult`
4. Confirm both functions call the exact same `GitHubClient.search_prs` method object (not duplicated HTTP logic) — this is directly asserted in the test below

**Acceptance Criteria**:
- [ ] `get_my_open_prs()` queries `is:pr is:open author:{username}` and sets `is_authored_by_me=True` on all returned `PullRequest` objects
- [ ] `get_prs_awaiting_my_review()` queries `is:pr is:open review-requested:{username}` and sets `is_review_requested=True` on all returned `PullRequest` objects
- [ ] Test: mock `GitHubClient.search_prs` once and assert **both** tool functions invoke it (not separate HTTP-calling code paths) — validates spec §6.9's shared-fetch design decision
- [ ] Test: mocked 5xx from `search_prs()` causes `ToolResult(ok=False, error=...)`, not a raised exception
- [ ] `age_score` is populated on every returned `PullRequest` via `ranking.score_pr`

---

### Task 1.8: Tool schemas & registry

**Description**: Implement `agent/schemas.py` (JSON-schema tool definitions for Claude) and `agent/registry.py` (`ToolRegistry` mapping tool name → callable), the boundary layer that's the *only* place aware of the LLM (spec §6.5).

**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.6, 1.7
**Can run parallel with**: nothing (needs both tool modules finished)

**Technical Requirements** (spec §6.5):

All four tools take **zero arguments** — each schema entry has an empty `"properties": {}` object:

```python
TOOL_SCHEMAS = [
    {
        "name": "jira.get_my_high_priority_issues",
        "description": "Fetch the current user's assigned, unresolved High/Highest priority Jira issues.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira.get_issues_without_prs",
        "description": "Fetch the current user's unresolved Jira issues that have no linked GitHub pull request.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github.get_my_open_prs",
        "description": "Fetch the current user's open, authored GitHub pull requests.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github.get_prs_awaiting_my_review",
        "description": "Fetch open GitHub pull requests where the current user's review has been requested.",
        "input_schema": {"type": "object", "properties": {}},
    },
]
```

`ToolRegistry` maps each schema `name` to its actual callable from Task 1.6/1.7:
```python
TOOL_REGISTRY: dict[str, Callable[[], Awaitable[ToolResult]]] = {
    "jira.get_my_high_priority_issues": jira_tools.get_my_high_priority_issues,
    "jira.get_issues_without_prs": jira_tools.get_issues_without_prs,
    "github.get_my_open_prs": github_tools.get_my_open_prs,
    "github.get_prs_awaiting_my_review": github_tools.get_prs_awaiting_my_review,
}
```

**Implementation Steps**:
1. Write `agent/schemas.py` with `TOOL_SCHEMAS` exactly as above (four entries, all `properties: {}`)
2. Write `agent/registry.py` with `TOOL_REGISTRY` mapping each schema name to its Task 1.6/1.7 function
3. Confirm `tools/jira_tools.py` and `tools/github_tools.py` are imported **only** here and in `agent/orchestrator.py` — not the reverse (schemas/registry know about tools, tools don't know about schemas/registry)

**Acceptance Criteria**:
- [ ] `TOOL_SCHEMAS` has exactly 4 entries, each with `"input_schema": {"type": "object", "properties": {}}`
- [ ] `TOOL_REGISTRY` keys exactly match `TOOL_SCHEMAS` names (test: assert `set(TOOL_REGISTRY) == {s["name"] for s in TOOL_SCHEMAS}`)
- [ ] Each `TOOL_REGISTRY` value is directly callable with zero arguments and returns an awaitable `ToolResult`

---

### Task 1.9: Orchestrator

**Description**: Implement `agent/orchestrator.py` — the hand-rolled Claude tool-calling loop with the loop-termination cap, Anthropic-failure handling, and system prompt from spec §6.6/§6.7.

**Size**: Large
**Priority**: High
**Dependencies**: Task 1.8
**Can run parallel with**: nothing

**Technical Requirements** (spec §6.6, §6.7 — copied verbatim):

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

**`dispatch()` contract** (spec §6.6): looks up the tool in `TOOL_REGISTRY` by the `tool_use` block's name, invokes it, and:
- If the tool raises OR returns `ToolResult(ok=False, error=...)`: append the error message to `warnings`, and still return a `tool_result` content block to Claude (containing the error, so the model knows the call failed) — **never** let a tool failure propagate up and abort the whole request
- If the tool returns `ToolResult(ok=True, data=...)`: append the tool's name to `called_tools`, return a `tool_result` content block containing the serialized `data`

```python
async def dispatch(block, called_tools: list[str], warnings: list[str]) -> dict:
    tool_fn = TOOL_REGISTRY[block.name]
    try:
        result = await tool_fn()
    except Exception as exc:
        warnings.append(f"{block.name}: {exc}")
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"error: {exc}", "is_error": True}

    if not result.ok:
        warnings.append(f"{block.name}: {result.error}")
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"error: {result.error}", "is_error": True}

    called_tools.append(block.name)
    return {"type": "tool_result", "tool_use_id": block.id, "content": result.data.model_dump_json() if hasattr(result.data, "model_dump_json") else str(result.data)}
```

**`OrchestratorUnavailable` exception** (spec §6.6): a plain `Exception` subclass defined in this module, raised when `anthropic_client.messages.create()` itself fails (auth/network/rate-limit) — distinct from a tool failing. `app/main.py` (Task 1.10) catches this at the route boundary and returns `503`.

**`SYSTEM_PROMPT` requirements** (spec §6.7 — implement the actual prompt text satisfying these): 
- Act as an engineering-status assistant with access to read-only Jira/GitHub tools — call whichever tools are relevant, including multiple tools in one turn when the question spans both systems
- Base the final answer only on returned tool data — never fabricate ticket keys, PR numbers, or counts
- Produce a concise, prioritized natural-language summary, not a restatement of raw tool output
- Acknowledge partial results in the answer when tool results include errors, rather than silently omitting the gap
- For queries unrelated to Jira/GitHub work status, answer directly without calling any tool

**Implementation Steps**:
1. Define `OrchestratorUnavailable(Exception)` at module level
2. Write `SYSTEM_PROMPT` as a module-level string satisfying all five bullet points above
3. Implement `dispatch()` exactly per the contract above
4. Implement `handle_query()` exactly per the loop above, importing `TOOL_SCHEMAS`/`TOOL_REGISTRY` from Task 1.8's modules
5. Implement `extract_text(content) -> str` helper pulling the text block(s) out of a Claude response's `content` list

**Acceptance Criteria** (spec §8 test_orchestrator.py):
- [ ] Scripted `tool_use` → `end_turn` mock sequence: loop dispatches to the correct `TOOL_REGISTRY` entry and assembles `tool_calls`/`warnings` correctly
- [ ] One tool returning `ToolResult(ok=False, ...)`: loop continues (doesn't crash), produces a `warnings` entry, still reaches a final answer
- [ ] `messages.create` mocked to always return `stop_reason == "tool_use"`: loop stops at `MAX_ITERATIONS`, with `tool_choice={"type": "none"}` forced on the final call
- [ ] `messages.create` mocked to raise `anthropic.APIError`: `OrchestratorUnavailable` is raised, not the raw SDK exception
- [ ] Concurrent `tool_use` blocks in one response are dispatched via `asyncio.gather` (test: assert both tools were awaited concurrently, not sequentially — e.g. via mock call-order/timing assertions)

---

### Task 1.10: FastAPI app

**Description**: Implement `app/main.py` with `POST /ask` and `GET /health`, wiring the orchestrator to HTTP and mapping `OrchestratorUnavailable` to a sanitized `503`.

**Size**: Medium
**Priority**: High
**Dependencies**: Task 1.9
**Can run parallel with**: nothing

**Technical Requirements** (spec §6.3, §6.6, §10):

Request/response contract:
```json
// POST /ask request
{ "query": "what should I work on today?" }

// POST /ask response (200)
{
  "answer": "...",
  "tool_calls": ["jira.get_my_high_priority_issues", "..."],
  "warnings": []
}

// POST /ask response (503, on OrchestratorUnavailable)
{ "detail": "assistant temporarily unavailable" }
```

`GET /health` requires no auth and no external calls — pure liveness check (spec §6.3).

Security (spec §10): the `503` error body must **never** include the raw `OrchestratorUnavailable` message or any underlying exception text — log it server-side only (e.g. via Python's `logging` module), return only the generic detail string to the client.

**Implementation Steps**:
1. Define a Pydantic `AskRequest(BaseModel)` with a required `query: str` field
2. Implement `POST /ask`: call `orchestrator.handle_query(request.query)`, return the `AskResponse` on success
3. Add an exception handler (FastAPI `@app.exception_handler(OrchestratorUnavailable)` or a try/except in the route) that logs the real exception and returns `JSONResponse(status_code=503, content={"detail": "assistant temporarily unavailable"})`
4. Implement `GET /health` returning `{"status": "ok"}` with no dependencies on `Settings`, the Anthropic client, or any external call

**Acceptance Criteria** (spec §8 test_api.py):
- [ ] `POST /ask` with the orchestrator mocked to return a normal `AskResponse`: response body/status match the contract above
- [ ] `POST /ask` with the orchestrator mocked to raise `OrchestratorUnavailable`: response is `503` with exactly `{"detail": "assistant temporarily unavailable"}` — no leaked exception text
- [ ] `GET /health` returns `200` with no credentials configured and no network access (test by not setting any `.env` values in the test environment for this specific test)
- [ ] All external HTTP (Jira, GitHub, Anthropic) is mocked — no test in `test_api.py` hits a real network call (spec §8)

---

## Phase 2: Reliability

### Task 2.1: TTL caching

**Description**: Implement `core/cache.py` and wire it into `JiraClient.search()` and `GitHubClient.search_prs()` to reduce redundant API calls within a running session, per spec §6.11.

**Size**: Medium
**Priority**: Medium
**Dependencies**: Task 1.4, 1.5
**Can run parallel with**: Task 2.3, 2.4

**Technical Requirements** (spec §6.11):

"A minimal in-memory TTL cache (dict of `key -> (value, expires_at)`), no new dependency required." Wraps the two client-level fetch methods with a short TTL (suggested starting value: 60s, per spec §13's flagged-as-arbitrary open question — tunable, not load-tested). Safe specifically because the FastAPI service is long-running (spec §6.9: "the TTL cache... only needs to key on the query string once, at the client layer, to benefit both [GitHub] tools").

```python
class TTLCache:
    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: object) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)
```

**Implementation Steps**:
1. Implement `TTLCache` in `core/cache.py` exactly as above (or equivalent), with `get`/`set` keyed by an arbitrary string
2. In `JiraClient.search()`: check cache by `jql` string before making the HTTP call; populate cache with the result on a cache miss
3. In `GitHubClient.search_prs()`: check cache by `query` string before making the HTTP call; populate cache with the result on a cache miss
4. Each client owns its own `TTLCache` instance (not a shared global) to keep client tests independent

**Acceptance Criteria**:
- [ ] Test: calling `JiraClient.search(jql)` twice within the TTL with the same `jql` results in exactly one HTTP call (mock assertion on call count)
- [ ] Test: calling with a different `jql` string results in a second, independent HTTP call
- [ ] Test: after manipulating/mocking time past the TTL, a repeated call results in a fresh HTTP call
- [ ] Same three behaviors verified for `GitHubClient.search_prs()`

---

### Task 2.2: End-to-end partial-failure verification

**Description**: Integration-test (and patch any gaps found in) the full partial-failure path from a single failing tool through to the `/ask` HTTP response, per spec §6.3/§6.6's degradation contract.

**Size**: Small
**Priority**: Medium
**Dependencies**: Task 1.6, 1.7, 1.9, 1.10
**Can run parallel with**: Task 2.1, 2.3, 2.4

**Technical Requirements** (spec §6.3):

Given a query that triggers both a Jira tool and a GitHub tool, with the GitHub client mocked to fail (e.g. 503), the end-to-end `/ask` response must match:
```json
{
  "answer": "GitHub data is currently unavailable, so this only reflects Jira: ...",
  "tool_calls": ["jira.get_my_high_priority_issues"],
  "warnings": ["github.get_prs_awaiting_my_review: <error detail>"]
}
```
with HTTP status **200** (not an error status) — a partial answer is still a valid answer (spec §6.3).

**Implementation Steps**:
1. Write an integration test in `test_api.py` (or a new `test_integration.py`) that mocks `GitHubClient.search_prs` to raise, mocks the Anthropic client to request both a Jira and GitHub tool in one turn, and asserts the full response shape above
2. If any layer in the chain (client → tool → dispatch → orchestrator → route) currently swallows or mishandles the warning, patch it — this task exists specifically to catch integration gaps between the individually-tested units from Phase 1

**Acceptance Criteria**:
- [ ] End-to-end test confirms `warnings` is populated and `tool_calls` excludes the failed tool, with a `200` status
- [ ] End-to-end test confirms the successful Jira data is still present and correctly ranked in the response despite the GitHub failure

---

### Task 2.3: README

**Description**: Write `README.md` covering setup, environment configuration, running the service, and example requests.

**Size**: Small
**Priority**: Medium
**Dependencies**: Task 1.10
**Can run parallel with**: Task 2.1, 2.4

**Technical Requirements** (spec §7, §11):

Must include:
- Install instructions (`pip install -e ".[dev]"` or equivalent from Task 0.1)
- `.env` setup pointing to `.env.example` (Task 1.1)
- Run instructions: `uvicorn app.main:app --reload`
- Example request:
  ```bash
  curl -X POST localhost:8000/ask -d '{"query": "what should I work on today?"}'
  ```
- Architecture diagram — reuse spec §6.1 verbatim:
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
- Note that this is local-only, single-user, no auth (spec §10) — not a production deployment

**Acceptance Criteria**:
- [ ] A new developer can go from clone → running service using only the README
- [ ] Example `curl` command works against a locally running instance with valid `.env` credentials

---

### Task 2.4: CLAUDE.md rewrite

**Description**: Replace the remaining `[CUSTOMIZE]` placeholders in the repo's `CLAUDE.md` with the real directory structure, dev commands, and environment variables now that the project exists.

**Size**: Small
**Priority**: Low
**Dependencies**: Task 1.10
**Can run parallel with**: Task 2.1, 2.3

**Technical Requirements** (spec §11):

Update these `CLAUDE.md` sections specifically:
- **Directory Structure**: replace the generic React/TS example tree with the actual tree from spec §6.2
- **Local Development**: replace `npm install`/`npm run dev`/`npm test` with `pip install -e ".[dev]"` / `uvicorn app.main:app --reload` / `pytest`
- **Environment Variables**: replace the generic `DATABASE_URL`/`API_KEY` example with the real six variables from Task 1.1's `Settings`
- Leave the "Project Purpose" section as-is (already customized per the conversation history)

**Implementation Steps**:
1. Edit `CLAUDE.md`'s Directory Structure section to match spec §6.2
2. Edit Local Development section with the real commands
3. Edit Environment Variables section with the real six settings fields (names only, not example values — never put real credentials in a checked-in file)

**Acceptance Criteria**:
- [ ] No `[CUSTOMIZE]` markers remain in `CLAUDE.md`
- [ ] Directory structure, dev commands, and env vars all match what Tasks 0.1/1.1 actually built

---

## Phase 3: Polish

### Task 3.1: Verify tool_calls audit trail

**Description**: Confirm the `tool_calls` transparency field (spec §6.3) is fully populated end-to-end; this is likely already satisfied by Task 1.9's `dispatch()`/`handle_query()` implementation — this task is a verification pass, not new functionality.

**Size**: Small
**Priority**: Low
**Dependencies**: Task 1.9, 1.10
**Can run parallel with**: Task 3.2, 3.3

**Technical Requirements** (spec §6.3, §12 Phase 3):

`tool_calls` is included for transparency/debuggability and as an interview talking point ("here's the audit trail of what the agent decided to call") — spec §6.3.

**Implementation Steps**:
1. Write/confirm a test asserting that a multi-tool query's `/ask` response includes all successfully-called tool names, in call order, in `tool_calls`
2. If any gap is found (e.g. a tool call succeeds but isn't recorded), patch `dispatch()` from Task 1.9

**Acceptance Criteria**:
- [ ] A query invoking 3 tools (2 succeed, 1 fails) produces `tool_calls` with exactly the 2 successful tool names, and `warnings` with the 1 failure — no successful call missing, no failed call incorrectly included

---

### Task 3.2 (Optional/stretch): Concurrency demo script

**Description**: A small standalone script demonstrating the latency difference between sequential and concurrent tool dispatch — an easy, tangible interview demo per the ideation research.

**Size**: Small
**Priority**: Low (optional — cut first if time-constrained)
**Dependencies**: Task 1.9
**Can run parallel with**: Task 3.1, 3.3

**Technical Requirements** (spec §12 Phase 3):

"Optional: demo script comparing sequential vs. concurrent tool dispatch latency (mentioned in research as a strong, easy interview demo)."

**Implementation Steps**:
1. Write `scripts/demo_concurrency.py` that calls all 4 tools once sequentially (timed) and once via `asyncio.gather` (timed), printing both durations
2. No test required — this is a demo artifact, not shipped functionality

**Acceptance Criteria**:
- [ ] Running the script against real or mocked tools prints a visible latency difference favoring the concurrent path

---

### Task 3.3: Future-improvements documentation

**Description**: Document (without building) the deferred future improvements identified during research/spec: GraphQL for GitHub, OS keychain for credentials, GitHub App auth.

**Size**: Small
**Priority**: Low
**Dependencies**: Task 2.3
**Can run parallel with**: Task 3.1, 3.2

**Technical Requirements** (spec §12 Phase 3, ideation doc research):

Add a "Future Improvements" section to `README.md` noting:
- GitHub GraphQL API as a more efficient alternative to the REST Search API for multi-repo queries (ideation research §3)
- OS keychain (`keyring` package) as a more production-grade credential store than `.env` (spec clarification #4)
- GitHub App auth (short-lived installation tokens, org-scoped, auditable) as the "correct" production answer vs. a personal PAT (ideation research §3)

**Implementation Steps**:
1. Add a "Future Improvements" section to the README (Task 2.3) with the three items above, one sentence each on what and why

**Acceptance Criteria**:
- [ ] README documents all three deferred improvements with a one-line rationale each
