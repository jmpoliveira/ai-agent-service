from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic
import httpx
import pytest
from google.genai.errors import ServerError
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
from ollama import ResponseError

from src.domain.errors import (
    LLMAuthError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
    ToolCallingNotSupportedError,
)
from src.infrastructure.llm.errors import translate_llm_error

_REQUEST = httpx.Request("POST", "https://example.invalid/v1")


def _response(status: int, headers: dict[str, str] | None = None) -> Any:
    """An httpx response. Typed as Any because the anthropic SDK declares its own Response type."""
    return httpx.Response(status, headers=headers, request=_REQUEST)


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        (lambda: ModelAuthenticationError("x"), LLMAuthError),
        (lambda: ModelPermissionDeniedError("x"), LLMAuthError),
        (lambda: ModelRateLimitError("x"), LLMRateLimitError),
        (lambda: ModelConnectionError("x"), LLMUnavailableError),
        (lambda: ModelTimeoutError("x"), LLMUnavailableError),
        (lambda: ModelAPIError("x"), LLMUnavailableError),
        (lambda: ModelNotFoundError("x"), LLMInvalidRequestError),
        (lambda: ModelInvalidRequestError("x"), LLMInvalidRequestError),
        (lambda: ContextOverflowError("x"), LLMInvalidRequestError),
        (lambda: ModelRefusalError("x"), LLMResponseError),
        (lambda: ModelError("x"), LLMError),
    ],
)
def test_langchain_model_errors_are_mapped(make_error: Callable[[], Exception], expected: type):
    # Act
    translated = translate_llm_error(make_error(), "claude")

    # Assert
    assert type(translated) is expected
    assert translated.provider == "claude"


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        (lambda: ResponseError("model not found", 404), LLMInvalidRequestError),
        (lambda: ResponseError("overloaded", 503), LLMUnavailableError),
        (lambda: ResponseError("too many", 429), LLMRateLimitError),
        (lambda: ResponseError("forbidden", 403), LLMAuthError),
        (lambda: ResponseError("weird", -1), LLMError),
        (lambda: httpx.ConnectError("refused"), LLMUnavailableError),
        (lambda: httpx.ReadTimeout("slow"), LLMUnavailableError),
        (lambda: ConnectionError("reset"), LLMUnavailableError),
        (lambda: TimeoutError("slow"), LLMUnavailableError),
        (lambda: ServerError(503, {"error": {"message": "x"}}), LLMUnavailableError),
        (lambda: RuntimeError("anything else"), LLMError),
    ],
)
def test_native_exceptions_are_mapped_by_status_or_type(
    make_error: Callable[[], Exception], expected: type
):
    assert type(translate_llm_error(make_error(), "ollama")) is expected


def test_models_without_tool_support_get_a_specific_error():
    # Arrange: the message Ollama returns for a model without the tools capability
    err = ResponseError("registry.ollama.ai/library/llama3:latest does not support tools", 400)

    # Act
    translated = translate_llm_error(err, "ollama")

    # Assert
    assert isinstance(translated, ToolCallingNotSupportedError)


def test_anthropic_rate_limit_keeps_the_retry_after_hint():
    # Arrange
    err = anthropic.RateLimitError(
        "slow down", response=_response(429, {"retry-after": "7"}), body=None
    )

    # Act
    translated = translate_llm_error(err, "anthropic")

    # Assert
    assert isinstance(translated, LLMRateLimitError)
    assert translated.retry_after_seconds == 7.0


@pytest.mark.parametrize("header", ["soon", ""])
def test_unparseable_retry_after_is_ignored(header: str):
    err = anthropic.RateLimitError("x", response=_response(429, {"retry-after": header}), body=None)

    translated = translate_llm_error(err, "anthropic")

    assert isinstance(translated, LLMRateLimitError)
    assert translated.retry_after_seconds is None


def test_anthropic_status_errors_use_the_http_status():
    auth = anthropic.AuthenticationError("bad key", response=_response(401), body=None)
    server = anthropic.InternalServerError("boom", response=_response(500), body=None)

    assert isinstance(translate_llm_error(auth, "anthropic"), LLMAuthError)
    assert isinstance(translate_llm_error(server, "anthropic"), LLMUnavailableError)


def test_upstream_message_is_not_echoed_in_the_domain_error():
    # Arrange
    err = ResponseError("secret-token-123 leaked in upstream text", 500)

    # Act
    translated = translate_llm_error(err, "ollama")

    # Assert
    assert "secret-token-123" not in str(translated)
