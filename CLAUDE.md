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
│   ├── main.py                   # FastAPI app: POST /ask, GET /health
│   ├── config.py                 # pydantic-settings Settings, loaded from .env
│   ├── agent/
│   │   ├── orchestrator.py       # hand-rolled Claude tool-calling loop
│   │   ├── registry.py           # TOOL_REGISTRY: tool name -> callable
│   │   └── schemas.py            # JSON-schema tool definitions passed to Claude
│   ├── clients/
│   │   ├── jira_client.py        # thin httpx wrapper: auth, JQL search, TTL cache
│   │   └── github_client.py      # thin httpx wrapper: auth, search_prs(), TTL cache
│   ├── tools/
│   │   ├── jira_tools.py         # get_my_high_priority_issues, get_issues_without_prs
│   │   └── github_tools.py       # get_my_open_prs, get_prs_awaiting_my_review
│   ├── core/
│   │   ├── models.py             # Issue, PullRequest, ToolResult[T], AskResponse
│   │   ├── ranking.py            # score_issue(), score_pr() - pure, LLM-free
│   │   ├── cache.py              # TTLCache (in-memory, injectable clock for tests)
│   │   └── errors.py             # sanitize_error() - never leak raw exception text
│   └── tests/                    # pytest; all external HTTP mocked via respx
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
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`
- `GITHUB_TOKEN`, `GITHUB_USERNAME`

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
```

```python
# app/tools/*.py — the pattern every tool follows
try:
    raw = await some_client.fetch(...)
except Exception as exc:
    return ToolResult(ok=False, data=None, error=sanitize_error(exc))
return ToolResult(ok=True, data=mapped, error=None)
```

`sanitize_error()` (`app/core/errors.py`) ensures only a status code/exception type reaches the
client or the LLM — never a raw response body, which could contain sensitive data.

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

---

## Dependencies

Runtime (see `pyproject.toml` for the authoritative list):
- `fastapi` + `uvicorn` — HTTP service
- `anthropic` — Claude API client, used only in `app/agent/`
- `httpx` — async HTTP client for Jira/GitHub, used only in `app/clients/`
- `pydantic` + `pydantic-settings` — data models and `.env`-backed settings

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
