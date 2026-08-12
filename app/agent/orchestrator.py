import asyncio
import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.agent.registry import TOOL_REGISTRY
from app.agent.schemas import TOOL_SCHEMAS
from app.config import settings
from app.core.errors import sanitize_error
from app.core.models import AskResponse

MAX_ITERATIONS = 6
MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are an engineering-status assistant with access to read-only tools over \
Jira and GitHub. Call whichever tools are relevant to the user's question, \
including multiple tools in the same turn when the question spans both \
systems (for example, "what should I work on today" needs high-priority \
issues, issues without PRs, and PRs awaiting review).

Base your final answer only on the data returned by tools - never invent \
ticket keys, PR numbers, or counts. Produce a concise, prioritized summary, \
not a restatement of raw tool output. If a tool result reports an error, \
acknowledge the gap in your answer (e.g. "GitHub data is unavailable, \
showing Jira only") rather than silently omitting it.

For questions unrelated to Jira/GitHub work status, answer directly without \
calling any tool.
"""

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


class OrchestratorUnavailable(Exception):
    """Raised when the Anthropic API call itself fails (auth/network/rate limit)."""


def extract_text(content: list[Any]) -> str:
    return "".join(block.text for block in content if block.type == "text")


def _serialize_tool_data(data: Any) -> str:
    """Tool results are typically list[BaseModel] (e.g. list[Issue]); handle
    that shape explicitly since a list itself has no model_dump_json()."""
    if isinstance(data, list):
        return json.dumps([item.model_dump(mode="json") for item in data])
    if isinstance(data, BaseModel):
        return data.model_dump_json()
    return json.dumps(data)


async def dispatch(block: Any, called_tools: list[str], warnings: list[str]) -> dict:
    tool_fn = TOOL_REGISTRY[block.name]
    try:
        result = await tool_fn()
    except Exception as exc:
        message = sanitize_error(exc)
        warnings.append(f"{block.name}: {message}")
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"error: {message}",
            "is_error": True,
        }

    if not result.ok:
        warnings.append(f"{block.name}: {result.error}")
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"error: {result.error}",
            "is_error": True,
        }

    called_tools.append(block.name)
    content = _serialize_tool_data(result.data)
    return {"type": "tool_result", "tool_use_id": block.id, "content": content}


async def handle_query(query: str) -> AskResponse:
    messages: list[dict] = [{"role": "user", "content": query}]
    warnings: list[str] = []
    called_tools: list[str] = []

    for iteration in range(MAX_ITERATIONS):
        force_final = iteration == MAX_ITERATIONS - 1
        try:
            # Plain dicts (not the SDK's TypedDict param constructors) are used
            # for messages/tools/tool_choice - structurally correct at runtime,
            # but mypy's strict overload matching can't verify that.
            response = await anthropic_client.messages.create(  # type: ignore[call-overload]
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                tool_choice={"type": "none"} if force_final else {"type": "auto"},
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise OrchestratorUnavailable(str(exc)) from exc

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return AskResponse(
                answer=extract_text(response.content),
                tool_calls=called_tools,
                warnings=warnings,
            )

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(
            *[dispatch(block, called_tools, warnings) for block in tool_use_blocks]
        )
        messages.append({"role": "user", "content": results})

    # Unreachable in practice: the forced tool_choice="none" turn above
    # always returns stop_reason != "tool_use". Kept as a defensive guard.
    raise OrchestratorUnavailable("model did not produce a final answer")
