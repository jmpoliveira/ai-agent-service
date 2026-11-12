"""MCP client adapter: implements ``ToolGatewayProvider`` over streamable HTTP.

Trace context travels in the request ``params._meta`` (W3C ``traceparent``) and NOT in HTTP
headers. The MCP client sends every request from a task created when the connection
opens, so header propagation would attach every call to the same parent span. ``_meta``
is set per request, so each call is parented to the span that is active when it is made.

``list_tools`` is called explicitly after ``initialize()``: the SDK would otherwise list
tools implicitly on the first ``call_tool`` without ``_meta``, creating an orphan trace.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from typing import Any, NoReturn, cast

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    PaginatedRequestParams,
    ResourceLink,
    TextContent,
)
from mcp.types import Tool as McpTool
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from src.application.ports import ToolGateway
from src.domain.errors import ToolExecutionError, ToolGatewayUnavailableError
from src.domain.messages import ToolCall
from src.domain.tools import ToolResult, ToolSpec

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ai_agent_service")

_MAX_LIST_PAGES = 20
_CONNECTION_ERRORS = (McpError, httpx.HTTPError, OSError)


def _trace_meta() -> dict[str, Any]:
    """Serialise the active span context into a ``_meta`` carrier (``traceparent``...)."""
    carrier: dict[str, Any] = {}
    propagate.inject(carrier)
    return carrier


class McpToolGatewayProvider:
    """Opens one MCP session per agent run. Nothing is kept between runs."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        tool_timeout_seconds: float,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout_seconds
        self._tool_timeout = tool_timeout_seconds

    def _http_timeout(self) -> httpx.Timeout:
        # The read timeout must outlast a tool call, otherwise httpx would cut the
        # response stream before the per-call limit (tool_timeout) has a chance to apply.
        return httpx.Timeout(
            connect=self._connect_timeout,
            read=self._tool_timeout + self._connect_timeout,
            write=self._connect_timeout,
            pool=self._connect_timeout,
        )

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[ToolGateway]:
        """Open a session; failures are classified by *where* they happen.

        The SDK closes its transport through an anyio task group, which wraps whatever
        propagates through it in an ``ExceptionGroup``. Three situations must stay apart:
        the connection could not be opened (-> ``ToolGatewayUnavailableError``), the caller's
        own code failed (-> re-raised with its original type, so a 503 stays a 503), or the
        session merely closed untidily after a successful run (-> logged, not raised).
        """
        connected = False
        body_failed = False
        try:
            async with AsyncExitStack() as stack:
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=self._http_timeout())
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                # From here on, a failure can no longer be a connection problem.
                connected = True
                try:
                    yield McpToolGateway(session, tool_timeout_seconds=self._tool_timeout)
                except BaseException:
                    # The caller's code failed, not the session: remember it, because on the
                    # way out the SDK wraps this error in a group together with any close error.
                    body_failed = True
                    raise
        except (*_CONNECTION_ERRORS, BaseExceptionGroup) as err:
            # Look inside the (possibly nested) group to find what really happened.
            leaves = _leaves(err)
            if not connected and all(isinstance(leaf, _CONNECTION_ERRORS) for leaf in leaves):
                logger.error("Could not open an MCP session", exc_info=True)
                raise ToolGatewayUnavailableError("The MCP server is unreachable") from err
            if not connected or body_failed:
                _reraise_unwrapped(err, leaves)
            # The run succeeded; failing the request over a messy close would lose the answer.
            logger.warning("The MCP session did not close cleanly", exc_info=True)


class McpToolGateway:
    """A live MCP session, valid until the surrounding ``open()`` block exits."""

    def __init__(self, session: ClientSession, *, tool_timeout_seconds: float) -> None:
        self._session = session
        self._tool_timeout = timedelta(seconds=tool_timeout_seconds)

    async def list_tools(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        cursor: str | None = None
        with _tracer.start_as_current_span("tool.list", kind=SpanKind.CLIENT):
            for _ in range(_MAX_LIST_PAGES):
                params = PaginatedRequestParams.model_validate(
                    {"_meta": _trace_meta(), "cursor": cursor}
                )
                try:
                    page = await self._session.list_tools(params=params)
                except _CONNECTION_ERRORS as err:
                    logger.error("Listing MCP tools failed", exc_info=True)
                    raise ToolGatewayUnavailableError("Could not list the MCP tools") from err
                specs.extend(_to_tool_spec(tool) for tool in page.tools)
                cursor = page.nextCursor
                if cursor is None:
                    return specs
        logger.warning(
            "MCP tool listing stopped after the page limit", extra={"pages": _MAX_LIST_PAGES}
        )
        return specs

    async def call_tool(self, call: ToolCall) -> ToolResult:
        # Called inside the caller's "execute_tool" span, so that span becomes the
        # parent of the server-side "tools/call" span.
        try:
            result = await self._session.call_tool(
                call.name,
                call.arguments,
                read_timeout_seconds=self._tool_timeout,
                meta=_trace_meta(),
            )
        except McpError as err:
            raise ToolExecutionError(f"Tool '{call.name}' failed") from err
        except (httpx.HTTPError, OSError) as err:
            raise ToolGatewayUnavailableError("The MCP server became unreachable") from err
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=_to_text(result),
            is_error=result.isError,
        )


def _leaves(error: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) exception groups into their individual exceptions."""
    if isinstance(error, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", error)
        return [leaf for inner in group.exceptions for leaf in _leaves(inner)]
    return [error]


def _reraise_unwrapped(error: BaseException, leaves: list[BaseException]) -> NoReturn:
    """Re-raise the single real exception hidden in a group, or the group itself."""
    if len(leaves) == 1 and leaves[0] is not error:
        # The original type matters: the API maps it to the right status code.
        # ``from None`` drops the group, which adds only noise to the traceback.
        raise leaves[0] from None
    raise error


def _to_tool_spec(tool: McpTool) -> ToolSpec:
    return ToolSpec(
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.inputSchema),
    )


def _to_text(result: CallToolResult) -> str:
    """Reduce an MCP result to text; non-text content becomes an explicit placeholder."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append("[image content omitted]")
        elif isinstance(block, ResourceLink):
            parts.append(f"[resource link: {block.uri}]")
        elif isinstance(block, EmbeddedResource):
            parts.append("[embedded resource omitted]")
        else:
            parts.append("[unsupported content omitted]")
    text = "\n".join(part for part in parts if part)
    if not text and result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False, default=str)
    return text
