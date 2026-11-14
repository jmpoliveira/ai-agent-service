"""What happened in one agent run, reduced to what the graders need."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.domain.agent import AgentRun
from src.domain.messages import Role


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """A tool call the model asked for, and whether its result was an error."""

    name: str
    arguments: dict[str, Any]
    is_error: bool
    step: int


@dataclass(frozen=True, slots=True)
class Trajectory:
    calls: tuple[RecordedCall, ...]
    answer: str
    stop_reason: str
    steps: int
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    trace_id: str | None

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.calls)

    @classmethod
    def from_run(cls, run: AgentRun) -> Trajectory:
        """Read the trajectory from the run's conversation.

        Graded on what the model *asked for*, not only on what reached the server: a call to
        a tool that was never offered is blocked by the agent, but it is still a wrong decision.
        """
        errors = {
            message.tool_call_id: message.is_error
            for message in run.messages
            if message.role is Role.TOOL
        }
        calls: list[RecordedCall] = []
        step = 0
        for message in run.messages:
            if message.role is not Role.ASSISTANT:
                continue
            step += 1
            calls.extend(
                RecordedCall(
                    name=call.name,
                    arguments=dict(call.arguments),
                    is_error=errors.get(call.id, False),
                    step=step,
                )
                for call in message.tool_calls
            )
        return cls(
            calls=tuple(calls),
            answer=run.answer,
            stop_reason=run.stop_reason.value,
            steps=run.steps,
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            provider=run.provider,
            model=run.model,
            trace_id=run.trace_id,
        )
