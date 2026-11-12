"""Google Gemini as a ``ChatModel``."""

from __future__ import annotations

import httpx
from google.genai.errors import APIError
from langchain_core.exceptions import ModelError
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
from pydantic import SecretStr

from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

PROVIDER = "gemini"

_HANDLED_ERRORS: tuple[type[Exception], ...] = (
    ChatGoogleGenerativeAIError,
    APIError,
    httpx.HTTPError,
    ModelError,
)


def create_gemini_chat_model(
    *,
    api_key: SecretStr,
    model: str,
    timeout_seconds: float,
    max_output_tokens: int,
    temperature: float,
) -> LangChainChatModel:
    """Build a Gemini-backed chat model. Retries are handled by our own decorator."""
    client = ChatGoogleGenerativeAI(
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return LangChainChatModel(
        provider=PROVIDER,
        model=model,
        client=client,
        handled_errors=_HANDLED_ERRORS,
        preserve_provider_state=True,
    )
