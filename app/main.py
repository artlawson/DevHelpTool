import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agent.orchestrator import OrchestratorUnavailable, handle_query
from app.core.models import AskResponse

logger = logging.getLogger(__name__)

app = FastAPI()


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
