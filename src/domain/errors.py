"""Domain errors. The interface layer maps them to HTTP responses."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidPromptError(DomainError):
    """The prompt is empty, too long or otherwise invalid."""


class UnknownProviderError(DomainError):
    """The requested LLM provider is not configured."""


class LLMError(DomainError):
    """An LLM provider failed. Subclasses say whether retrying can help."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class LLMUnavailableError(LLMError):
    """Network failure, timeout or provider-side 5xx. Retryable."""


class LLMRateLimitError(LLMError):
    """The provider throttled the request. Retryable after a delay."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_seconds = retry_after_seconds


class LLMAuthError(LLMError):
    """Missing, invalid or unauthorised credentials. A configuration problem."""


class LLMInvalidRequestError(LLMError):
    """The provider rejected the request (bad parameters, unknown model...)."""


class ToolCallingNotSupportedError(LLMInvalidRequestError):
    """The configured model cannot do tool calling."""


class LLMResponseError(LLMError):
    """The provider answered, but the answer is unusable (for example empty)."""


class ToolGatewayError(DomainError):
    """Base class for failures reaching or using the tool server."""


class ToolExecutionError(ToolGatewayError):
    """One tool call failed. Recoverable: the model is told and may adapt."""


class ToolGatewayUnavailableError(ToolGatewayError):
    """The tool server cannot be reached. Not recoverable within the run."""
