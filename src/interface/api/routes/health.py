"""Health probes: liveness (process is up) and readiness (dependencies reachable)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from src.application.ports import HealthCheck
from src.interface.api.schemas import (
    DependencyStatusResponse,
    LivenessResponse,
    ReadinessResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    """Only confirms that the process responds; checks no dependency."""
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(request: Request) -> JSONResponse:
    """Checks every configured dependency concurrently."""
    checks: Sequence[HealthCheck] = request.app.state.health_checks
    results = await asyncio.gather(*(check.check() for check in checks))

    ready = all(result.healthy for result in results)
    body = ReadinessResponse(
        status="ready" if ready else "not_ready",
        dependencies=[
            DependencyStatusResponse(name=r.name, healthy=r.healthy, detail=r.detail)
            for r in results
        ],
    )
    if not ready:
        logger.warning(
            "Service not ready", extra={"unhealthy": [r.name for r in results if not r.healthy]}
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(),
    )
