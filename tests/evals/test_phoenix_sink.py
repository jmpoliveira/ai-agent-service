from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from phoenix.client import AsyncClient

from config.settings import PhoenixSettings
from evals.graders import Check
from evals.phoenix_sink import PhoenixSink, open_phoenix_sink, phoenix_dataset_name
from evals.runner import RunRecord
from evals.sink import EvalResults, SinkError
from tests.evals.helpers import case, dataset, trajectory
from tests.evals.phoenix_fake import FakePhoenix

STARTED = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
META: dict[str, Any] = {
    "provider": "ollama",
    "model": "qwen3:8b",
    "temperature": 0.0,
    "system_prompt_fingerprint": "abc",
    "git_commit": "1234567",
    "started_at": STARTED.isoformat(),
    "warnings": ["not copied to the experiment"],
}
DATA = dataset(
    case("lookup", "Who is C-001?", tags=("customer",), expect={"tools": ["search_customers"]}),
    case("hello", "Hi!", expect={"no_tools": True}),
    stub_tools={"create_ticket": "{}"},
    defaults={"forbidden_tools": ["create_ticket"]},
)


def _run(
    case_id: str,
    *,
    repeat: int = 1,
    checks: Sequence[Check] = (Check("answer", True),),
    error: str | None = None,
    started_at: datetime | None = STARTED,
) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        repeat=repeat,
        tags=(),
        checks=tuple(checks),
        trajectory=None
        if error
        else trajectory(
            [("search_customers", {"query": "C-001"})],
            answer="Alice",
            results=["x" * 5000],
        ),
        latency_seconds=2.5,
        error=error,
        started_at=started_at,
    )


def _results(*records: RunRecord, report: str = "20261117-120000-ollama-test.json") -> EvalResults:
    return EvalResults(DATA, META, records, report)


async def _publish(fake: FakePhoenix, results: EvalResults) -> Any:
    async with fake.client() as http:
        return await PhoenixSink(AsyncClient(http_client=http)).publish(results)


async def test_publish_creates_the_dataset_experiment_runs_and_evaluations():
    # Arrange
    fake = FakePhoenix()
    results = _results(
        _run("lookup", checks=[Check("tool_sequence", True), Check("answer", False, "missing")]),
        _run("hello", repeat=2),
    )

    # Act
    published = await _publish(fake, results)

    # Assert: one dataset named after the grading rules, one example per case
    [ds] = fake.datasets.values()
    assert ds["name"] == phoenix_dataset_name(DATA) == f"test-v1-{DATA.fingerprint}"
    examples = fake.examples[ds["id"]]
    assert [e["input"] for e in examples] == [{"prompt": "Who is C-001?"}, {"prompt": "Hi!"}]
    assert examples[0]["metadata"] == {
        "case_id": "lookup",
        "tags": ["customer"],
        "stubbed_tools": ["create_ticket"],
    }
    # the expectation with the defaults applied, without the unchecked fields
    assert examples[0]["output"]["expect"] == {
        "tools": ["search_customers"],
        "forbidden_tools": ["create_ticket"],
        "stop_reason": "finished",
    }
    # one experiment per report, carrying what explains a difference between runs
    [experiment] = fake.experiments
    assert experiment["name"] == f"ollama / qwen3:8b @ {STARTED.isoformat()}"
    assert experiment["repetitions"] == 2
    assert experiment["metadata"]["report"] == "20261117-120000-ollama-test.json"
    assert experiment["metadata"]["git_commit"] == "1234567"
    assert "warnings" not in experiment["metadata"]
    # every run, linked to its trace, with the answer and the (capped) tool results
    first, second = fake.runs
    assert first["dataset_example_id"] == examples[0]["id"]
    assert first["trace_id"] == "abc123"
    assert first["output"]["answer"] == "Alice"
    assert len(first["output"]["tool_calls"][0]["result"]) == 2000
    assert (second["dataset_example_id"], second["repetition_number"]) == (examples[1]["id"], 2)
    assert first["end_time"] == (STARTED + timedelta(seconds=2.5)).isoformat()
    # one evaluation per check plus the overall verdict
    evaluations = fake.evaluations_for(first["id"])
    assert set(evaluations) == {"tool_sequence", "answer", "passed"}
    assert evaluations["answer"]["result"] == {
        "score": 0.0,
        "label": "fail",
        "explanation": "missing",
    }
    assert evaluations["passed"]["result"]["explanation"] == "failed: answer"
    assert evaluations["tool_sequence"]["annotator_kind"] == "CODE"
    assert published.created is True
    assert published.url == (
        f"http://phoenix.test:6006/datasets/{ds['id']}/compare?experimentId={experiment['id']}"
    )


async def test_publishing_again_reuses_the_dataset_and_does_not_duplicate_the_report():
    # Arrange
    fake = FakePhoenix()
    await _publish(fake, _results(_run("lookup")))

    # Act: the same report again, then another report graded by the same dataset
    again = await _publish(fake, _results(_run("lookup")))
    other = await _publish(fake, _results(_run("lookup"), report="other.json"))

    # Assert
    assert again.created is False
    assert again.url.endswith(f"experimentId={fake.experiments[0]['id']}")
    assert other.created is True
    assert fake.uploads == 1
    assert len(fake.experiments) == 2
    assert len(fake.runs) == 2


async def test_the_expectation_keeps_zero_limits_and_drops_unset_matcher_fields():
    # Arrange
    fake = FakePhoenix()
    data = dataset(
        case("c", expect={"arguments": {"search_customers": {"query": {"contains": "alice"}}}}),
        defaults={"max_tool_errors": 0},
    )

    # Act
    await _publish(fake, EvalResults(data, META, [_run("c")], "r.json"))

    # Assert
    [examples] = fake.examples.values()
    assert examples[0]["output"]["expect"] == {
        "arguments": {"search_customers": {"query": {"contains": "alice"}}},
        "max_tool_errors": 0,
        "stop_reason": "finished",
    }


async def test_a_failed_run_is_published_with_its_error():
    fake = FakePhoenix()

    await _publish(fake, _results(_run("lookup", checks=(), error="LLMUnavailableError")))

    [run] = fake.runs
    assert run["error"] == "LLMUnavailableError"
    assert "trace_id" not in run
    assert fake.evaluations_for(run["id"])["passed"]["result"] == {
        "score": 0.0,
        "label": "error",
        "explanation": "run raised LLMUnavailableError",
    }


async def test_runs_without_a_start_time_are_placed_one_after_another():
    # Reports written before runs were timestamped only know when the eval started.
    fake = FakePhoenix()
    records = [_run("lookup", started_at=None), _run("hello", started_at=None)]

    await _publish(fake, _results(*records))

    starts = [run["start_time"] for run in fake.runs]
    assert starts == [STARTED.isoformat(), (STARTED + timedelta(seconds=2.5)).isoformat()]


async def test_an_unreachable_or_failing_server_is_a_sink_error():
    fake = FakePhoenix(fail_with=500)

    with pytest.raises(SinkError, match="Phoenix request failed"):
        await _publish(fake, _results(_run("lookup")))


async def test_a_dataset_without_an_example_for_a_case_is_rejected():
    # Arrange: a dataset with the expected name exists, but was created with other cases.
    fake = FakePhoenix()
    await _publish(fake, _results(_run("hello")))
    [ds_id] = fake.examples
    fake.examples[ds_id] = [e for e in fake.examples[ds_id] if e["metadata"]["case_id"] != "lookup"]

    # Act / Assert
    with pytest.raises(SinkError, match=r"no example for cases \['lookup'\]"):
        await _publish(fake, _results(_run("lookup"), report="new.json"))


async def test_open_phoenix_sink_requires_a_base_url():
    settings = PhoenixSettings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(SinkError, match="PHOENIX_BASE_URL is not set"):
        async with open_phoenix_sink(settings):
            pass


async def test_open_phoenix_sink_sends_the_api_key_as_a_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
):
    # Arrange: capture the headers of the client the factory builds.
    seen: dict[str, str] = {}
    fake = FakePhoenix()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return fake.handle(request)

    real = httpx.AsyncClient

    def patched(**kwargs: Any) -> httpx.AsyncClient:
        return real(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("evals.phoenix_sink.httpx.AsyncClient", patched)
    settings = PhoenixSettings(
        _env_file=None,  # type: ignore[call-arg]
        base_url="http://phoenix.test:6006",  # type: ignore[arg-type]
        api_key="secret-key",  # type: ignore[arg-type]
    )

    # Act
    async with open_phoenix_sink(settings) as sink:
        await sink.publish(_results(_run("lookup")))

    # Assert
    assert seen["authorization"] == "Bearer secret-key"
