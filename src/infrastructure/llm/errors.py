"""Translate provider and LangChain exceptions into the domain's LLM error taxonomy.

Two families reach this module:

* LangChain's own ``ModelError`` hierarchy. The Claude and Gemini integrations raise
  classes that are both the vendor SDK's exception and a ``ModelError`` subclass.
* Native exceptions (``ollama.ResponseError``, ``httpx`` errors...), which carry an
  HTTP status code.

The domain message never echoes the upstream text: it can contain request details.
The full exception is logged by the adapter instead.
"""

from __future__ import annotations

import httpx
from langchain_core.exceptions import (
    ContextOverflowError,
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelRefusalError,
    ModelTimeoutError,
)

from src.domain.errors import (
    LLMAuthError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
    ToolCallingNotSupportedError,
)

_UNSUPPORTED_TOOLS_MARKER = "does not support tools"


def translate_llm_error(err: Exception, provider: str) -> LLMError:
    """Map any handled provider exception to a domain ``LLMError``."""
    # Checked first: Ollama reports it as a plain 400, which would otherwise look like a
    # malformed request instead of "pick another model".
    if _UNSUPPORTED_TOOLS_MARKER in str(err).lower():
        return ToolCallingNotSupportedError(
            f"The {provider} model does not support tool calling", provider=provider
        )
    if isinstance(err, ModelError):
        return _from_langchain_error(err, provider)
    if isinstance(err, httpx.HTTPError | ConnectionError | TimeoutError):
        return LLMUnavailableError(f"{provider} is unreachable or timed out", provider=provider)

    status = _status_code(err)
    if status is not None:
        return _from_status(status, provider, _retry_after(err))
    return LLMError(f"{provider} request failed", provider=provider)


def _from_langchain_error(err: ModelError, provider: str) -> LLMError:
    if isinstance(err, ModelAuthenticationError | ModelPermissionDeniedError):
        return LLMAuthError(f"{provider} rejected the credentials", provider=provider)
    if isinstance(err, ModelRateLimitError):
        return LLMRateLimitError(
            f"{provider} rate limit reached",
            provider=provider,
            retry_after_seconds=_retry_after(err),
        )
    if isinstance(err, ModelConnectionError | ModelTimeoutError | ModelAPIError):
        return LLMUnavailableError(f"{provider} is unavailable", provider=provider)
    if isinstance(err, ModelNotFoundError | ModelInvalidRequestError | ContextOverflowError):
        return LLMInvalidRequestError(f"{provider} rejected the request", provider=provider)
    if isinstance(err, ModelRefusalError):
        return LLMResponseError(f"{provider} refused to answer", provider=provider)
    return LLMError(f"{provider} request failed", provider=provider)


def _from_status(status: int, provider: str, retry_after: float | None) -> LLMError:
    if status in (401, 403):
        return LLMAuthError(f"{provider} rejected the credentials", provider=provider)
    if status == 429:
        return LLMRateLimitError(
            f"{provider} rate limit reached", provider=provider, retry_after_seconds=retry_after
        )
    if status in (408, 409) or status >= 500:
        return LLMUnavailableError(f"{provider} is unavailable", provider=provider)
    if 400 <= status < 500:
        return LLMInvalidRequestError(f"{provider} rejected the request", provider=provider)
    return LLMError(f"{provider} request failed", provider=provider)


def _status_code(err: Exception) -> int | None:
    """HTTP status carried by SDK exceptions (``status_code`` or ``code`` attribute)."""
    for attribute in ("status_code", "code"):
        value = getattr(err, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value if value > 0 else None
    return None


def _retry_after(err: Exception) -> float | None:
    """Seconds from a ``Retry-After`` header, when the exception carries a response."""
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None
