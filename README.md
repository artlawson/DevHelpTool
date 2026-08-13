# Engineering Productivity Agent

A local FastAPI service that answers natural-language questions about your current engineering work — "what should I work on today?", "which high-priority Jira tickets don't have PRs?", "which of my tickets already have a matching PR?", "what's awaiting my review?" — by orchestrating a hand-rolled Claude tool-calling loop over the Jira and GitHub REST APIs.

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
| `JIRA_PROJECT_KEY` | Your Jira project key, e.g. `AL` — scopes Agile board/sprint lookups to your project |
| `GITHUB_TOKEN` | Fine-grained GitHub PAT, scoped to the repos you want monitored |
| `GITHUB_USERNAME` | Your GitHub username, used to build `author:`/`review-requested:` queries |
| `SLACK_BOT_TOKEN` | *(optional)* Slack bot token (`xoxb-...`) — enables @-mention replies and the daily digest |
| `SLACK_APP_TOKEN` | *(optional)* Slack app-level token (`xapp-...`) — enables Socket Mode, no public endpoint needed |
| `SLACK_CHANNEL_ID` | *(optional)* Channel/DM ID the daily digest posts to |

## Running

```bash
uvicorn app.main:app --reload --reload-dir app
```

```bash
curl -X POST localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "what should I work on today?"}'
```

```json
{
  "answer": "You have 2 high-priority tickets without PRs: PROJ-123 and PROJ-140. You also have 1 PR awaiting your review: org/repo#42.",
  "tool_calls": ["jira_get_my_high_priority_issues", "jira_get_issues_without_prs", "github_get_prs_awaiting_my_review"],
  "warnings": []
}
```

`GET /health` is a liveness check that requires no credentials or network access.

**Scope note:** this is a local-only, single-user tool — `/ask` has no authentication, and there's no deployment story. That's intentional, not an oversight; see `specs/feat-engineering-productivity-agent-mvp.md` §4/§10 for the reasoning.

## Slack Integration (optional)

Slack is fully optional — everything above works exactly the same with no Slack credentials set.

### @-mention replies

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**.
2. Under **Socket Mode**, enable it and generate an app-level token with the `connections:write` scope — this is `SLACK_APP_TOKEN`. Socket Mode means no public HTTPS endpoint or tunnel is needed; the app connects out to Slack over a websocket.
3. Under **OAuth & Permissions**, add the `app_mentions:read` and `chat:write` bot scopes, then install the app to your workspace — this gives you `SLACK_BOT_TOKEN`.
4. Under **Event Subscriptions**, subscribe to the `app_mention` bot event.
5. Invite the bot to a channel (`/invite @YourBotName`) and copy that channel's ID for `SLACK_CHANNEL_ID`.

Add all three to `.env`, then start the server as usual — @-mention the bot and it replies in-thread using the same orchestrator as `/ask`.

### Daily digest

`scripts/post_digest.py` is a separate, short-lived script — it does **not** require the FastAPI server to be running. Schedule it with `launchd` (macOS):

Create `~/Library/LaunchAgents/com.devhelptool.digest.plist` (replace the two `/absolute/path/to/DevHelpTool` placeholders):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.devhelptool.digest</string>
    <key>ProgramArguments</key>
    <array>
        <string>/absolute/path/to/DevHelpTool/.venv/bin/python</string>
        <string>scripts/post_digest.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/absolute/path/to/DevHelpTool</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/devhelptool-digest.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/devhelptool-digest.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.devhelptool.digest.plist
```

It runs daily at 9:00am, posting a **High Priority** / **Upcoming** digest to `SLACK_CHANNEL_ID` — see `CLAUDE.md`'s Slack Integration section for exactly what determines each section's contents, and its Troubleshooting entry for checking whether a scheduled run actually fired.

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
