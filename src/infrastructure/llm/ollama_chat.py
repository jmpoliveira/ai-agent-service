"""Ollama (local models) as a ``ChatModel``."""

from __future__ import annotations

import httpx
from langchain_core.exceptions import ModelError
from langchain_ollama import ChatOllama
from ollama import ResponseError

from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

PROVIDER = "ollama"

_HANDLED_ERRORS: tuple[type[Exception], ...] = (
    ResponseError,
    httpx.HTTPError,
    ConnectionError,
    TimeoutError,
    ModelError,
)


def create_ollama_chat_model(
    *,
    base_url: str,
    model: str,
    timeout_seconds: float,
    max_output_tokens: int,
    temperature: float,
    reasoning: bool | None,
) -> LangChainChatModel:
    """Build an Ollama-backed chat model. Raises nothing: connection happens on first use."""
    client = ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        num_predict=max_output_tokens,
        reasoning=reasoning,
        client_kwargs={"timeout": timeout_seconds},
    )
    return LangChainChatModel(
        provider=PROVIDER,
        model=model,
        client=client,
        handled_errors=_HANDLED_ERRORS,
    )
