"""Provider-neutral conversation types.

Every LLM adapter converts these types to and from its own wire format, so the agent
loop never sees a vendor-specific structure and a conversation can be replayed on
another provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.domain.tools import ToolResult


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ToolCall.id must not be empty")
        if not self.name:
            raise ValueError("ToolCall.name must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderState:
    """Opaque data a provider needs to see again on later turns.

    Some providers attach data to an assistant turn (for example reasoning blocks or
    signatures) that must be replayed verbatim for tool use to continue. The adapter
    that produced it is the only one allowed to read it; any other provider ignores it
    and rebuilds the turn from ``content`` and ``tool_calls``.
    """

    provider: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    provider_state: ProviderState | None = None

    def __post_init__(self) -> None:
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError("Only assistant messages may carry tool calls")
        if self.role is Role.TOOL and not self.tool_call_id:
            raise ValueError("Tool messages require a tool_call_id")
        if self.role is not Role.TOOL and self.tool_call_id is not None:
            raise ValueError("Only tool messages may carry a tool_call_id")

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str = "",
        tool_calls: tuple[ToolCall, ...] = (),
        provider_state: ProviderState | None = None,
    ) -> Message:
        return cls(
            role=Role.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            provider_state=provider_state,
        )

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> Message:
        return cls(
            role=Role.TOOL,
            content=result.content,
            tool_call_id=result.call_id,
            tool_name=result.name,
            is_error=result.is_error,
        )
