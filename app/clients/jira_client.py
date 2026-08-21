import httpx

from app.config import Settings
from app.core.cache import TTLCache

_ISSUE_FIELDS = "summary,priority,status,duedate,description"


class JiraClient:
    def __init__(self, settings: Settings, cache: TTLCache | None = None):
        self._base_url = settings.jira_base_url
        self._auth = httpx.BasicAuth(settings.jira_email, settings.jira_api_token)
        self._project_key = settings.jira_project_key
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

    async def get_boards(self) -> list[dict]:
        """Wraps GET /rest/agile/1.0/board, scoped to the configured project."""
        cache_key = f"boards:{self._project_key}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get(
                "/rest/agile/1.0/board",
                params={"projectKeyOrId": self._project_key},
            )
            response.raise_for_status()
            boards = response.json()["values"]

        self._cache.set(cache_key, boards)
        return boards

    async def _get_sprints(self, board_id: int, state: str) -> list[dict]:
        """Wraps GET /rest/agile/1.0/board/{board_id}/sprint?state=<state>."""
        cache_key = f"sprints:{state}:{board_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get(
                f"/rest/agile/1.0/board/{board_id}/sprint",
                params={"state": state},
            )
            response.raise_for_status()
            sprints = response.json()["values"]

        self._cache.set(cache_key, sprints)
        return sprints

    async def get_closed_sprints(self, board_id: int) -> list[dict]:
        return await self._get_sprints(board_id, "closed")

    async def get_active_sprints(self, board_id: int) -> list[dict]:
        return await self._get_sprints(board_id, "active")

    async def search_users(self, query: str) -> list[dict]:
        """Wraps GET /rest/api/3/user/search?query=<query>. Unlike
        get_boards()/_get_sprints(), this endpoint's response is a bare JSON
        array, not wrapped in {"values": [...]}."""
        cache_key = f"user_search:{query}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get(
                "/rest/api/3/user/search", params={"query": query}
            )
            response.raise_for_status()
            users = response.json()

        self._cache.set(cache_key, users)
        return users

    async def get_myself(self) -> dict:
        """Wraps GET /rest/api/3/myself."""
        cached = self._cache.get("myself")
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get("/rest/api/3/myself")
            response.raise_for_status()
            myself = response.json()

        self._cache.set("myself", myself)
        return myself

    async def get_comments(self, issue_key: str) -> list[dict]:
        """Wraps GET /rest/api/3/issue/{issue_key}/comment. orderBy=created is
        passed explicitly since mention/reply-detection logic depends on
        strict chronological order."""
        cache_key = f"comments:{issue_key}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.get(
                f"/rest/api/3/issue/{issue_key}/comment",
                params={"orderBy": "created"},
            )
            response.raise_for_status()
            comments = response.json()["comments"]

        self._cache.set(cache_key, comments)
        return comments

    async def add_comment(self, issue_key: str, body: dict) -> dict:
        """Wraps POST /rest/api/3/issue/{issue_key}/comment. `body` is the
        full Atlassian Document Format comment-body dict (see jira_tools'
        _plain_text_to_adf). This is the client's only mutating call - unlike
        every read method above, its result is never cached."""
        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._auth
        ) as client:
            response = await client.post(
                f"/rest/api/3/issue/{issue_key}/comment", json={"body": body}
            )
            response.raise_for_status()
            return response.json()
