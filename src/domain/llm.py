"""Result of a single model call."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.domain.messages import Message
from src.domain.usage import Usage


class FinishReason(StrEnum):
    """Why the model stopped, normalised across providers."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass(frozen=True, slots=True)
class ChatResult:
    message: Message
    usage: Usage
    finish_reason: FinishReason
    provider: str
    model: str
