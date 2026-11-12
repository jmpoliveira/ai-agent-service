"""Test doubles for the application ports and for LangChain chat models."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult as LangChainChatResult
from pydantic import Field

from src.application.ports import ToolGateway
from src.domain.errors import ToolGatewayUnavailableError
from src.domain.llm import ChatResult, FinishReason
from src.domain.messages import Message, ToolCall
from src.domain.tools import ToolResult, ToolSpec
from src.domain.usage import Usage


def reply(
    text: str = "",
    *,
    tool_calls: Sequence[ToolCall] = (),
    usage: Usage | None = None,
    finish: FinishReason | None = None,
    provider: str = "fake",
    model: str = "fake-model",
) -> ChatResult:
    """Build a domain ``ChatResult`` the way an adapter would."""
    calls = tuple(tool_calls)
    return ChatResult(
        message=Message.assistant(content=text, tool_calls=calls),
        usage=usage if usage is not None else Usage(10, 5),
        finish_reason=finish or (FinishReason.TOOL_USE if calls else FinishReason.END_TURN),
        provider=provider,
        model=model,
    )


class ScriptedChatModel:
    """A ``ChatModel`` that plays back a script of results or exceptions."""

    def __init__(
        self,
        script: Sequence[ChatResult | BaseException],
        *,
        provider: str = "fake",
        model: str = "fake-model",
    ) -> None:
        self._script = list(script)
        self._provider = provider
        self._model = model
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        self.calls.append((list(messages), list(tools)))
        if not self._script:
            raise AssertionError("ScriptedChatModel ran out of scripted replies")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeToolGateway:
    """A tool server with fixed tools and canned results (or exceptions) per tool name."""

    def __init__(
        self,
        tools: Sequence[ToolSpec] = (),
        results: dict[str, ToolResult | Exception] | None = None,
    ) -> None:
        self._tools = list(tools)
        self._results = results or {}
        self.calls: list[ToolCall] = []

    async def list_tools(self) -> Sequence[ToolSpec]:
        return list(self._tools)

    async def call_tool(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        outcome = self._results.get(call.name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or ToolResult(call.id, call.name, "ok")


class FakeToolGatewayProvider:
    """Opens a ``FakeToolGateway``, or fails to connect when ``open_error`` is set."""

    def __init__(self, gateway: ToolGateway, open_error: Exception | None = None) -> None:
        self.gateway = gateway
        self._open_error = open_error
        self.opened = 0

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[ToolGateway]:
        if self._open_error is not None:
            raise self._open_error
        self.opened += 1
        yield self.gateway


def unreachable_provider() -> FakeToolGatewayProvider:
    return FakeToolGatewayProvider(
        FakeToolGateway(), open_error=ToolGatewayUnavailableError("unreachable")
    )


def tool_spec(name: str = "search_customers", description: str = "Search customers") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


class FakeLangChainModel(BaseChatModel):
    """A real LangChain ``BaseChatModel`` that replays AI messages and records what it saw."""

    replies: list[Any]
    received: list[list[BaseMessage]] = Field(default_factory=list[list[BaseMessage]])
    bound_tools: list[Any] = Field(default_factory=list[Any])

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.bound_tools.extend(tools)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> LangChainChatResult:
        self.received.append(list(messages))
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, AIMessage)
        return LangChainChatResult(generations=[ChatGeneration(message=item)])
