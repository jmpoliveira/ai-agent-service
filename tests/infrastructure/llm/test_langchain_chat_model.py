from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.domain.errors import LLMRateLimitError, LLMUnavailableError
from src.domain.llm import FinishReason
from src.domain.messages import Message, ProviderState, ToolCall
from src.domain.tools import ToolResult
from src.infrastructure.llm.langchain_chat_model import (
    LangChainChatModel,
    extract_text,
    from_ai_message,
    to_langchain_messages,
)
from tests.fakes import FakeLangChainModel, tool_spec


def _model(
    replies: list[Any], *, preserve: bool = False, provider: str = "claude"
) -> tuple[LangChainChatModel, FakeLangChainModel]:
    client = FakeLangChainModel(replies=replies)
    adapter = LangChainChatModel(
        provider=provider,
        model="m1",
        client=client,
        handled_errors=(httpx.HTTPError,),
        preserve_provider_state=preserve,
    )
    return adapter, client


def _ai(content: Any = "", **kwargs: Any) -> AIMessage:
    return AIMessage(content=content, **kwargs)


# ----------------------------------------------------------------------- conversion in


def test_domain_messages_become_langchain_messages():
    # Arrange
    call = ToolCall(id="c1", name="search", arguments={"q": "x"})
    messages = [
        Message.system("sys"),
        Message.user("hi"),
        Message.assistant("thinking", tool_calls=(call,)),
        Message.from_tool_result(ToolResult("c1", "search", "boom", is_error=True)),
    ]

    # Act
    converted = to_langchain_messages(messages, "claude")

    # Assert
    assert isinstance(converted[0], SystemMessage)
    assert isinstance(converted[1], HumanMessage)
    assistant = converted[2]
    assert isinstance(assistant, AIMessage)
    assert assistant.tool_calls[0]["id"] == "c1"
    assert assistant.tool_calls[0]["args"] == {"q": "x"}
    tool = converted[3]
    assert isinstance(tool, ToolMessage)
    assert (tool.tool_call_id, tool.name, tool.status) == ("c1", "search", "error")


def test_provider_state_is_replayed_only_to_the_provider_that_produced_it():
    # Arrange
    raw_blocks = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "hi"}]
    state = ProviderState("claude", {"content": raw_blocks, "additional_kwargs": {"k": "v"}})
    call = ToolCall(id="c1", name="search")
    message = Message.assistant("hi", tool_calls=(call,), provider_state=state)

    # Act
    same = to_langchain_messages([message], "claude")[0]
    other = to_langchain_messages([message], "gemini")[0]

    # Assert
    assert same.content == raw_blocks
    assert same.additional_kwargs == {"k": "v"}
    assert other.content == "hi"
    assert other.additional_kwargs == {}


# ---------------------------------------------------------------------- conversion out


def test_text_is_extracted_from_strings_and_typed_blocks():
    assert extract_text("plain") == "plain"
    blocks: list[str | dict[str, Any]] = [
        {"type": "thinking", "thinking": "private"},
        {"type": "text", "text": "Hello "},
        "world",
        {"type": "tool_use", "id": "x"},
    ]
    assert extract_text(blocks) == "Hello world"


def test_reply_is_converted_with_tool_calls_usage_and_finish_reason():
    # Arrange
    ai = _ai(
        "calling",
        tool_calls=[{"id": "c1", "name": "search", "args": {"q": "x"}, "type": "tool_call"}],
        usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    )

    # Act
    result = from_ai_message(ai, provider="claude", model="m1", preserve_provider_state=False)

    # Assert
    assert result.message.tool_calls == (ToolCall("c1", "search", {"q": "x"}),)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 3)
    assert result.finish_reason is FinishReason.TOOL_USE
    assert (result.provider, result.model) == ("claude", "m1")


def test_missing_and_duplicate_tool_call_ids_are_replaced():
    # Arrange: Ollama-style calls without ids, plus a duplicated id
    ai = _ai(
        tool_calls=[
            {"id": None, "name": "a", "args": {}, "type": "tool_call"},
            {"id": "dup", "name": "b", "args": {}, "type": "tool_call"},
            {"id": "dup", "name": "c", "args": {}, "type": "tool_call"},
        ]
    )

    # Act
    result = from_ai_message(ai, provider="ollama", model="m1", preserve_provider_state=True)

    # Assert
    ids = [call.id for call in result.message.tool_calls]
    assert len(set(ids)) == 3
    assert all(ids)
    assert result.message.provider_state is None  # replaying would now mismatch the ids


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"stop_reason": "max_tokens"}, FinishReason.MAX_TOKENS),
        ({"finish_reason": "MAX_TOKENS"}, FinishReason.MAX_TOKENS),
        ({"done_reason": "length"}, FinishReason.MAX_TOKENS),
        ({"stop_reason": "end_turn"}, FinishReason.END_TURN),
        ({}, FinishReason.END_TURN),
    ],
)
def test_finish_reason_is_normalised(metadata: dict[str, str], expected: FinishReason):
    ai = _ai("text", response_metadata=metadata)

    result = from_ai_message(ai, provider="p", model="m", preserve_provider_state=False)

    assert result.finish_reason is expected


def test_missing_usage_metadata_counts_as_zero():
    result = from_ai_message(_ai("x"), provider="p", model="m", preserve_provider_state=False)
    assert result.usage.total_tokens == 0


def test_provider_state_is_captured_only_when_it_carries_something():
    plain = from_ai_message(_ai("x"), provider="claude", model="m", preserve_provider_state=True)
    rich = from_ai_message(
        _ai([{"type": "text", "text": "x"}]),
        provider="claude",
        model="m",
        preserve_provider_state=True,
    )
    not_wanted = from_ai_message(
        _ai([{"type": "text", "text": "x"}]),
        provider="ollama",
        model="m",
        preserve_provider_state=False,
    )

    assert plain.message.provider_state is None
    assert rich.message.provider_state is not None
    assert not_wanted.message.provider_state is None


# ------------------------------------------------------------------------------ chat()


async def test_chat_binds_tools_in_function_format_and_returns_the_reply():
    # Arrange
    adapter, client = _model([_ai("answer")])

    # Act
    result = await adapter.chat([Message.user("hi")], [tool_spec()])

    # Assert
    assert result.message.content == "answer"
    bound = client.bound_tools[0]
    assert bound["type"] == "function"
    assert bound["function"]["name"] == "search_customers"
    assert bound["function"]["parameters"]["type"] == "object"


async def test_chat_without_tools_does_not_bind_anything():
    # Arrange
    adapter, client = _model([_ai("answer")])

    # Act
    await adapter.chat([Message.user("hi")], [])

    # Assert
    assert client.bound_tools == []


async def test_handled_provider_errors_become_domain_errors():
    # Arrange
    adapter, _ = _model([httpx.ConnectError("refused")])

    # Act / Assert
    with pytest.raises(LLMUnavailableError) as raised:
        await adapter.chat([Message.user("hi")], [])
    assert isinstance(raised.value.__cause__, httpx.ConnectError)
    assert raised.value.provider == "claude"


async def test_langchain_rate_limit_errors_become_domain_rate_limit_errors():
    from langchain_core.exceptions import ModelRateLimitError

    client = FakeLangChainModel(replies=[ModelRateLimitError("slow")])
    adapter = LangChainChatModel(
        provider="gemini", model="m", client=client, handled_errors=(ModelRateLimitError,)
    )

    with pytest.raises(LLMRateLimitError):
        await adapter.chat([Message.user("hi")], [])


async def test_unexpected_exceptions_are_not_swallowed():
    # Arrange: a programming error must surface, not be disguised as an LLM failure
    adapter, _ = _model([ValueError("bug")])

    # Act / Assert
    with pytest.raises(ValueError, match="bug"):
        await adapter.chat([Message.user("hi")], [])


async def test_adapter_exposes_provider_and_model():
    adapter, _ = _model([])
    assert (adapter.provider, adapter.model) == ("claude", "m1")
