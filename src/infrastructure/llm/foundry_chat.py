"""Microsoft Foundry (Azure OpenAI v1 API) as a ``ChatModel``.

Foundry exposes an OpenAI-compatible endpoint (``https://<resource>.openai.azure.com/openai/v1``)
that needs no ``api-version``, so the plain ``ChatOpenAI`` client talks to it directly. The
deployment name goes in ``model``. See docs/adr/0007-microsoft-foundry-provider.md.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.exceptions import ModelError
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

PROVIDER = "foundry"

# With reasoning enabled, GPT-6 models reject sampling parameters such as temperature.
_EFFORT_WITHOUT_REASONING = "none"

# The OpenAI SDK wraps its transport errors (it uses httpx2, not httpx) in OpenAIError, and
# langchain-openai re-raises most of them as LangChain ModelError subclasses.
_HANDLED_ERRORS: tuple[type[Exception], ...] = (openai.OpenAIError, ModelError)


def create_foundry_chat_model(
    *,
    base_url: str,
    api_key: SecretStr,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    max_output_tokens: int,
    temperature: float,
    http_async_client: Any = None,
) -> LangChainChatModel:
    """Build a Foundry-backed chat model. Retries are handled by our own decorator.

    ``http_async_client`` exists for tests: an ``httpx2.AsyncClient`` with a mock transport,
    so the real request (URL, headers, body) can be checked without network access.
    """
    # Passed as a dict for the same reason as in claude_chat.py: ChatOpenAI declares several
    # fields through pydantic aliases (api_key, base_url, timeout), invisible to type checkers.
    options: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        # Recent OpenAI models count this against max_completion_tokens; ChatOpenAI maps it.
        "max_tokens": max_output_tokens,
        "timeout": timeout_seconds,
        "max_retries": 0,
        "reasoning_effort": reasoning_effort,
    }
    if http_async_client is not None:
        options["http_async_client"] = http_async_client
    # Only a non-reasoning request accepts a temperature: sending it otherwise is a 400.
    if reasoning_effort == _EFFORT_WITHOUT_REASONING:
        options["temperature"] = temperature
    client = ChatOpenAI(**options)
    return LangChainChatModel(
        provider=PROVIDER,
        model=model,
        client=client,
        handled_errors=_HANDLED_ERRORS,
        # Chat Completions carries no opaque provider data (no reasoning items or signatures)
        # that has to be replayed; the portable fields rebuild every turn exactly.
        preserve_provider_state=False,
    )
