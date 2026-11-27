"""Microsoft Foundry (Azure OpenAI v1 API) as a ``ChatModel``.

Foundry exposes an OpenAI-compatible endpoint (``https://<resource>.openai.azure.com/openai/v1``)
that needs no ``api-version``, so the plain ``ChatOpenAI`` client talks to it directly. The
deployment name goes in ``model``. See docs/adr/0007-microsoft-foundry-provider.md.

Two ways to authenticate: an API key, or Microsoft Entra ID tokens (keyless). With Entra ID the
client receives a function instead of a key; it asks for a token before each request and the
provider returns a cached one until it is close to expiring.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import openai
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_core.exceptions import ModelAuthenticationError, ModelError
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

PROVIDER = "foundry"

# With reasoning enabled, GPT-6 models reject sampling parameters such as temperature.
_EFFORT_WITHOUT_REASONING = "none"

# The OpenAI SDK wraps its transport errors (it uses httpx2, not httpx) in OpenAIError, and
# langchain-openai re-raises most of them as LangChain ModelError subclasses.
_HANDLED_ERRORS: tuple[type[Exception], ...] = (openai.OpenAIError, ModelError)

TokenProvider = Callable[[], str]


def entra_id_token_provider(scope: str) -> TokenProvider:
    """Bearer tokens for ``scope`` from ``DefaultAzureCredential``.

    The credential tries, in order, environment variables (a service principal), workload and
    managed identity, then developer sign-ins such as ``az login``; the same code therefore runs
    on a laptop and on Azure compute. The synchronous credential is deliberate: langchain-openai
    runs a synchronous provider in a worker thread, so token refreshes do not block the event
    loop, and it avoids the extra async HTTP stack the ``aio`` credential needs.
    """
    return get_bearer_token_provider(DefaultAzureCredential(), scope)


def _as_model_errors(provider: TokenProvider) -> TokenProvider:
    """Report a failed sign-in as an authentication error, not as an unknown failure.

    The OpenAI client calls the provider itself and lets its exceptions through unchanged, so a
    missing ``az login`` would otherwise surface as an unclassified error (and be retried).
    """

    def provide() -> str:
        try:
            return provider()
        except ClientAuthenticationError as err:
            raise ModelAuthenticationError(
                "Could not get a Microsoft Entra ID token for Foundry"
            ) from err

    return provide


def create_foundry_chat_model(
    *,
    base_url: str,
    api_key: SecretStr | TokenProvider,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    max_output_tokens: int,
    temperature: float,
    http_async_client: Any = None,
) -> LangChainChatModel:
    """Build a Foundry-backed chat model. Retries are handled by our own decorator.

    ``api_key`` is either the resource key or a token provider (``entra_id_token_provider``).

    ``http_async_client`` exists for tests: an ``httpx2.AsyncClient`` with a mock transport,
    so the real request (URL, headers, body) can be checked without network access.
    """
    # Passed as a dict for the same reason as in claude_chat.py: ChatOpenAI declares several
    # fields through pydantic aliases (api_key, base_url, timeout), invisible to type checkers.
    options: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key if isinstance(api_key, SecretStr) else _as_model_errors(api_key),
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
