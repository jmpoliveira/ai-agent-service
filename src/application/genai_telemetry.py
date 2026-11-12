"""Tracing helpers that follow the OpenTelemetry GenAI semantic conventions.

This is the only module that knows attribute names and span naming. The conventions
are still in "Development" status and may change, so a rename touches one file.

Using ``opentelemetry-api`` directly in the application layer is a deliberate, documented
exception to the dependency rule (see docs/adr/0001-architecture.md): the API is already
vendor-neutral and a wrapper port would add code without adding independence.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from src.domain.agent import AgentRun
from src.domain.llm import ChatResult
from src.domain.messages import ToolCall

_tracer = trace.get_tracer("ai_agent_service")

# Provider aliases used in configuration -> values of gen_ai.provider.name.
_PROVIDER_NAMES = {"gemini": "gcp.gemini", "anthropic": "anthropic", "ollama": "ollama"}

ATTR_OPERATION = "gen_ai.operation.name"
ATTR_PROVIDER = "gen_ai.provider.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_FINISH_REASONS = "gen_ai.response.finish_reasons"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_TOOL_TYPE = "gen_ai.tool.type"
ATTR_ERROR_TYPE = "error.type"
ATTR_AGENT_STEPS = "agent.steps"
ATTR_AGENT_STOP_REASON = "agent.stop_reason"


def provider_name(alias: str) -> str:
    return _PROVIDER_NAMES.get(alias, alias)


def current_trace_id() -> str | None:
    """Hex trace id of the active span, or None when there is no valid active span."""
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


@contextmanager
def invoke_agent_span(agent_name: str, provider: str, model: str) -> Generator[Span]:
    with _tracer.start_as_current_span(
        f"invoke_agent {agent_name}",
        kind=SpanKind.INTERNAL,
        attributes={
            ATTR_OPERATION: "invoke_agent",
            ATTR_AGENT_NAME: agent_name,
            ATTR_PROVIDER: provider_name(provider),
            ATTR_REQUEST_MODEL: model,
        },
    ) as span:
        yield span


def record_agent_run(span: Span, run: AgentRun) -> None:
    span.set_attribute(ATTR_AGENT_STEPS, run.steps)
    span.set_attribute(ATTR_AGENT_STOP_REASON, run.stop_reason.value)
    span.set_attribute(ATTR_INPUT_TOKENS, run.usage.input_tokens)
    span.set_attribute(ATTR_OUTPUT_TOKENS, run.usage.output_tokens)


@contextmanager
def chat_span(provider: str, model: str) -> Generator[Span]:
    with _tracer.start_as_current_span(
        f"chat {model}",
        kind=SpanKind.CLIENT,
        attributes={
            ATTR_OPERATION: "chat",
            ATTR_PROVIDER: provider_name(provider),
            ATTR_REQUEST_MODEL: model,
        },
    ) as span:
        yield span


def record_chat_result(span: Span, result: ChatResult) -> None:
    span.set_attribute(ATTR_RESPONSE_MODEL, result.model)
    span.set_attribute(ATTR_FINISH_REASONS, [result.finish_reason.value])
    span.set_attribute(ATTR_INPUT_TOKENS, result.usage.input_tokens)
    span.set_attribute(ATTR_OUTPUT_TOKENS, result.usage.output_tokens)


@contextmanager
def execute_tool_span(call: ToolCall) -> Generator[Span]:
    """Span around one tool call. Arguments and results are deliberately not recorded."""
    with _tracer.start_as_current_span(
        f"execute_tool {call.name}",
        kind=SpanKind.CLIENT,
        attributes={
            ATTR_OPERATION: "execute_tool",
            ATTR_TOOL_NAME: call.name,
            ATTR_TOOL_CALL_ID: call.id,
            ATTR_TOOL_TYPE: "function",
        },
    ) as span:
        yield span


def mark_error(span: Span, error_type: str) -> None:
    """Flag a span as failed without raising (used for errors fed back to the model)."""
    span.set_attribute(ATTR_ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, error_type))
