"""Port for publishing eval results somewhere they can be explored (ADR 0006).

The JSON report is the source of truth: it is written first, kept in git as a baseline and
can be re-graded. A sink is a *view* of it: a tool where each case can be followed end to end
(prompt, tool calls, checks, trace) and runs can be compared case by case. Phoenix is the
first implementation; another one (for example Azure AI Foundry) only needs this interface.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from evals.dataset import Dataset
from evals.runner import RunRecord


@dataclass(frozen=True, slots=True)
class EvalResults:
    """One finished eval run, as saved in its report."""

    dataset: Dataset
    meta: Mapping[str, Any]
    records: Sequence[RunRecord]
    # The report file name: identifies the run, so publishing it twice can be detected.
    report_name: str


@dataclass(frozen=True, slots=True)
class Published:
    url: str
    # False when this report had already been published: nothing new was written.
    created: bool


class SinkError(RuntimeError):
    """The results could not be published. The report itself is not affected."""


class ResultSink(Protocol):
    async def publish(self, results: EvalResults) -> Published: ...


# Opens a sink for the duration of one publish (the sink owns network clients).
SinkFactory = Callable[[], AbstractAsyncContextManager[ResultSink]]
