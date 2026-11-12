"""HTTP request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.domain.agent import AgentRun, StopReason


class AgentRunRequest(BaseModel):
    prompt: str = Field(
        min_length=1,
        description="What the agent should do. Validated against the configured maximum length.",
        examples=["Which open tickets does the customer Bruno Costa have?"],
    )
    provider: str | None = Field(
        default=None,
        description="LLM provider alias (for example 'ollama'). Defaults to the configured one.",
        examples=["ollama"],
    )


class UsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int


class AgentRunResponse(BaseModel):
    answer: str
    stop_reason: StopReason
    steps: int
    provider: str
    model: str
    usage: UsageResponse
    trace_id: str | None = Field(
        default=None, description="OpenTelemetry trace id; search for it in Jaeger."
    )

    @classmethod
    def from_run(cls, run: AgentRun) -> AgentRunResponse:
        return cls(
            answer=run.answer,
            stop_reason=run.stop_reason,
            steps=run.steps,
            provider=run.provider,
            model=run.model,
            usage=UsageResponse(
                input_tokens=run.usage.input_tokens,
                output_tokens=run.usage.output_tokens,
            ),
            trace_id=run.trace_id,
        )


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class LivenessResponse(BaseModel):
    status: str = "alive"


class DependencyStatusResponse(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: str
    dependencies: list[DependencyStatusResponse]
