"""Builders shared by the eval tests: a real RunAgent driven by a scripted model."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evals.dataset import Case, Dataset, Expectation
from evals.graders import Check
from evals.runner import RunRecord
from evals.trajectory import RecordedCall, Trajectory
from src.application.catalog import ModelCatalog
from src.application.ports import ToolGatewayProvider
from src.application.run_agent import AgentConfig, RunAgent
from src.domain.agent import AgentBudget
from src.domain.llm import ChatResult
from tests.fakes import FakeToolGateway, FakeToolGatewayProvider, ScriptedChatModel, tool_spec


def make_agent(
    script: Sequence[ChatResult | BaseException],
    tools: ToolGatewayProvider | None = None,
    *,
    alias: str = "fake",
) -> RunAgent:
    gateway = FakeToolGateway([tool_spec("search_customers"), tool_spec("create_ticket")])
    config = AgentConfig("eval-agent", "SYSTEM", 500, 2000, AgentBudget(6, 100_000, 5.0, 2))
    return RunAgent(
        ModelCatalog({alias: ScriptedChatModel(script)}, default=alias),
        tools or FakeToolGatewayProvider(gateway),
        config,
    )


def trajectory(
    calls: Sequence[tuple[str, dict[str, Any]]] = (),
    *,
    answer: str = "",
    stop_reason: str = "finished",
    steps: int = 1,
    errors: Sequence[bool] = (),
    prompt: str | None = None,
    results: Sequence[str] | None = None,
    call_steps: Sequence[int] | None = None,
) -> Trajectory:
    recorded = tuple(
        RecordedCall(
            name,
            args,
            errors[i] if i < len(errors) else False,
            step=call_steps[i] if call_steps else i + 1,
            result=results[i] if results is not None else None,
        )
        for i, (name, args) in enumerate(calls)
    )
    return Trajectory(
        calls=recorded,
        answer=answer,
        stop_reason=stop_reason,
        steps=steps,
        input_tokens=100,
        output_tokens=20,
        provider="fake",
        model="fake-model",
        trace_id="abc123",
        prompt=prompt,
    )


def expect(**fields: Any) -> Expectation:
    return Expectation.model_validate(fields)


def dataset(*cases: Case, **fields: Any) -> Dataset:
    return Dataset.model_validate({"name": "test", "version": 1, "cases": cases, **fields})


def case(case_id: str = "c1", prompt: str = "question", **fields: Any) -> Case:
    return Case.model_validate({"id": case_id, "prompt": prompt, **fields})


def record(
    case_id: str = "c1",
    *,
    passed: bool = True,
    repeat: int = 1,
    tags: tuple[str, ...] = (),
    error: str | None = None,
    latency: float = 1.0,
    steps: int = 2,
) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        repeat=repeat,
        tags=tags,
        checks=(Check("answer", passed, "" if passed else "missing ['x']"),) if not error else (),
        trajectory=None if error else trajectory(steps=steps),
        latency_seconds=latency,
        error=error,
    )
