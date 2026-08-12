import httpx

from app.config import Settings
from app.core.cache import TTLCache

GITHUB_API_BASE_URL = "https://api.github.com"


class GitHubClient:
    def __init__(self, settings: Settings, cache: TTLCache | None = None):
        self._token = settings.github_token
        self._cache = cache if cache is not None else TTLCache()

    async def search_prs(self, query: str) -> list[dict]:
        """Wraps GET /search/issues (Search API, PR-scoped). Returns raw item dicts."""
        cached = self._cache.get(query)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            headers={"Authorization": f"Bearer {self._token}"},
        ) as client:
            response = await client.get("/search/issues", params={"q": query})
            response.raise_for_status()
            items = response.json()["items"]

        self._cache.set(query, items)
        return items
