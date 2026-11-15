from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.regrade import regrade
from evals.report import ReportError, summarise, write_report
from evals.runner import RunRecord
from evals.trajectory import trajectory_from_report
from tests.evals.helpers import case, dataset, trajectory

META: dict[str, Any] = {
    "dataset": "test",
    "dataset_version": 1,
    "dataset_fingerprint": "old",
    "provider": "ollama",
    "model": "qwen3:8b",
}


def _saved_report(tmp_path: Path, records: list[RunRecord]) -> dict[str, Any]:
    path = tmp_path / "report.json"
    write_report(path, META, summarise(records), records)
    return json.loads(path.read_text(encoding="utf-8"))


def _record(case_id: str, answer: str, *, error: str | None = None) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        repeat=1,
        tags=(),
        checks=(),
        trajectory=None if error else trajectory([("get_ticket_info", {"id": 1})], answer=answer),
        latency_seconds=2.5,
        error=error,
    )


def test_saved_trajectories_round_trip(tmp_path: Path):
    original = trajectory(
        [("search_customers", {"query": "Bruno"}), ("get_ticket_info", {"ticket_id": "C-002"})],
        answer="done",
        errors=[False, True],
        steps=3,
    )
    record = RunRecord("a", 1, (), (), original, 1.0)

    saved = _saved_report(tmp_path, [record])["runs"][0]["trajectory"]

    assert trajectory_from_report(saved, "fake", "fake-model") == original


def test_new_expectations_change_the_outcome_of_old_runs(tmp_path: Path):
    # Arrange: the answer says "could not be found"; v1 only accepted "not found"... in v2 both.
    document = _saved_report(tmp_path, [_record("missing", "It could not be found.")])
    v2 = dataset(
        case("missing", expect={"answer_contains_any": ["not found", "not be found"]}),
        version=2,
    )

    # Act
    result = regrade(document, v2, "report.json")

    # Assert
    [record] = result.records
    assert record.passed
    assert record.latency_seconds == 2.5
    assert result.meta["dataset_version"] == 2
    assert result.meta["regraded_from"]["dataset_version"] == 1
    assert result.meta["regraded_from"]["report"] == "report.json"
    assert result.meta["model"] == "qwen3:8b"  # what produced the runs does not change


def test_failed_runs_stay_failed_and_dataset_changes_are_reported(tmp_path: Path):
    document = _saved_report(
        tmp_path,
        [_record("kept", "", error="LLMUnavailableError"), _record("removed", "x")],
    )
    data = dataset(case("kept", tags=["t"]), case("added"))

    result = regrade(document, data, "report.json")

    assert [(r.case_id, r.error, r.tags) for r in result.records] == [
        ("kept", "LLMUnavailableError", ("t",))
    ]
    assert result.notes == [
        "case 'removed' is not in dataset v1: dropped",
        "case 'added' has no runs in this report (new in the dataset): needs a real run",
    ]
    assert result.meta["cases"] == 1


_MALFORMED: list[dict[str, Any]] = [
    {},
    {"meta": {"provider": "x"}, "runs": []},
    {"meta": META, "runs": [{"case": "a"}]},
    {
        "meta": META,
        "runs": [{"case": "a", "repeat": 1, "latency_seconds": 1, "trajectory": {}}],
    },
]


@pytest.mark.parametrize("document", _MALFORMED)
def test_malformed_reports_are_rejected(document: dict[str, Any]):
    with pytest.raises(ReportError, match=r"not an eval report|malformed run"):
        regrade(document, dataset(case("a")), "bad.json")
