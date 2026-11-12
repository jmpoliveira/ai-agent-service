"""Tool-related domain types, independent of any protocol (MCP) or LLM provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Description of a tool the model may call.

    ``input_schema`` is a JSON Schema object, the common denominator between MCP
    tool definitions and the function-calling formats of the LLM providers.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolSpec.name must not be empty")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Outcome of one tool call, already reduced to text."""

    call_id: str
    name: str
    content: str
    is_error: bool = False
