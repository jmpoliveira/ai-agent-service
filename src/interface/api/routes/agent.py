"""Agent endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from src.application.run_agent import RunAgent
from src.interface.api.schemas import AgentRunRequest, AgentRunResponse, ErrorResponse
from src.interface.api.security import require_api_key

router = APIRouter(prefix="/v1/agent", tags=["agent"], dependencies=[Depends(require_api_key)])


def get_run_agent(request: Request) -> RunAgent:
    run_agent: RunAgent = request.app.state.run_agent
    return run_agent


@router.post(
    "/runs",
    response_model=AgentRunResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_run(
    body: AgentRunRequest,
    run_agent: Annotated[RunAgent, Depends(get_run_agent)],
) -> AgentRunResponse:
    """Run the agent once: it may call tools, then returns the final answer."""
    run = await run_agent.execute(body.prompt, body.provider)
    return AgentRunResponse.from_run(run)
