"""The agent loop: model turn -> tool calls -> model turn... until a stop condition.

The loop is explicit on purpose. It is the business logic of an agent, it must always
terminate (budget) and it must be testable without a network (scripted fake model).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from src.application.catalog import ModelCatalog
from src.application.conversation_log import ConversationLogger
from src.application.genai_telemetry import (
    chat_span,
    current_trace_id,
    execute_tool_span,
    invoke_agent_span,
    mark_error,
    record_agent_run,
    record_chat_result,
)
from src.application.ports import ChatModel, ToolGateway, ToolGatewayProvider
from src.application.sanitization import (
    frame_tool_output,
    sanitize_prompt,
    sanitize_tool_output,
)
from src.domain.agent import AgentBudget, AgentRun, StopReason
from src.domain.errors import LLMResponseError, ToolExecutionError
from src.domain.llm import ChatResult, FinishReason
from src.domain.messages import Message, ToolCall
from src.domain.tools import ToolResult, ToolSpec
from src.domain.usage import Usage

logger = logging.getLogger(__name__)

_TOOL_UNAVAILABLE_MESSAGE = "Error: unknown or unavailable tool."
_TOOL_FAILED_MESSAGE = "Error: the tool call failed."


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Everything ``RunAgent`` needs that is not a collaborator. Built from settings."""

    name: str
    system_prompt: str
    max_prompt_length: int
    max_tool_output_chars: int
    budget: AgentBudget
    tool_allowlist: frozenset[str] | None = None


@dataclass(slots=True)
class _RunState:
    """Mutable state of one run. Lives only inside ``execute``; the service stays stateless."""

    messages: list[Message]
    usage: Usage
    steps: int = 0
    stop_reason: StopReason = StopReason.MAX_STEPS
    answer: str = ""
    last_result: ChatResult | None = None


class RunAgent:
    """Use case: answer a prompt, calling tools through the gateway as needed."""

    def __init__(
        self,
        catalog: ModelCatalog,
        tools: ToolGatewayProvider,
        config: AgentConfig,
        conversation_log: ConversationLogger | None = None,
    ) -> None:
        self._catalog = catalog
        self._tools = tools
        self._config = config
        self._conversation_log = conversation_log

    @property
    def providers(self) -> tuple[str, ...]:
        return self._catalog.aliases

    async def execute(self, prompt: str, provider: str | None = None) -> AgentRun:
        """Run the agent.

        Raises:
            InvalidPromptError: the prompt is empty or too long.
            UnknownProviderError: ``provider`` is not configured.
            LLMError: a model call failed after retries and fallbacks.
            ToolGatewayUnavailableError: the tool server cannot be reached.
        """
        clean_prompt = sanitize_prompt(prompt, self._config.max_prompt_length)
        model = self._catalog.get(provider)

        with invoke_agent_span(self._config.name, model.provider, model.model) as span:
            logger.info(
                "Agent run started",
                extra={"provider": model.provider, "model": model.model},
            )
            if self._conversation_log is not None:
                self._conversation_log.prompt(clean_prompt)
            state = _RunState(
                messages=[
                    Message.system(self._config.system_prompt),
                    Message.user(clean_prompt),
                ],
                usage=Usage(),
            )
            await self._run_with_deadline(state, model)
            run = self._build_run(state, model)
            record_agent_run(span, run)
            logger.info(
                "Agent run finished",
                extra={
                    "provider": run.provider,
                    "model": run.model,
                    "steps": run.steps,
                    "stop_reason": run.stop_reason.value,
                    "input_tokens": run.usage.input_tokens,
                    "output_tokens": run.usage.output_tokens,
                },
            )
            return run

    async def _run_with_deadline(self, state: _RunState, model: ChatModel) -> None:
        deadline = asyncio.timeout(self._config.budget.timeout_seconds)
        try:
            async with deadline, self._tools.open() as gateway:
                specs = self._visible_tools(await gateway.list_tools())
                await self._loop(state, model, gateway, specs)
        except TimeoutError:
            if not deadline.expired():
                raise
            state.stop_reason = StopReason.TIMEOUT
            logger.warning("Agent run hit the time budget")

    async def _loop(
        self,
        state: _RunState,
        model: ChatModel,
        gateway: ToolGateway,
        specs: Sequence[ToolSpec],
    ) -> None:
        budget = self._config.budget
        offered = frozenset(spec.name for spec in specs)
        # Counted over the whole run, not only consecutive steps: a model that alternates
        # between the same calls is looping too.
        seen_calls: Counter[tuple[str, str]] = Counter()

        for _ in range(budget.max_steps):
            result = await self._chat(model, state.messages, specs)
            state.steps += 1
            state.usage += result.usage
            state.last_result = result
            state.messages.append(result.message)
            if self._conversation_log is not None:
                self._conversation_log.model_reply(state.steps, result)

            calls = result.message.tool_calls
            if not calls:
                state.answer = self._require_answer(result)
                truncated = result.finish_reason is FinishReason.MAX_TOKENS
                state.stop_reason = (
                    StopReason.OUTPUT_TRUNCATED if truncated else StopReason.FINISHED
                )
                return

            if state.usage.total_tokens >= budget.max_total_tokens:
                state.stop_reason = StopReason.TOKEN_BUDGET
                return

            if self._is_looping(calls, seen_calls, budget.max_identical_tool_calls):
                state.stop_reason = StopReason.REPEATED_TOOL_CALL
                return

            for call in calls:
                tool_result = await self._execute_tool(gateway, call, offered)
                if self._conversation_log is not None:
                    self._conversation_log.tool_result(tool_result)
                state.messages.append(Message.from_tool_result(tool_result))

        state.stop_reason = StopReason.MAX_STEPS

    async def _chat(
        self,
        model: ChatModel,
        messages: Sequence[Message],
        specs: Sequence[ToolSpec],
    ) -> ChatResult:
        with chat_span(model.provider, model.model) as span:
            result = await model.chat(messages, specs)
            record_chat_result(span, result)
            return result

    async def _execute_tool(
        self,
        gateway: ToolGateway,
        call: ToolCall,
        offered: frozenset[str],
    ) -> ToolResult:
        with execute_tool_span(call) as span:
            # The model can hallucinate a tool name, or be steered towards a tool that
            # was filtered out. Only tools we offered may be called.
            if call.name not in offered:
                logger.warning(
                    "Model requested a tool that was not offered", extra={"tool": call.name}
                )
                mark_error(span, "tool_not_offered")
                return ToolResult(call.id, call.name, _TOOL_UNAVAILABLE_MESSAGE, is_error=True)

            try:
                raw = await gateway.call_tool(call)
            except ToolExecutionError:
                logger.warning("Tool call failed", extra={"tool": call.name}, exc_info=True)
                mark_error(span, "tool_execution_error")
                return ToolResult(call.id, call.name, _TOOL_FAILED_MESSAGE, is_error=True)

            if raw.is_error:
                mark_error(span, "tool_reported_error")

            safe = sanitize_tool_output(raw.content, self._config.max_tool_output_chars)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=frame_tool_output(call.name, safe),
                is_error=raw.is_error,
            )

    def _visible_tools(self, available: Sequence[ToolSpec]) -> list[ToolSpec]:
        allowlist = self._config.tool_allowlist
        if allowlist is None:
            return list(available)
        visible = [spec for spec in available if spec.name in allowlist]
        missing = allowlist - {spec.name for spec in available}
        if missing:
            logger.warning(
                "Allow-listed tools not found on the server", extra={"tools": sorted(missing)}
            )
        return visible

    @staticmethod
    def _is_looping(
        calls: Sequence[ToolCall],
        seen: Counter[tuple[str, str]],
        limit: int,
    ) -> bool:
        """True when the model repeats the exact same call more than ``limit`` times."""
        for call in calls:
            # sort_keys: {"a": 1, "b": 2} and {"b": 2, "a": 1} are the same call.
            signature = (call.name, json.dumps(call.arguments, sort_keys=True, default=str))
            seen[signature] += 1
            if seen[signature] > limit:
                logger.warning("Repeated identical tool call", extra={"tool": call.name})
                return True
        return False

    @staticmethod
    def _require_answer(result: ChatResult) -> str:
        answer = result.message.content.strip()
        if not answer:
            raise LLMResponseError("The model returned an empty answer", provider=result.provider)
        return answer

    @staticmethod
    def _build_run(state: _RunState, requested: ChatModel) -> AgentRun:
        last = state.last_result
        return AgentRun(
            answer=state.answer,
            stop_reason=state.stop_reason,
            steps=state.steps,
            usage=state.usage,
            provider=last.provider if last else requested.provider,
            model=last.model if last else requested.model,
            messages=tuple(state.messages),
            trace_id=current_trace_id(),
        )
