"""The Foundry adapter against a mock HTTP transport: the real request and reply, no network.

``httpx2`` is the HTTP library of the ``openai`` SDK; a mock transport lets these tests check the
exact URL, headers and body sent to the Azure OpenAI v1 endpoint.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from azure.core.exceptions import ClientAuthenticationError
from pydantic import SecretStr

from src.domain.errors import (
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from src.domain.llm import FinishReason
from src.domain.messages import Message, ToolCall
from src.domain.tools import ToolResult, ToolSpec
from src.infrastructure.llm import foundry_chat
from src.infrastructure.llm.foundry_chat import (
    TokenProvider,
    create_foundry_chat_model,
    entra_id_token_provider,
)
from src.infrastructure.llm.langchain_chat_model import LangChainChatModel

_BASE_URL = "https://res.openai.azure.com/openai/v1"
_TOOL = ToolSpec(
    name="search_customers",
    description="Find customers by name.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


class _Recorder:
    """Mock transport handler: records each request and answers with a fixed response."""

    def __init__(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        self._status = status
        self._body = body
        self._headers = headers or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self._status, json=self._body, headers=self._headers)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def _completion(message: dict[str, Any], finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
    }


def _error(code: str) -> dict[str, Any]:
    return {"error": {"message": "upstream detail", "type": "invalid_request_error", "code": code}}


def _model(
    handler: _Recorder,
    *,
    reasoning_effort: str = "none",
    api_key: SecretStr | TokenProvider | None = None,
) -> LangChainChatModel:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return create_foundry_chat_model(
        base_url=_BASE_URL,
        api_key=api_key or SecretStr("test-key"),
        model="luna-deployment",
        reasoning_effort=reasoning_effort,
        timeout_seconds=5,
        max_output_tokens=256,
        temperature=0.0,
        http_async_client=client,
    )


_MESSAGES = [Message.system("Be brief."), Message.user("Find Alice")]


async def test_request_goes_to_the_v1_chat_completions_endpoint_with_a_bearer_key():
    # Arrange
    handler = _Recorder(200, _completion({"role": "assistant", "content": "Hi"}))

    # Act
    await _model(handler).chat(_MESSAGES, [])

    # Assert
    request = handler.requests[0]
    assert str(request.url) == f"{_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert "api-version" not in str(request.url)  # the v1 API needs none


async def test_payload_uses_the_deployment_name_and_disables_reasoning():
    # Arrange
    handler = _Recorder(200, _completion({"role": "assistant", "content": "Hi"}))

    # Act
    await _model(handler).chat(_MESSAGES, [_TOOL])

    # Assert
    payload = handler.payload
    assert payload["model"] == "luna-deployment"
    assert payload["reasoning_effort"] == "none"
    assert payload["temperature"] == 0.0
    assert payload["max_completion_tokens"] == 256
    assert payload["tools"][0]["function"]["name"] == "search_customers"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


async def test_temperature_is_not_sent_when_reasoning_is_enabled():
    # Arrange: GPT-6 models reject sampling parameters while reasoning (HTTP 400).
    handler = _Recorder(200, _completion({"role": "assistant", "content": "Hi"}))

    # Act
    await _model(handler, reasoning_effort="low").chat(_MESSAGES, [])

    # Assert
    assert handler.payload["reasoning_effort"] == "low"
    assert "temperature" not in handler.payload


async def test_tool_calls_and_usage_are_converted_to_domain_types():
    # Arrange
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_customers", "arguments": '{"query": "Alice"}'},
    }
    reply = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
    handler = _Recorder(200, _completion(reply, finish_reason="tool_calls"))

    # Act
    result = await _model(handler).chat(_MESSAGES, [_TOOL])

    # Assert
    (call,) = result.message.tool_calls
    assert (call.id, call.name, call.arguments) == (
        "call_1",
        "search_customers",
        {"query": "Alice"},
    )
    assert result.finish_reason is FinishReason.TOOL_USE
    assert (result.usage.input_tokens, result.usage.output_tokens) == (42, 7)
    assert (result.provider, result.model) == ("foundry", "luna-deployment")
    assert result.message.provider_state is None  # nothing opaque to replay


async def test_a_tool_result_turn_is_sent_back_in_chat_completions_format():
    # Arrange
    handler = _Recorder(200, _completion({"role": "assistant", "content": "Alice is C-001"}))
    call = ToolCall(id="call_1", name="search_customers", arguments={"query": "Alice"})
    history = [
        *_MESSAGES,
        Message.assistant(tool_calls=(call,)),
        Message.from_tool_result(
            ToolResult(call_id="call_1", name="search_customers", content="[]")
        ),
    ]

    # Act
    await _model(handler).chat(history, [_TOOL])

    # Assert
    sent = handler.payload["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]) == {"query": "Alice"}
    assert sent[3] == {"role": "tool", "content": "[]", "tool_call_id": "call_1"}


async def test_rate_limit_keeps_the_retry_after_hint():
    # Arrange
    handler = _Recorder(429, _error("rate_limit_exceeded"), headers={"retry-after": "7"})

    # Act / Assert
    with pytest.raises(LLMRateLimitError) as caught:
        await _model(handler).chat(_MESSAGES, [])
    assert caught.value.retry_after_seconds == 7.0
    assert "upstream detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthError),
        (403, LLMAuthError),
        (404, LLMInvalidRequestError),  # unknown deployment name
        (400, LLMInvalidRequestError),
        (500, LLMUnavailableError),
    ],
)
async def test_http_errors_are_translated(status: int, expected: type[Exception]):
    handler = _Recorder(status, _error("some_code"))
    with pytest.raises(expected):
        await _model(handler).chat(_MESSAGES, [])


async def test_connection_failures_are_reported_as_unavailable():
    # Arrange
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    model = create_foundry_chat_model(
        base_url=_BASE_URL,
        api_key=SecretStr("test-key"),
        model="luna-deployment",
        reasoning_effort="none",
        timeout_seconds=5,
        max_output_tokens=256,
        temperature=0.0,
        http_async_client=client,
    )

    # Act / Assert
    with pytest.raises(LLMUnavailableError):
        await model.chat(_MESSAGES, [])


# --- Microsoft Entra ID (keyless) ---------------------------------------------------------


async def test_a_token_provider_is_sent_as_a_bearer_token_on_every_request():
    # Arrange: the provider is asked before each request (it caches and refreshes tokens).
    tokens = iter(["entra-token-1", "entra-token-2"])
    handler = _Recorder(200, _completion({"role": "assistant", "content": "Hi"}))
    model = _model(handler, api_key=lambda: next(tokens))

    # Act
    await model.chat(_MESSAGES, [])
    await model.chat(_MESSAGES, [])

    # Assert
    sent = [r.headers["authorization"] for r in handler.requests]
    assert sent == ["Bearer entra-token-1", "Bearer entra-token-2"]


async def test_a_failed_sign_in_is_an_auth_error_and_sends_nothing():
    # Arrange: what DefaultAzureCredential raises without az login or a managed identity.
    def no_credential() -> str:
        raise ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token")

    handler = _Recorder(200, _completion({"role": "assistant", "content": "Hi"}))

    # Act / Assert: an auth error is a configuration problem, so it is not retried.
    with pytest.raises(LLMAuthError):
        await _model(handler, api_key=no_credential).chat(_MESSAGES, [])
    assert handler.requests == []


def test_entra_id_provider_asks_default_azure_credential_for_the_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    # Arrange
    seen: dict[str, object] = {}

    class FakeCredential:
        pass

    def fake_provider(credential: object, *scopes: str) -> TokenProvider:
        seen["credential"] = credential
        seen["scopes"] = scopes
        return lambda: "token"

    monkeypatch.setattr(foundry_chat, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(foundry_chat, "get_bearer_token_provider", fake_provider)

    # Act
    provider = entra_id_token_provider("https://ai.azure.com/.default")

    # Assert
    assert isinstance(seen["credential"], FakeCredential)
    assert seen["scopes"] == ("https://ai.azure.com/.default",)
    assert provider() == "token"
