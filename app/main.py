import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agent.orchestrator import OrchestratorUnavailable, handle_query
from app.config import settings
from app.core.models import AskResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    handler = None
    if settings.slack_bot_token and settings.slack_app_token:
        # Deferred import: app.slack.bolt_app constructs a real slack_bolt.AsyncApp
        # at import time using settings.slack_bot_token, which raises if the token
        # is falsy - only import it once we've confirmed Slack is configured, so
        # the app boots identically to today when it isn't.
        from slack_bolt.adapter.socket_mode.async_handler import (
            AsyncSocketModeHandler,
        )

        from app.slack.bolt_app import slack_app

        handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
        await handler.connect_async()
    else:
        logger.info(
            "Slack integration disabled (SLACK_BOT_TOKEN/SLACK_APP_TOKEN not set)"
        )

    yield

    if handler is not None:
        await handler.disconnect_async()


app = FastAPI(lifespan=lifespan)


class AskRequest(BaseModel):
    query: str


@app.exception_handler(OrchestratorUnavailable)
async def orchestrator_unavailable_handler(
    _request: Request, exc: OrchestratorUnavailable
) -> JSONResponse:
    logger.error("orchestrator unavailable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "assistant temporarily unavailable"},
    )


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    return await handle_query(request.query)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
