# Engineering Productivity Agent

A local FastAPI service that answers natural-language questions about your current engineering work — "what should I work on today?", "which high-priority Jira tickets don't have PRs?", "what's awaiting my review?" — by orchestrating a hand-rolled Claude tool-calling loop over the Jira and GitHub REST APIs.

Data is fetched and deterministically ranked in Python (priority, due date, review-request age); Claude's role is limited to deciding which tools to call and narrating the final answer — it never decides what's "important."

## Architecture

```
Client (curl / thin CLI)
        │  POST /ask {"query": "..."}
        ▼
FastAPI app (app/main.py)
        │
        ▼
Orchestrator (app/agent/orchestrator.py)
  - hand-rolled loop against the anthropic SDK
  - dispatches tool_use blocks via TOOL_REGISTRY
        │
        ├──► app/tools/jira_tools.py    ──► app/clients/jira_client.py    ──► Jira REST v3
        └──► app/tools/github_tools.py  ──► app/clients/github_client.py  ──► GitHub REST Search API
        │
        ▼
app/core/ranking.py (deterministic scoring, pure functions)
        │
        ▼
Claude narrates the final answer from ranked, structured tool results
        │
        ▼
FastAPI returns {"answer": "...", "tool_calls": [...], "warnings": [...]}
```

Tool implementations never import `anthropic` — `app/agent/schemas.py` and `app/agent/registry.py` are the only modules aware of the LLM. Every tool returns a `ToolResult` (`ok`/`data`/`error`), so one integration failing degrades the response gracefully instead of crashing the whole request.

## Setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key used for the tool-calling loop |
| `JIRA_BASE_URL` | Your Jira Cloud instance, e.g. `https://your-domain.atlassian.net` |
| `JIRA_EMAIL` | Email of the Jira account tied to the API token |
| `JIRA_API_TOKEN` | Jira API token (Basic Auth) — [create one here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `GITHUB_TOKEN` | Fine-grained GitHub PAT, scoped to the repos you want monitored |
| `GITHUB_USERNAME` | Your GitHub username, used to build `author:`/`review-requested:` queries |

## Running

```bash
uvicorn app.main:app --reload
```

```bash
curl -X POST localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "what should I work on today?"}'
```

```json
{
  "answer": "You have 2 high-priority tickets without PRs: PROJ-123 and PROJ-140. You also have 1 PR awaiting your review: org/repo#42.",
  "tool_calls": ["jira.get_my_high_priority_issues", "jira.get_issues_without_prs", "github.get_prs_awaiting_my_review"],
  "warnings": []
}
```

`GET /health` is a liveness check that requires no credentials or network access.

**Scope note:** this is a local-only, single-user tool — `/ask` has no authentication, and there's no deployment story. That's intentional, not an oversight; see `specs/feat-engineering-productivity-agent-mvp.md` §4/§10 for the reasoning.

## Development

```bash
ruff check app/       # lint
mypy app/              # type check
pytest app/tests/      # tests (all external HTTP is mocked - no live credentials needed)
```

## Future Improvements

Deferred intentionally for MVP scope — documented here rather than built:

- **GitHub GraphQL API** instead of the REST Search API — a single query could pull PR/review data across multiple repos in one round trip, more efficient than the current per-query REST calls.
- **OS keychain (`keyring` package)** instead of `.env` for credential storage — avoids plaintext secrets on disk.
- **GitHub App auth** (short-lived, org-scoped, auditable installation tokens) instead of a personal access token — the "correct" production answer, but overkill for a single-user local tool.

## Project Docs

- `specs/feat-engineering-productivity-agent-mvp.md` — full technical spec
- `specs/feat-engineering-productivity-agent-mvp-tasks.md` — implementation task breakdown
- `docs/ideation/engineering-productivity-agent-mvp.md` — original research and design decisions
