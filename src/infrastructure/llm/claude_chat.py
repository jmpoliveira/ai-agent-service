"""Anthropic Claude as a ``ChatModel``."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import ModelError
from pydantic import SecretStr

from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

PROVIDER = "anthropic"

_HANDLED_ERRORS: tuple[type[Exception], ...] = (
    anthropic.AnthropicError,
    httpx.HTTPError,
    ModelError,
)


def create_claude_chat_model(
    *,
    api_key: SecretStr,
    model: str,
    timeout_seconds: float,
    max_output_tokens: int,
    temperature: float,
) -> LangChainChatModel:
    """Build a Claude-backed chat model. Retries are handled by our own decorator."""
    # Passed as a dict: ChatAnthropic declares its fields through pydantic aliases
    # (model_name, stop...), which static type checkers cannot see in the signature.
    options: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    client = ChatAnthropic(**options)
    return LangChainChatModel(
        provider=PROVIDER,
        model=model,
        client=client,
        handled_errors=_HANDLED_ERRORS,
        preserve_provider_state=True,
    )
