import httpx

from app.core.errors import sanitize_error, unwrap_tool_result
from app.core.models import ToolResult


def test_sanitize_http_status_error_returns_status_and_reason_only():
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(
        500, request=request, text="sensitive internal stack trace details"
    )
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    message = sanitize_error(exc)

    assert message == "500 Internal Server Error"
    assert "sensitive internal stack trace details" not in message


def test_sanitize_generic_exception_returns_generic_message():
    message = sanitize_error(RuntimeError("connection string: postgres://user:pw@host"))

    assert message == "RuntimeError: request failed"
    assert "postgres" not in message


def test_unwrap_tool_result_returns_data_on_success():
    result: ToolResult[list[str]] = ToolResult(ok=True, data=["AL-1"], error=None)

    assert unwrap_tool_result("some_tool", result) == ["AL-1"]


def test_unwrap_tool_result_degrades_to_empty_list_on_failure():
    result: ToolResult[list[str]] = ToolResult(ok=False, data=None, error="boom")

    assert unwrap_tool_result("some_tool", result) == []
