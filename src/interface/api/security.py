"""Inbound authentication: a shared API key in the ``X-API-Key`` header."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Reject the request unless it carries the configured key. No key configured = open."""
    expected = request.app.state.api_key
    if expected is None:
        return
    provided = (x_api_key or "").encode()
    if not secrets.compare_digest(provided, expected.get_secret_value().encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
