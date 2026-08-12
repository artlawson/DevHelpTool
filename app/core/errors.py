import httpx


def sanitize_error(exc: Exception) -> str:
    """Convert an exception into a client-safe message. Deliberately never
    includes response bodies or headers, which may contain sensitive data."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} {exc.response.reason_phrase}"
    return f"{type(exc).__name__}: request failed"
