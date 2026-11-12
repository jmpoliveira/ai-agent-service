"""FastAPI application factory. It receives its collaborators; it never builds them."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import SecretStr

from src.application.ports import HealthCheck
from src.application.run_agent import RunAgent
from src.interface.api.errors import register_error_handlers
from src.interface.api.routes import agent, health

logger = logging.getLogger(__name__)


def create_api(
    *,
    title: str,
    run_agent: RunAgent,
    health_checks: Sequence[HealthCheck],
    api_key: SecretStr | None,
    on_shutdown: Callable[[], None],
    expose_docs: bool,
) -> FastAPI:
    """Assemble the HTTP interface around an already-built ``RunAgent``."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        logger.info("Service starting")
        yield
        logger.info("Service shutting down")
        on_shutdown()  # flush pending spans before the process exits

    app = FastAPI(
        title=title,
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.state.run_agent = run_agent
    app.state.health_checks = tuple(health_checks)
    app.state.api_key = api_key

    app.include_router(health.router, prefix="/health")
    app.include_router(agent.router)
    register_error_handlers(app)
    return app
