"""Agent run types: budget (the limits of a run) and the outcome of a run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.domain.messages import Message
from src.domain.usage import Usage


class StopReason(StrEnum):
    """Why an agent run ended."""

    FINISHED = "finished"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"  # noqa: S105 - a stop reason, not a credential
    TIMEOUT = "timeout"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    OUTPUT_TRUNCATED = "output_truncated"


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """Hard limits that guarantee an agent run always terminates."""

    max_steps: int
    max_total_tokens: int
    timeout_seconds: float
    max_identical_tool_calls: int

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_identical_tool_calls < 1:
            raise ValueError("max_identical_tool_calls must be at least 1")


@dataclass(frozen=True, slots=True)
class AgentRun:
    answer: str
    stop_reason: StopReason
    steps: int
    usage: Usage
    provider: str
    model: str
    messages: tuple[Message, ...]
    trace_id: str | None = None
