"""Ports: the interfaces the application needs. Infrastructure implements them."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from src.domain.llm import ChatResult
from src.domain.messages import Message, ToolCall
from src.domain.tools import ToolResult, ToolSpec


class ChatModel(Protocol):
    """One LLM, on one provider. Implementations hide every vendor difference."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        """Run one model turn.

        Raises:
            LLMError: (or a subclass) for every provider failure.
        """
        ...


class ToolGateway(Protocol):
    """An open connection to a tool server, valid for the duration of one run."""

    async def list_tools(self) -> Sequence[ToolSpec]: ...

    async def call_tool(self, call: ToolCall) -> ToolResult:
        """Call one tool.

        Raises:
            ToolExecutionError: the call failed but the connection is fine.
            ToolGatewayUnavailableError: the server cannot be reached.
        """
        ...


class ToolGatewayProvider(Protocol):
    """Opens a ``ToolGateway`` per run, which keeps the service stateless."""

    def open(self) -> AbstractAsyncContextManager[ToolGateway]: ...


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    name: str
    healthy: bool
    detail: str | None = None


class HealthCheck(Protocol):
    """A readiness probe for one dependency. Must never raise."""

    @property
    def name(self) -> str: ...

    async def check(self) -> DependencyStatus: ...
