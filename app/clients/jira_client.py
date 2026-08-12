import httpx

from app.config import Settings
from app.core.cache import TTLCache

_ISSUE_FIELDS = "summary,priority,status,duedate"


class JiraClient:
    def __init__(self, settings: Settings, cache: TTLCache | None = None):
        self._base_url = settings.jira_base_url
        self._auth = httpx.BasicAuth(settings.jira_email, settings.jira_api_token)
        self._cache = cache if cache is not None else TTLCache()

    async def search(self, jql: str) -> list[dict]:
        """Wraps GET /rest/api/3/search/jql. Returns raw issue dicts."""
        cached = self._cache.get(jql)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get(
                "/rest/api/3/search/jql",
                # search/jql omits `key` and `fields` unless explicitly requested.
                params={"jql": jql, "fields": _ISSUE_FIELDS},
            )
            response.raise_for_status()
            issues = response.json()["issues"]

        self._cache.set(jql, issues)
        return issues
