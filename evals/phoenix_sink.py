"""Publishes eval results to Arize Phoenix as a dataset plus an experiment (ADR 0006).

How a report maps onto Phoenix:

- dataset file -> a Phoenix dataset named ``<name>-v<version>-<fingerprint>``, one example
  per case (input: the prompt; output: the expectation with the dataset defaults applied;
  metadata: case id, tags, stubbed tools). The fingerprint in the name means that only runs
  graded by the same rules share a dataset, so Phoenix's side-by-side comparison never mixes a
  behaviour change with an expectation change (the same rule as ``--baseline``).
- report -> an experiment on that dataset (provider, model, prompt fingerprint, commit...).
- run -> an experiment run: answer, tool calls and their results, and the run's trace id,
  which links the case to its trace (the trace itself arrives through the OTel collector).
- check -> an evaluation on that run (score 1/0, label pass/fail, the check's detail), plus
  an overall ``passed`` evaluation.

Publishing only records results that the runner already produced; Phoenix never drives the
agent. That keeps one way of running cases (repeats, stubs, rate limits, abort rules) and lets
any saved report, including old baselines, be published later.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from phoenix.client import AsyncClient
from phoenix.client.exceptions import PhoenixException
from phoenix.client.resources.datasets import Dataset as PhoenixDataset
from phoenix.client.resources.datasets import DatasetUploadError

from config.settings import PhoenixSettings
from evals.dataset import Case, Dataset
from evals.report import parse_timestamp
from evals.runner import RunRecord
from evals.sink import EvalResults, Published, ResultSink, SinkError

logger = logging.getLogger(__name__)

OVERALL = "passed"
# Report metadata copied onto the experiment: what explains a difference between two runs.
_EXPERIMENT_META = (
    "provider",
    "model",
    "temperature",
    "ollama_reasoning",
    "foundry_reasoning_effort",
    "system_prompt_fingerprint",
    "dataset_version",
    "dataset_fingerprint",
    "git_commit",
    "git_dirty",
    "repeats",
    "max_rpm",
    "started_at",
    "regraded_from",
)
# Tool results can be long; the report keeps them in full.
_MAX_RESULT_CHARS = 2000


def phoenix_dataset_name(dataset: Dataset) -> str:
    return f"{dataset.name}-v{dataset.version}-{dataset.fingerprint}"


class PhoenixSink:
    """A ``ResultSink`` backed by the Phoenix REST API (through the official client)."""

    def __init__(self, client: AsyncClient, *, timeout_seconds: int = 30) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def publish(self, results: EvalResults) -> Published:
        try:
            return await self._publish(results)
        except (httpx.HTTPError, PhoenixException, DatasetUploadError) as err:
            raise SinkError(f"Phoenix request failed: {err}") from err

    async def _publish(self, results: EvalResults) -> Published:
        dataset = await self._dataset_for(results.dataset)
        example_ids = _example_ids_by_case(dataset)
        missing = sorted({r.case_id for r in results.records} - example_ids.keys())
        if missing:
            raise SinkError(f"Phoenix dataset '{dataset.name}' has no example for cases {missing}")

        experiments = self._client.experiments
        existing = await self._experiment_for_report(dataset.id, results.report_name)
        if existing is not None:
            logger.info("Report already published", extra={"report": results.report_name})
            return Published(experiments.get_experiment_url(dataset.id, existing), created=False)

        meta = results.meta
        experiment = await experiments.create(
            dataset_id=dataset.id,
            dataset_version_id=dataset.version_id,
            experiment_name=f"{meta.get('provider')} / {meta.get('model')}"
            f" @ {meta.get('started_at', '?')}",
            experiment_description=f"Eval report {results.report_name}",
            experiment_metadata={
                "report": results.report_name,
                **{key: meta[key] for key in _EXPERIMENT_META if key in meta},
            },
            repetitions=max((r.repeat for r in results.records), default=1),
            timeout=self._timeout,
        )
        for record, started in _timeline(results):
            await self._log_run(experiment["id"], example_ids[record.case_id], record, started)
        logger.info(
            "Eval results published",
            extra={"experiment": experiment["id"], "runs": len(results.records)},
        )
        return Published(experiments.get_experiment_url(dataset.id, experiment["id"]), True)

    async def _dataset_for(self, dataset: Dataset) -> PhoenixDataset:
        """The Phoenix dataset for these exact grading rules, created on first use."""
        name = phoenix_dataset_name(dataset)
        datasets = self._client.datasets
        listed = await datasets.list(timeout=self._timeout)
        found = next((item for item in listed if item["name"] == name), None)
        if found is not None:
            return await datasets.get_dataset(dataset=found["id"], timeout=self._timeout)
        logger.info("Creating Phoenix dataset", extra={"dataset": name})
        # The optional `dataframe` parameter is typed with pandas, which we do not install.
        return await datasets.create_dataset(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            inputs=[{"prompt": case.prompt} for case in dataset.cases],
            outputs=[{"expect": _expectation(dataset, case)} for case in dataset.cases],
            metadata=[
                {
                    "case_id": case.id,
                    "tags": list(case.tags),
                    "stubbed_tools": sorted(dataset.stubs_for(case)),
                }
                for case in dataset.cases
            ],
            dataset_description=dataset.description.strip()
            or f"Eval dataset {dataset.name} v{dataset.version}",
            timeout=self._timeout,
        )

    async def _experiment_for_report(self, dataset_id: str, report_name: str) -> str | None:
        experiments = await self._client.experiments.list(
            dataset_id=dataset_id, timeout=self._timeout
        )
        return next(
            (e["id"] for e in experiments if e["metadata"].get("report") == report_name), None
        )

    async def _log_run(
        self, experiment_id: str, example_id: str, record: RunRecord, started: datetime
    ) -> None:
        ended = started + timedelta(seconds=record.latency_seconds)
        trajectory = record.trajectory
        run = await self._client.experiments.log_run(
            experiment_id=experiment_id,
            dataset_example_id=example_id,
            output=None if trajectory is None else _output(record),
            start_time=started,
            end_time=ended,
            repetition_number=record.repeat,
            trace_id=trajectory.trace_id if trajectory else None,
            error=record.error,
            timeout=self._timeout,
        )
        for name, passed, label, explanation in _evaluations(record):
            await self._client.experiments.log_evaluation(
                experiment_run_id=run["id"],
                name=name,
                annotator_kind="CODE",
                start_time=ended,
                end_time=ended,
                score=1.0 if passed else 0.0,
                label=label,
                explanation=explanation or None,
                timeout=self._timeout,
            )


@asynccontextmanager
async def open_phoenix_sink(settings: PhoenixSettings) -> AsyncGenerator[ResultSink, None]:
    """A Phoenix sink whose HTTP client is closed when the block ends.

    The client is built from our settings only (base URL, optional API key), so Phoenix's own
    environment variables and config files cannot silently point it somewhere else.
    """
    if settings.base_url is None:
        raise SinkError("PHOENIX_BASE_URL is not set: there is nowhere to publish to")
    headers = (
        {"Authorization": f"Bearer {settings.api_key.get_secret_value()}"}
        if settings.api_key
        else {}
    )
    async with httpx.AsyncClient(
        base_url=str(settings.base_url),
        headers=headers,
        timeout=settings.request_timeout_seconds,
    ) as http:
        yield PhoenixSink(
            AsyncClient(http_client=http),
            timeout_seconds=max(1, round(settings.request_timeout_seconds)),
        )


def _example_ids_by_case(dataset: PhoenixDataset) -> dict[str, str]:
    return {
        str(example["metadata"]["case_id"]): example["id"]
        for example in dataset.examples
        if "case_id" in example["metadata"]
    }


def _expectation(dataset: Dataset, case: Case) -> dict[str, Any]:
    """What the case checks, without the fields left at their "not checked" default."""
    return _compact(dataset.expectation_for(case).model_dump(mode="json"))


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    # Identity checks, not `in (None, False, ...)`: 0 == False in Python, and a limit of 0
    # (for example max_tool_errors) is a real expectation.
    return {
        key: _compact(cast(dict[str, Any], value)) if isinstance(value, dict) else value
        for key, value in values.items()
        if not (value is None or value is False or value == [] or value == {})
    }


def _output(record: RunRecord) -> dict[str, Any]:
    trajectory = record.trajectory
    assert trajectory is not None  # noqa: S101 - callers only pass finished runs
    return {
        "answer": trajectory.answer,
        "tool_calls": [
            {
                "step": call.step,
                "name": call.name,
                "arguments": call.arguments,
                "is_error": call.is_error,
                "result": None if call.result is None else call.result[:_MAX_RESULT_CHARS],
            }
            for call in trajectory.calls
        ],
        "stop_reason": trajectory.stop_reason,
        "steps": trajectory.steps,
        "input_tokens": trajectory.input_tokens,
        "output_tokens": trajectory.output_tokens,
        "latency_seconds": round(record.latency_seconds, 3),
    }


def _evaluations(record: RunRecord) -> Iterator[tuple[str, bool, str, str]]:
    """(name, passed, label, explanation) for every check, then the overall verdict."""
    for check in record.checks:
        yield check.name, check.passed, "pass" if check.passed else "fail", check.detail
    if record.error:
        yield OVERALL, False, "error", f"run raised {record.error}"
        return
    failed = ", ".join(check.name for check in record.checks if not check.passed)
    yield (
        OVERALL,
        record.passed,
        "pass" if record.passed else "fail",
        (f"failed: {failed}" if failed else ""),
    )


def _timeline(results: EvalResults) -> Iterator[tuple[RunRecord, datetime]]:
    """Each run with its start time.

    Reports written before runs were timestamped only have the eval's start: runs were
    sequential, so each start is estimated as that time plus the latencies before it.
    """
    estimate = parse_timestamp(results.meta.get("started_at")) or datetime.now(UTC)
    for record in results.records:
        started = record.started_at or estimate
        yield record, started
        estimate = started + timedelta(seconds=record.latency_seconds)
