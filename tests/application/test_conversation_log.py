from __future__ import annotations

import json
import logging

import pytest

from src.application.conversation_log import ConversationLogger
from src.domain.llm import FinishReason
from src.domain.messages import ToolCall
from src.domain.tools import ToolResult
from tests.fakes import reply

_LOGGER = "src.application.conversation_log.content"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def test_prompt_is_logged_at_debug(caplog: pytest.LogCaptureFixture):
    # Arrange
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    # Act
    ConversationLogger(max_chars=100).prompt("Which tickets does Ada have?")

    # Assert
    [record] = _records(caplog)
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == "LLM prompt"
    assert record.__dict__["content"] == "Which tickets does Ada have?"


def test_model_reply_includes_text_tool_calls_and_finish_reason(caplog: pytest.LogCaptureFixture):
    # Arrange
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    call = ToolCall(id="c1", name="search_customers", arguments={"query": "Ada Lovelace"})
    result = reply("Let me look.", tool_calls=[call], finish=FinishReason.TOOL_USE)

    # Act
    ConversationLogger(max_chars=500).model_reply(1, result)

    # Assert
    [record] = _records(caplog)
    assert record.__dict__["step"] == 1
    assert record.__dict__["content"] == "Let me look."
    assert record.__dict__["finish_reason"] == "tool_use"
    assert json.loads(record.__dict__["tool_calls"]) == [
        {"name": "search_customers", "arguments": {"query": "Ada Lovelace"}}
    ]


def test_tool_result_is_logged_as_the_model_will_see_it(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    framed = '<tool_result name="get_ticket_info">T-1 open</tool_result>'

    ConversationLogger(max_chars=500).tool_result(
        ToolResult("c1", "get_ticket_info", framed, is_error=True)
    )

    [record] = _records(caplog)
    assert record.__dict__["tool"] == "get_ticket_info"
    assert record.__dict__["is_error"] is True
    assert record.__dict__["content"] == framed


def test_long_content_is_clipped_with_the_number_of_hidden_chars(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    ConversationLogger(max_chars=5).prompt("abcdefghij")

    assert _records(caplog)[0].__dict__["content"] == "abcde... [5 more chars]"


def test_nothing_is_written_when_debug_is_off(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=_LOGGER)

    ConversationLogger(max_chars=100).prompt("secret plans")

    assert _records(caplog) == []


def test_max_chars_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        ConversationLogger(max_chars=0)
