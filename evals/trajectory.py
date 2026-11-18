"""What happened in one agent run, reduced to what the graders need."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.domain.agent import AgentRun
from src.domain.messages import Role


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """A tool call the model asked for, and what came back.

    ``result`` is the text the model received (sanitised and framed). It is ``None`` in reports
    written before results were recorded, so checks that need it are skipped for those.
    """

    name: str
    arguments: dict[str, Any]
    is_error: bool
    step: int
    result: str | None = None


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
    # The user's prompt as the model saw it; None in reports written before it was recorded.
    prompt: str | None = None

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.calls)

    @classmethod
    def from_run(cls, run: AgentRun) -> Trajectory:
        """Read the trajectory from the run's conversation.

        Graded on what the model *asked for*, not only on what reached the server: a call to
        a tool that was never offered is blocked by the agent, but it is still a wrong decision.
        """
        results = {
            message.tool_call_id: message for message in run.messages if message.role is Role.TOOL
        }
        prompt = next((m.content for m in run.messages if m.role is Role.USER), None)
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
                    is_error=result.is_error if (result := results.get(call.id)) else False,
                    step=step,
                    result=result.content if result else None,
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
            prompt=prompt,
        )


def trajectory_from_report(data: Mapping[str, Any], provider: str, model: str) -> Trajectory:
    """Rebuild a trajectory saved by ``report.write_report`` (the inverse of its ``runs`` entry).

    Raises KeyError or TypeError when the data is not a saved trajectory.
    """
    calls = tuple(
        RecordedCall(
            name=str(call["name"]),
            arguments=dict(call["arguments"]),
            is_error=bool(call["is_error"]),
            step=int(call["step"]),
            result=call.get("result"),
        )
        for call in data["calls"]
    )
    return Trajectory(
        calls=calls,
        answer=str(data["answer"]),
        stop_reason=str(data["stop_reason"]),
        steps=int(data["steps"]),
        input_tokens=int(data["input_tokens"]),
        output_tokens=int(data["output_tokens"]),
        provider=provider,
        model=model,
        trace_id=data.get("trace_id"),
        prompt=data.get("prompt"),
    )
