import logging

import httpx

from app.core.models import ToolResult

logger = logging.getLogger(__name__)


def sanitize_error(exc: Exception) -> str:
    """Convert an exception into a client-safe message. Deliberately never
    includes response bodies or headers, which may contain sensitive data."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} {exc.response.reason_phrase}"
    return f"{type(exc).__name__}: request failed"


# Shared by every caller that invokes a tool function directly rather than
# through the orchestrator (scripts/post_digest.py, app/slack/bolt_app.py's
# action handlers) - degrades a failed ToolResult to an empty list instead of
# raising, logging the failure since there's no ToolResult.error surfaced
# to an LLM/HTTP caller to report it for them.
def unwrap_tool_result[T](tool_name: str, result: ToolResult[list[T]]) -> list[T]:
    if not result.ok:
        logger.error("%s failed: %s", tool_name, result.error)
        return []
    return result.data or []
