# Engineering Productivity Agent

A local FastAPI service that answers natural-language questions about your current engineering work — "what should I work on today?", "which high-priority Jira tickets don't have PRs?", "which of my tickets already have a matching PR?", "what's awaiting my review?", "what is Sam working on?" — by orchestrating a hand-rolled Claude tool-calling loop over the Jira and GitHub REST APIs.

Data is fetched and deterministically ranked in Python (priority, due date, review-request age); Claude's role is limited to deciding which tools to call and narrating the final answer — it never decides what's "important."

## Architecture

Three entry points, all calling the same orchestrator — only `/ask` goes through FastAPI at all;
Slack and the CLI call `handle_query()` directly, in-process, no HTTP round trip:

- `curl` / any HTTP client → `POST /ask` → `app/main.py`
- Slack @-mention → `app/slack/bolt_app.py` (Socket Mode, in-process)
- `devhelp <query>` → `app/cli.py`

```
                    (any of the three entry points above)
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
AskResponse{"answer", "tool_calls", "warnings", ...} → HTTP JSON / Slack thread reply / stdout
```

Tool implementations never import `anthropic` — `app/agent/schemas.py` and `app/agent/registry.py` are the only modules aware of the LLM. Every tool returns a `ToolResult` (`ok`/`data`/`error`), so one integration failing degrades the response gracefully instead of crashing the whole request. `devhelp standup` is the one exception to "all roads lead to the orchestrator" — it bypasses the LLM entirely and calls two tools directly, same as Slack's standup-summary button (see CLAUDE.md's CLI section for why).

## Setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**If this project lives under `~/Desktop` or `~/Documents` with iCloud Drive syncing those
folders**, the `devhelp` console script (below) may intermittently fail with
`ModuleNotFoundError: No module named 'app'`, even though everything else (`pytest`, `uvicorn`)
works fine — iCloud periodically evicts the small `.pth` file the editable install relies on to
make `app` importable, and `pip install -e ...` (in any mode) doesn't fix this durably. See
CLAUDE.md's Troubleshooting section for the full explanation and two fixes: a workaround that
always works (`python -m app.cli <query>` instead of `devhelp <query>`) and a durable one
(exclude `.venv` from iCloud sync).

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
| `GITHUB_REPO` | *(optional)* `"owner/repo"` — scopes the digest's "PRs You Could Review" section; that section is skipped if unset |
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
  "warnings": [],
  "referenced_issues": [],
  "referenced_prs": []
}
```

`referenced_issues`/`referenced_prs` are the actual Issue/PullRequest objects fetched by tool calls this turn (empty above since the tools were mocked in this example) - Slack's formatting layer uses them to turn plain-text ticket/PR mentions in `answer` into real hyperlinks, but they're present on every `/ask` response regardless of caller.

`GET /health` is a liveness check that requires no credentials or network access.

**Scope note:** this is a local-only, single-user tool — `/ask` has no authentication, and there's no deployment story. That's intentional, not an oversight; see `specs/feat-engineering-productivity-agent-mvp.md` §4/§10 for the reasoning.

### CLI

A `devhelp` command is installed with the package (see Setup above) as a thin terminal wrapper
over the same orchestrator used by `/ask` and the Slack @-mention — no FastAPI server needs to
be running:

```bash
devhelp
Ask: what should I work on today?
...
Ask: does that top one have a due date?
...
Ask:
```

Run it with no arguments and it opens a **persistent session**: each answer stays in context for
the next question (so "does that top one have a due date?" resolves correctly without repeating
the ticket key), until you end it with a blank line, Ctrl-D, or typing `exit`/`quit`. Typed input
never goes through the shell's parser either way, so punctuation like `?` or `*` just works.

Passing the query as arguments instead stays single-shot — one answer, then exit, no persistent
context — which is what you want from a script or a one-off shell command: `devhelp "what should
I work on today?"` (quote it so the shell, zsh in particular, doesn't try to glob-expand `?`/`*`).
If the note above about iCloud/Desktop applies to you, substitute `python -m app.cli` for
`devhelp` in either form.

`devhelp standup` (that one word, nothing else) prints the same Doing/Reviewing/Next Up summary
as Slack's standup-summary button — it bypasses the LLM entirely and calls the same two tools
directly, so it's instant and doesn't touch the Anthropic API:

```bash
devhelp standup
Doing:
  🔴 AL-12 — Refactor caching layer
Reviewing:
  • acme/widgets#514 — Fix auth bug
```

Both the free-form answer and `devhelp standup`'s output turn Jira issue keys and PR mentions
into clickable links when stdout is a real terminal — the equivalent of Slack's hyperlinking,
using the OSC 8 escape sequence most modern terminal emulators support (iTerm2, Terminal.app,
kitty, Windows Terminal, VS Code's integrated terminal). Piped or redirected output
(`devhelp standup > log.txt`, `devhelp ... | grep ...`) automatically falls back to plain text —
this is detected per run via `sys.stdout.isatty()`, not a setting.

Each answer prints to stdout; any tool-call warnings print to stderr. In the persistent
no-args session, a blank line/Ctrl-D/`exit`/`quit` always exits `0`, and a failed Anthropic call
mid-session just prints a warning and prompts again rather than ending the session. In the
single-shot (direct-argument) form, a blank/whitespace-only query exits `2`, and a failed
Anthropic call exits `1` — both end the process immediately, since there's no session to keep
open.

### Leaving a note on a ticket

If you ask a question and mention you don't have time for a ticket but have a quick thought on
it ("I don't have time for AL-16 right now, but a quick thought: ..."), the assistant drafts a
Jira **comment** (never edits the ticket's description) and always asks you to confirm before
anything is actually posted — nothing is written to Jira without an explicit yes:

```
devhelp "I don't have time for AL-16, but a quick thought: add a before/after comparison"
I've drafted that note on AL-16. It hasn't been posted yet.

Draft comment for AL-16:
  "add a before/after comparison"
Post this to Jira? [y/N]: y
Comment posted to AL-16.
```

In Slack, the same drafted note appears as its own block under the answer with two buttons,
**Post to Jira** and **Discard** — clicking Post posts the comment under your own configured
Jira account (no separate credential needed) and updates that block in place to confirm, without
touching the rest of the message.

## Slack Integration (optional)

Slack is fully optional — everything above works exactly the same with no Slack credentials set.

### @-mention replies

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**.
2. Under **Socket Mode**, enable it and generate an app-level token with the `connections:write` scope — this is `SLACK_APP_TOKEN`. Socket Mode means no public HTTPS endpoint or tunnel is needed; the app connects out to Slack over a websocket.
3. Under **OAuth & Permissions**, add the `app_mentions:read`, `chat:write`, and `channels:history` bot scopes (the last one lets the bot read plain replies inside a thread it started — see "Continuing a conversation" below; add `groups:history`/`im:history`/`mpim:history` too if the bot will also be used in a private channel, DM, or group DM), then install the app to your workspace — this gives you `SLACK_BOT_TOKEN`.
4. Under **Event Subscriptions**, subscribe to both the `app_mention` and `message.channels` bot events (add `message.groups`/`message.im`/`message.mpim` to match whichever extra scopes you added in step 3).
5. Under **Interactivity & Shortcuts**, toggle it **on**. This is required for the reply buttons (Quick Links, standup summary, comment-draft confirm) to work — no Request URL field needs filling in, since Socket Mode delivers button clicks the same way it delivers events, but Slack won't send them at all unless this toggle is on.
6. Invite the bot to a channel (`/invite @YourBotName`) and copy that channel's ID for `SLACK_CHANNEL_ID`.

**If you're adding scopes/events to an app that's already installed:** Slack requires you to
reinstall the app to the workspace any time you add a scope or event subscription — the OAuth &
Permissions page shows a banner/button for this. Skipping the reinstall step is the most common
reason a newly-added event silently never arrives.

### Continuing a conversation

Once the bot replies in a thread (via @-mention), you can keep replying in that same thread
**without** @-mentioning it again — each plain reply continues the conversation with the earlier
turns still in context, the same way the CLI's persistent session works. This only applies to
threads the bot itself started; a reply to any other thread is left alone. The existing
standup-summary and comment-draft-confirm buttons keep working on every reply, initial or
follow-up. Conversation history is kept in memory per-thread and capped to the last few exchanges
(`MAX_HISTORY_TURNS` in `app/agent/orchestrator.py`) to bound growing token cost, and is lost if
the app process restarts.

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
- **PR-linking for `get_persons_open_issues`** — currently Jira-only; the existing PR-linking helper is hardcoded to your own GitHub username, so extending it to look up an arbitrary teammate's authored PRs is a separate change.
- **Comment pagination** for `get_issues_awaiting_my_response` — Jira's comment endpoint returns at most 50 comments per page; an issue with more than that would only have its first page inspected for an unanswered mention.

## Project Docs

- `specs/feat-engineering-productivity-agent-mvp.md` — full technical spec
- `specs/feat-engineering-productivity-agent-mvp-tasks.md` — implementation task breakdown
- `docs/ideation/engineering-productivity-agent-mvp.md` — original research and design decisions
