"""Stubbed tools: evals must not change the data of the server they run against.

A stubbed tool is still listed by the real server, so the model sees its real name, description
and schema; only the call is intercepted and answered with canned text. Write tools are stubbed
so cases stay independent and repeatable, and a case can stub a read tool to inject hostile
content (prompt injection tests).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from src.application.ports import ToolGateway, ToolGatewayProvider
from src.domain.messages import ToolCall
from src.domain.tools import ToolResult, ToolSpec


class StubbingToolGateway:
    def __init__(self, inner: ToolGateway, stubs: Mapping[str, str]) -> None:
        self._inner = inner
        self._stubs = dict(stubs)

    async def list_tools(self) -> Sequence[ToolSpec]:
        return await self._inner.list_tools()

    async def call_tool(self, call: ToolCall) -> ToolResult:
        stub = self._stubs.get(call.name)
        if stub is None:
            return await self._inner.call_tool(call)
        return ToolResult(call_id=call.id, name=call.name, content=stub)


class StubbingToolGatewayProvider:
    """Wraps a real provider; with no stubs it behaves exactly like the wrapped one."""

    def __init__(self, inner: ToolGatewayProvider, stubs: Mapping[str, str]) -> None:
        self._inner = inner
        self._stubs = dict(stubs)

    def open(self) -> AbstractAsyncContextManager[ToolGateway]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[ToolGateway]:
        async with self._inner.open() as gateway:
            yield StubbingToolGateway(gateway, self._stubs)
