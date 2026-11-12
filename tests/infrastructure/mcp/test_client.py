"""Unit tests for the MCP adapter, with a mocked ``ClientSession``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolResult,
    ContentBlock,
    EmbeddedResource,
    ErrorData,
    ImageContent,
    ListToolsResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
    Tool,
)
from opentelemetry import trace
from pydantic import AnyUrl

from src.domain.errors import ToolExecutionError, ToolGatewayUnavailableError
from src.domain.messages import ToolCall
from src.infrastructure.mcp import client as client_module
from src.infrastructure.mcp.client import McpToolGateway, McpToolGatewayProvider


def _tool(name: str) -> Tool:
    return Tool(name=name, description=f"{name} tool", inputSchema={"type": "object"})


def _gateway(session: Any) -> McpToolGateway:
    return McpToolGateway(session, tool_timeout_seconds=7)


def _session() -> MagicMock:
    session = MagicMock()
    session.list_tools = AsyncMock()
    session.call_tool = AsyncMock()
    return session


async def test_list_tools_follows_pagination_and_converts_specs():
    # Arrange
    session = _session()
    session.list_tools.side_effect = [
        ListToolsResult(tools=[_tool("a")], nextCursor="page2"),
        ListToolsResult(tools=[_tool("b")]),
    ]

    # Act
    specs = await _gateway(session).list_tools()

    # Assert
    assert [s.name for s in specs] == ["a", "b"]
    assert specs[0].description == "a tool"
    cursors = [call.kwargs["params"].cursor for call in session.list_tools.call_args_list]
    assert cursors == [None, "page2"]


async def test_list_tools_stops_after_the_page_limit():
    # Arrange: a server that always claims there is another page
    session = _session()
    session.list_tools.return_value = ListToolsResult(tools=[_tool("a")], nextCursor="again")

    # Act
    specs = await _gateway(session).list_tools()

    # Assert
    assert session.list_tools.await_count == 20
    assert len(specs) == 20


async def test_list_tools_carries_the_trace_context_in_meta():
    # Arrange
    session = _session()
    session.list_tools.return_value = ListToolsResult(tools=[])

    # Act
    with trace.get_tracer("test").start_as_current_span("root") as root:
        await _gateway(session).list_tools()

    # Assert: _meta.traceparent belongs to the same trace as the caller
    params = session.list_tools.call_args.kwargs["params"]
    traceparent = params.meta.model_extra["traceparent"]
    assert traceparent.split("-")[1] == f"{root.get_span_context().trace_id:032x}"


async def test_list_tools_failure_is_reported_as_unavailable():
    session = _session()
    session.list_tools.side_effect = McpError(ErrorData(code=-32000, message="nope"))

    with pytest.raises(ToolGatewayUnavailableError):
        await _gateway(session).list_tools()


async def test_call_tool_sends_arguments_timeout_and_trace_meta():
    # Arrange
    session = _session()
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="hi")])
    call = ToolCall(id="c1", name="search", arguments={"q": "ada"})

    # Act
    with trace.get_tracer("test").start_as_current_span("tool-span") as span:
        result = await _gateway(session).call_tool(call)

    # Assert
    args, kwargs = session.call_tool.call_args
    assert args == ("search", {"q": "ada"})
    assert kwargs["read_timeout_seconds"].total_seconds() == 7
    assert kwargs["meta"]["traceparent"].split("-")[2] == f"{span.get_span_context().span_id:016x}"
    assert (result.call_id, result.name, result.content, result.is_error) == (
        "c1",
        "search",
        "hi",
        False,
    )


async def test_tool_level_errors_from_the_server_are_flagged():
    session = _session()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="invalid id")], isError=True
    )

    result = await _gateway(session).call_tool(ToolCall("c1", "search"))

    assert result.is_error is True
    assert result.content == "invalid id"


async def test_protocol_errors_become_execution_errors():
    session = _session()
    session.call_tool.side_effect = McpError(ErrorData(code=-32602, message="bad params"))

    with pytest.raises(ToolExecutionError):
        await _gateway(session).call_tool(ToolCall("c1", "search"))


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), OSError("reset")])
async def test_transport_errors_become_gateway_unavailable(error: Exception):
    session = _session()
    session.call_tool.side_effect = error

    with pytest.raises(ToolGatewayUnavailableError):
        await _gateway(session).call_tool(ToolCall("c1", "search"))


async def test_non_text_content_becomes_explicit_placeholders():
    # Arrange
    session = _session()
    blocks: list[ContentBlock] = [
        TextContent(type="text", text="caption"),
        ImageContent(type="image", data="AAAA", mimeType="image/png"),
        ResourceLink(type="resource_link", name="doc", uri=AnyUrl("file:///doc.txt")),
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(uri=AnyUrl("file:///a.txt"), text="secret"),
        ),
    ]
    session.call_tool.return_value = CallToolResult(content=blocks)

    # Act
    result = await _gateway(session).call_tool(ToolCall("c1", "search"))

    # Assert: no raw binary and no embedded payload is forwarded to the model
    assert "caption" in result.content
    assert "[image content omitted]" in result.content
    assert "file:///doc.txt" in result.content
    assert "[embedded resource omitted]" in result.content
    assert "secret" not in result.content


async def test_structured_content_is_used_when_there_is_no_text():
    session = _session()
    session.call_tool.return_value = CallToolResult(content=[], structuredContent={"id": 7})

    result = await _gateway(session).call_tool(ToolCall("c1", "search"))

    assert result.content == '{"id": 7}'


async def test_opening_a_session_to_an_unreachable_server_is_reported_cleanly(
    monkeypatch: pytest.MonkeyPatch,
):
    # Arrange
    def refuse(*_: Any, **__: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(client_module, "streamable_http_client", refuse)
    provider = McpToolGatewayProvider(
        "http://mcp.test/mcp", connect_timeout_seconds=1, tool_timeout_seconds=1
    )

    # Act / Assert
    with pytest.raises(ToolGatewayUnavailableError):
        async with provider.open():
            pytest.fail("the body must not run when the connection fails")
