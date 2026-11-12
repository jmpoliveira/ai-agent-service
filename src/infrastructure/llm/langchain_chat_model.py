"""Adapter that implements the ``ChatModel`` port on top of a LangChain chat model.

LangChain's partner packages (``langchain-ollama``, ``langchain-google-genai``,
``langchain-anthropic``) already normalise ``bind_tools``, ``tool_calls`` and
``usage_metadata``. This class converts between them and the domain types, so nothing
outside ``infrastructure`` ever sees a LangChain object (see docs/adr/0002-llm-port.md).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages import ToolCall as LangChainToolCall

from src.domain.llm import ChatResult, FinishReason
from src.domain.messages import Message, ProviderState, Role, ToolCall
from src.domain.tools import ToolSpec
from src.domain.usage import Usage
from src.infrastructure.llm.errors import translate_llm_error

logger = logging.getLogger(__name__)

_MAX_TOKENS_MARKERS = frozenset({"max_tokens", "length", "max_output_tokens"})
_FINISH_REASON_KEYS = ("stop_reason", "finish_reason", "done_reason")


class LangChainChatModel:
    """A ``ChatModel`` backed by a LangChain ``BaseChatModel``."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        client: BaseChatModel,
        handled_errors: tuple[type[Exception], ...],
        preserve_provider_state: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._client = client
        self._handled_errors = handled_errors
        self._preserve_provider_state = preserve_provider_state

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResult:
        lc_messages = to_langchain_messages(messages, self._provider)
        try:
            if tools:
                runnable = self._client.bind_tools([_to_function_schema(tool) for tool in tools])
                reply = await runnable.ainvoke(lc_messages)
            else:
                reply = await self._client.ainvoke(lc_messages)
        except self._handled_errors as err:
            logger.warning(
                "LLM request failed",
                extra={"provider": self._provider, "error_type": type(err).__name__},
                exc_info=True,
            )
            raise translate_llm_error(err, self._provider) from err

        return from_ai_message(
            reply,
            provider=self._provider,
            model=self._model,
            preserve_provider_state=self._preserve_provider_state,
        )


def _to_function_schema(tool: ToolSpec) -> dict[str, Any]:
    """OpenAI-style function definition, which every LangChain ``bind_tools`` accepts."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def to_langchain_messages(messages: Sequence[Message], provider: str) -> list[BaseMessage]:
    converted: list[BaseMessage] = []
    for message in messages:
        match message.role:
            case Role.SYSTEM:
                converted.append(SystemMessage(content=message.content))
            case Role.USER:
                converted.append(HumanMessage(content=message.content))
            case Role.ASSISTANT:
                converted.append(_assistant_to_langchain(message, provider))
            case Role.TOOL:
                converted.append(
                    ToolMessage(
                        content=message.content,
                        tool_call_id=message.tool_call_id or "",
                        name=message.tool_name,
                        status="error" if message.is_error else "success",
                    )
                )
    return converted


def _assistant_to_langchain(message: Message, provider: str) -> AIMessage:
    tool_calls: list[LangChainToolCall] = [
        {"id": call.id, "name": call.name, "args": call.arguments, "type": "tool_call"}
        for call in message.tool_calls
    ]
    state = message.provider_state
    if state is not None and state.provider == provider:
        # Same provider: replay what it produced, byte for byte.
        return AIMessage(
            content=state.data.get("content", message.content),
            additional_kwargs=dict(state.data.get("additional_kwargs", {})),
            tool_calls=tool_calls,
        )
    # Another provider (after a fallback): it cannot read the first provider's opaque data and
    # may reject it, so the turn is rebuilt from the portable fields only.
    return AIMessage(content=message.content, tool_calls=tool_calls)


def from_ai_message(
    reply: AIMessage,
    *,
    provider: str,
    model: str,
    preserve_provider_state: bool,
) -> ChatResult:
    calls, ids_were_generated = _extract_tool_calls(reply)
    if reply.invalid_tool_calls:
        logger.warning(
            "Model produced malformed tool calls",
            extra={"provider": provider, "count": len(reply.invalid_tool_calls)},
        )

    state = None
    # If we had to invent tool-call ids, they no longer match the ids inside the provider's own
    # data; replaying that data would break the call/result pairing, so it is dropped.
    if preserve_provider_state and not ids_were_generated:
        state = _capture_provider_state(reply, provider)

    return ChatResult(
        message=Message.assistant(
            content=extract_text(reply.content),
            tool_calls=calls,
            provider_state=state,
        ),
        usage=_extract_usage(reply),
        finish_reason=_extract_finish_reason(reply, has_tool_calls=bool(calls)),
        provider=provider,
        model=model,
    )


def _extract_tool_calls(reply: AIMessage) -> tuple[tuple[ToolCall, ...], bool]:
    """Domain tool calls with guaranteed unique ids; also says if any id had to be invented."""
    calls: list[ToolCall] = []
    used_ids: set[str] = set()
    generated = False
    for raw in reply.tool_calls:
        call_id = raw.get("id") or ""
        if not call_id or call_id in used_ids:
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            generated = True
        used_ids.add(call_id)
        calls.append(ToolCall(id=call_id, name=raw["name"], arguments=dict(raw["args"])))
    return tuple(calls), generated


def _capture_provider_state(reply: AIMessage, provider: str) -> ProviderState | None:
    if isinstance(reply.content, str) and not reply.additional_kwargs:
        return None
    return ProviderState(
        provider=provider,
        data={"content": reply.content, "additional_kwargs": dict(reply.additional_kwargs)},
    )


def extract_text(content: str | list[str | dict[str, Any]]) -> str:
    """Plain text of a reply. Providers return a string or a list of typed content blocks."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _extract_usage(reply: AIMessage) -> Usage:
    metadata = reply.usage_metadata
    if metadata is None:
        logger.debug("Reply carries no usage metadata")
        return Usage()
    return Usage(
        input_tokens=int(metadata.get("input_tokens", 0)),
        output_tokens=int(metadata.get("output_tokens", 0)),
    )


def _extract_finish_reason(reply: AIMessage, *, has_tool_calls: bool) -> FinishReason:
    if has_tool_calls:
        return FinishReason.TOOL_USE
    for key in _FINISH_REASON_KEYS:
        value = reply.response_metadata.get(key)
        if isinstance(value, str) and value.lower() in _MAX_TOKENS_MARKERS:
            return FinishReason.MAX_TOKENS
    return FinishReason.END_TURN
