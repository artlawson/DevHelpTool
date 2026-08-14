from datetime import datetime

from app.clients.github_client import GitHubClient
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import PullRequest, ToolResult
from app.core.ranking import RawPR, score_pr

github_client = GitHubClient(settings)


def _repo_full_name(repository_url: str) -> str:
    return "/".join(repository_url.rstrip("/").split("/")[-2:])


def to_pull_request(
    raw: dict,
    *,
    is_authored_by_me: bool = False,
    is_review_requested: bool = False,
) -> PullRequest:
    opened_at = datetime.fromisoformat(raw["created_at"])
    raw_pr = RawPR(opened_at=opened_at, is_review_requested=is_review_requested)
    score = score_pr(raw_pr)
    return PullRequest(
        repo=_repo_full_name(raw["repository_url"]),
        number=raw["number"],
        title=raw["title"],
        url=raw["html_url"],
        opened_at=opened_at,
        is_review_requested=is_review_requested,
        is_authored_by_me=is_authored_by_me,
        age_score=score,
    )


async def get_my_open_prs() -> ToolResult[list[PullRequest]]:
    query = f"is:pr is:open author:{settings.github_username}"
    try:
        raw = await github_client.search_prs(query)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    return ToolResult(
        ok=True,
        data=[to_pull_request(item, is_authored_by_me=True) for item in raw],
        error=None,
    )


async def get_prs_awaiting_my_review() -> ToolResult[list[PullRequest]]:
    query = f"is:pr is:open review-requested:{settings.github_username}"
    try:
        raw = await github_client.search_prs(query)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    return ToolResult(
        ok=True,
        data=[to_pull_request(item, is_review_requested=True) for item in raw],
        error=None,
    )


# Digest-only (not in TOOL_REGISTRY/TOOL_SCHEMAS - same pattern as
# jira_tools.get_current_sprint_issues etc.), since it's discovery for the
# scheduled digest, not something the /ask orchestrator needs to call on demand.
# Scoped to a single repo (settings.github_repo) rather than an org-wide or
# unscoped search, which would otherwise match essentially all of public GitHub -
# skips the search entirely (empty result, not an error) if unset, so this
# stays optional the same way Slack's settings are.
async def get_prs_i_could_review() -> ToolResult[list[PullRequest]]:
    if not settings.github_repo:
        return ToolResult(ok=True, data=[], error=None)

    query = (
        f"is:pr is:open repo:{settings.github_repo} "
        f"-author:{settings.github_username} "
        f"-review-requested:{settings.github_username}"
    )
    try:
        raw = await github_client.search_prs(query)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error=sanitize_error(exc))

    return ToolResult(ok=True, data=[to_pull_request(item) for item in raw], error=None)
