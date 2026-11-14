"""Runs every selected case ``repeats`` times against the real agent and grades each run."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from evals.dataset import Case, Dataset
from evals.graders import Check, grade
from evals.trajectory import Trajectory
from src.domain.agent import AgentRun
from src.domain.errors import (
    DomainError,
    LLMAuthError,
    ToolCallingNotSupportedError,
    ToolGatewayUnavailableError,
    UnknownProviderError,
)

logger = logging.getLogger(__name__)

# Misconfiguration, not model behaviour. Transient errors (LLMUnavailableError, rate limits)
# are recorded per run instead: a provider can recover in the middle of an eval.
_ENVIRONMENT_ERRORS = (
    ToolGatewayUnavailableError,
    LLMAuthError,
    UnknownProviderError,
    ToolCallingNotSupportedError,
)


class EvalAbortedError(RuntimeError):
    """The eval cannot produce meaningful results in the current environment."""


class Agent(Protocol):
    async def execute(self, prompt: str, provider: str | None = None) -> AgentRun: ...


# Builds an agent whose tool calls to the given tool names return the given canned text.
AgentFactory = Callable[[Mapping[str, str]], Agent]


@dataclass(frozen=True, slots=True)
class RunRecord:
    case_id: str
    repeat: int
    tags: tuple[str, ...]
    checks: tuple[Check, ...]
    trajectory: Trajectory | None
    latency_seconds: float
    # Set when the run raised instead of finishing (provider down, MCP unreachable...).
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)


class EvalRunner:
    """Runs cases one at a time.

    Sequential on purpose: a local model serves one request at a time, hosted APIs rate
    limit, and latency numbers are only comparable when runs do not compete with each other.
    """

    def __init__(
        self,
        dataset: Dataset,
        agent_for: AgentFactory,
        *,
        provider: str | None,
        repeats: int,
        on_record: Callable[[RunRecord], None] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if repeats < 1:
            raise ValueError("repeats must be at least 1")
        self._dataset = dataset
        self._agent_for = agent_for
        self._provider = provider
        self._repeats = repeats
        self._on_record = on_record
        self._clock = clock

    async def run(self, cases: Sequence[Case]) -> list[RunRecord]:
        records: list[RunRecord] = []
        for case in cases:
            agent = self._agent_for(self._dataset.stubs_for(case))
            for repeat in range(1, self._repeats + 1):
                record = await self._run_once(agent, case, repeat)
                records.append(record)
                if self._on_record is not None:
                    self._on_record(record)
        return records

    async def _run_once(self, agent: Agent, case: Case, repeat: int) -> RunRecord:
        started = self._clock()
        try:
            run = await agent.execute(case.prompt, self._provider)
        except _ENVIRONMENT_ERRORS as err:
            # Every following run would fail the same way: stop now instead of after N timeouts.
            raise EvalAbortedError(
                f"{type(err).__name__} on case '{case.id}': check the provider, its credentials "
                "and the MCP server before running the evals"
            ) from err
        except DomainError as err:
            # A failure of the system under test is a result to report, not a harness crash.
            logger.warning("Eval run failed", extra={"case": case.id, "error": type(err).__name__})
            return RunRecord(
                case_id=case.id,
                repeat=repeat,
                tags=case.tags,
                checks=(),
                trajectory=None,
                latency_seconds=self._clock() - started,
                error=type(err).__name__,
            )
        trajectory = Trajectory.from_run(run)
        checks = grade(self._dataset.expectation_for(case), trajectory)
        return RunRecord(
            case_id=case.id,
            repeat=repeat,
            tags=case.tags,
            checks=tuple(checks),
            trajectory=trajectory,
            latency_seconds=self._clock() - started,
        )
