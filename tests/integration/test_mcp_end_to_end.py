"""End-to-end test against a real MCP server running in-process on a local port.

No external service is involved: the server is a tiny FastMCP app started by the test. It
proves what mocks cannot: that the real SDK client connects, lists tools, calls them, and
that the W3C ``traceparent`` really arrives in the server's request ``_meta``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.application.catalog import ModelCatalog
from src.application.run_agent import AgentConfig, RunAgent
from src.domain.agent import AgentBudget, StopReason
from src.domain.errors import LLMUnavailableError, ToolGatewayUnavailableError
from src.domain.llm import ChatResult
from src.domain.messages import ToolCall
from src.infrastructure.mcp.client import McpToolGatewayProvider
from tests.conftest import span_by_name
from tests.fakes import ScriptedChatModel, reply

pytestmark = pytest.mark.integration


def _build_server() -> FastMCP:
    server = FastMCP("test-tools", stateless_http=True)

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @server.tool()
    def trace_info(ctx: Context[Any, Any, Any]) -> str:
        """Report the trace context this request arrived with."""
        meta = ctx.request_context.meta
        extras = (meta.model_extra or {}) if meta else {}
        return json.dumps({"traceparent": extras.get("traceparent")})

    @server.tool()
    def explode() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    return server


@pytest.fixture
async def mcp_url() -> AsyncGenerator[str]:
    config = uvicorn.Config(
        _build_server().streamable_http_app(), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an awaitable
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


def _provider(url: str) -> McpToolGatewayProvider:
    return McpToolGatewayProvider(url, connect_timeout_seconds=5, tool_timeout_seconds=10)


def _agent(url: str, script: list[ChatResult | BaseException]) -> RunAgent:
    model = ScriptedChatModel(script)
    config = AgentConfig("e2e", "SYSTEM", 200, 2000, AgentBudget(5, 100_000, 20.0, 2))
    return RunAgent(ModelCatalog({"fake": model}, default="fake"), _provider(url), config)


async def test_tools_are_listed_from_a_real_server(mcp_url: str):
    # Act
    async with _provider(mcp_url).open() as gateway:
        specs = await gateway.list_tools()

    # Assert
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"add", "trace_info", "explode"}
    assert by_name["add"].description == "Add two integers."
    assert by_name["add"].input_schema["properties"]["a"]["type"] == "integer"


async def test_a_tool_can_be_called_through_the_real_sdk(mcp_url: str):
    # Act
    async with _provider(mcp_url).open() as gateway:
        result = await gateway.call_tool(ToolCall("c1", "add", {"a": 2, "b": 3}))

    # Assert
    assert result.is_error is False
    assert result.content == "5"


async def test_a_failing_tool_is_reported_as_a_tool_error_not_a_crash(mcp_url: str):
    async with _provider(mcp_url).open() as gateway:
        result = await gateway.call_tool(ToolCall("c1", "explode", {}))

    assert result.is_error is True
    assert "kaboom" in result.content


async def test_the_trace_context_reaches_the_server_in_request_meta(
    mcp_url: str, spans: InMemorySpanExporter
):
    # Arrange: the model asks for trace_info, then answers
    call = ToolCall("c1", "trace_info", {})
    agent = _agent(mcp_url, [reply(tool_calls=[call]), reply("done")])

    # Act
    run = await agent.execute("where am I?")

    # Assert
    assert run.stop_reason is StopReason.FINISHED
    tool_message = run.messages[3]
    seen = json.loads(tool_message.content.split("\n")[1])["traceparent"]
    _, trace_id, parent_span_id, _ = seen.split("-")

    tool_span = span_by_name(spans, "execute_tool trace_info")
    assert trace_id == run.trace_id
    assert parent_span_id == f"{tool_span.context.span_id:016x}"  # type: ignore[union-attr]
    assert span_by_name(spans, "tool.list").parent is not None


async def test_listing_tools_happens_once_inside_the_run_trace(
    mcp_url: str, spans: InMemorySpanExporter
):
    # Arrange
    agent = _agent(mcp_url, [reply("hello")])

    # Act
    run = await agent.execute("hi")

    # Assert: the explicit tools/list is a child of the run, not an orphan trace
    listing = span_by_name(spans, "tool.list")
    root = span_by_name(spans, "invoke_agent e2e")
    assert listing.parent is not None
    assert listing.parent.span_id == root.context.span_id  # type: ignore[union-attr]
    assert f"{listing.context.trace_id:032x}" == run.trace_id  # type: ignore[union-attr]


async def test_errors_raised_inside_a_run_keep_their_type_while_the_connection_is_open(
    mcp_url: str,
):
    # Arrange: the SDK closes its connection through an anyio task group, which may wrap
    # whatever propagates through it in an ExceptionGroup. The API maps *domain* errors
    # to HTTP statuses, so the original type must survive.
    agent = _agent(mcp_url, [LLMUnavailableError("model down", provider="fake")])

    # Act / Assert
    with pytest.raises(LLMUnavailableError):
        await agent.execute("hello")


async def test_the_time_budget_still_works_through_a_real_connection(mcp_url: str):
    # Arrange: cancellation must cross the SDK's task group and still be recognised
    class SlowModel(ScriptedChatModel):
        async def chat(self, messages, tools):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return reply("too late")

    config = AgentConfig("e2e", "SYSTEM", 200, 2000, AgentBudget(5, 100_000, 0.3, 2))
    agent = RunAgent(
        ModelCatalog({"fake": SlowModel([])}, default="fake"), _provider(mcp_url), config
    )

    # Act
    run = await agent.execute("hello")

    # Assert
    assert run.stop_reason is StopReason.TIMEOUT


async def test_an_unreachable_server_is_reported_cleanly():
    # Arrange: grab a free port, then close it so that nothing listens there
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider = _provider(f"http://127.0.0.1:{port}/mcp")

    # Act / Assert
    with pytest.raises(ToolGatewayUnavailableError):
        async with provider.open():
            pytest.fail("the body must not run when the connection fails")
