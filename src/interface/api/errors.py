"""Map domain errors to HTTP responses.

Messages for provider and tool-server failures are fixed strings: upstream details stay in
the logs and never reach the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.domain.errors import (
    DomainError,
    InvalidPromptError,
    LLMAuthError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
    ToolCallingNotSupportedError,
    ToolGatewayError,
    ToolGatewayUnavailableError,
    UnknownProviderError,
)
from src.interface.api.schemas import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Mapping:
    error_type: type[DomainError]
    status_code: int
    code: str
    message: str | None  # None: the domain message is safe to show to the caller


# Order matters: the first match wins, so subclasses come before their base classes.
_MAPPINGS: tuple[_Mapping, ...] = (
    _Mapping(InvalidPromptError, 422, "invalid_prompt", None),
    _Mapping(UnknownProviderError, 400, "unknown_provider", None),
    _Mapping(
        ToolCallingNotSupportedError,
        422,
        "tool_calling_not_supported",
        "The selected model cannot call tools.",
    ),
    _Mapping(
        LLMRateLimitError, 429, "rate_limited", "The model provider is rate limiting requests."
    ),
    _Mapping(LLMUnavailableError, 503, "llm_unavailable", "The model provider is unavailable."),
    # 502, not 401: the caller's credentials are fine; ours towards the provider are not.
    _Mapping(
        LLMAuthError, 502, "llm_misconfigured", "The model provider is not configured correctly."
    ),
    _Mapping(
        LLMInvalidRequestError,
        502,
        "llm_rejected_request",
        "The model provider rejected the request.",
    ),
    _Mapping(LLMResponseError, 502, "llm_bad_response", "The model returned an unusable response."),
    _Mapping(LLMError, 502, "llm_error", "The model provider failed."),
    _Mapping(
        ToolGatewayUnavailableError, 503, "tools_unavailable", "The tool server is unavailable."
    ),
    _Mapping(ToolGatewayError, 502, "tools_error", "The tool server failed."),
    _Mapping(DomainError, 500, "internal_error", "Internal error."),
)


def _resolve(err: DomainError) -> _Mapping:
    for mapping in _MAPPINGS:
        if isinstance(err, mapping.error_type):
            return mapping
    return _MAPPINGS[-1]


async def _handle_domain_error(_: Request, err: Exception) -> JSONResponse:
    if not isinstance(err, DomainError):  # pragma: no cover - guarded by registration
        raise err
    mapping = _resolve(err)
    message = mapping.message if mapping.message is not None else str(err)

    if mapping.status_code >= 500:
        logger.error("Request failed", extra={"error_code": mapping.code}, exc_info=err)
    else:
        logger.warning("Request rejected", extra={"error_code": mapping.code})

    headers: dict[str, str] = {}
    if isinstance(err, LLMRateLimitError) and err.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(1, round(err.retry_after_seconds)))

    body = ErrorResponse(error=ErrorDetail(code=mapping.code, message=message))
    return JSONResponse(status_code=mapping.status_code, content=body.model_dump(), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _handle_domain_error)
