from __future__ import annotations

import pytest

from src.domain.messages import Message, ProviderState, Role, ToolCall
from src.domain.tools import ToolResult, ToolSpec
from src.domain.usage import Usage


def test_factories_set_the_expected_role():
    # Arrange / Act
    messages = [Message.system("s"), Message.user("u"), Message.assistant("a")]

    # Assert
    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER, Role.ASSISTANT]


def test_from_tool_result_copies_the_result_fields():
    # Arrange
    result = ToolResult(call_id="c1", name="search", content="data", is_error=True)

    # Act
    message = Message.from_tool_result(result)

    # Assert
    assert message.role is Role.TOOL
    assert (message.tool_call_id, message.tool_name, message.content) == ("c1", "search", "data")
    assert message.is_error is True


def test_only_assistant_messages_may_carry_tool_calls():
    # Arrange
    call = ToolCall(id="c1", name="search")

    # Act / Assert
    with pytest.raises(ValueError, match="assistant"):
        Message(role=Role.USER, tool_calls=(call,))


def test_tool_message_requires_a_call_id():
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=Role.TOOL, content="x")


def test_non_tool_message_must_not_have_a_call_id():
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=Role.USER, content="x", tool_call_id="c1")


@pytest.mark.parametrize(("call_id", "name"), [("", "tool"), ("c1", "")])
def test_tool_call_rejects_empty_identifiers(call_id: str, name: str):
    with pytest.raises(ValueError, match="must not be empty"):
        ToolCall(id=call_id, name=name)


def test_assistant_message_keeps_provider_state():
    # Arrange
    state = ProviderState(provider="anthropic", data={"content": []})

    # Act
    message = Message.assistant("hi", provider_state=state)

    # Assert
    assert message.provider_state is state


def test_tool_spec_rejects_blank_names():
    with pytest.raises(ValueError, match="name"):
        ToolSpec(name="  ", description="d")


def test_usage_adds_component_wise():
    # Arrange / Act
    total = Usage(1, 2) + Usage(10, 20)

    # Assert
    assert total == Usage(11, 22)
    assert total.total_tokens == 33


def test_usage_rejects_negative_counts():
    with pytest.raises(ValueError, match="negative"):
        Usage(input_tokens=-1)
