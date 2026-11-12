"""Opt-in logging of conversation content (prompts, model replies, tool results) for debugging.

Off by default and refused in production by the settings: this content can hold personal
data and untrusted text. Each piece is logged once, when it enters the conversation, so a run
can be read top to bottom without repeating the whole history at every step.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from src.domain.llm import ChatResult
from src.domain.tools import ToolResult

# A child of the module logger, so it can be silenced on its own in logging.json.
logger = logging.getLogger(f"{__name__}.content")

_MIN_MAX_CHARS: Final = 1


class ConversationLogger:
    """Writes conversation content at DEBUG, each field clipped to ``max_chars``."""

    def __init__(self, max_chars: int) -> None:
        if max_chars < _MIN_MAX_CHARS:
            raise ValueError("max_chars must be at least 1")
        self._max_chars = max_chars

    def prompt(self, text: str) -> None:
        logger.debug("LLM prompt", extra={"content": self._clip(text)})

    def model_reply(self, step: int, result: ChatResult) -> None:
        calls = [
            {"name": call.name, "arguments": call.arguments} for call in result.message.tool_calls
        ]
        logger.debug(
            "LLM reply",
            extra={
                "step": step,
                "content": self._clip(result.message.content),
                "tool_calls": self._clip(json.dumps(calls, ensure_ascii=False, default=str)),
                "finish_reason": result.finish_reason.value,
            },
        )

    def tool_result(self, result: ToolResult) -> None:
        # The text exactly as the model will see it: sanitised, capped and framed.
        logger.debug(
            "Tool result",
            extra={
                "tool": result.name,
                "is_error": result.is_error,
                "content": self._clip(result.content),
            },
        )

    def _clip(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return f"{text[: self._max_chars]}... [{len(text) - self._max_chars} more chars]"
