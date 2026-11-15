from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.report import ReportError, read_report, render, summarise, write_report
from tests.evals.helpers import record

META: dict[str, Any] = {
    "dataset": "tool_use",
    "dataset_version": 1,
    "dataset_fingerprint": "abc",
    "provider": "ollama",
    "model": "qwen3:8b",
    "temperature": 0.0,
    "cases": 2,
    "repeats": 2,
    "system_prompt_fingerprint": "def",
}


def _records() -> list[Any]:
    return [
        record("a", tags=("lookup",), latency=1.0, steps=2),
        record("a", repeat=2, tags=("lookup",), latency=3.0, steps=4),
        record("b", passed=False, tags=("tickets",), latency=2.0),
        record("b", repeat=2, error="LLMUnavailableError", tags=("tickets",), latency=6.0),
    ]


def test_summary_rates_and_averages():
    summary = summarise(_records())

    assert summary.runs == 4
    assert summary.pass_rate == 0.5
    assert summary.case_rates == {"a": 1.0, "b": 0.0}
    assert summary.consistent_cases == 1
    assert summary.tag_rates == {"lookup": 1.0, "tickets": 0.0}
    assert summary.check_rates == {"answer": 2 / 3}  # the errored run has no checks
    assert summary.errors == {"LLMUnavailableError": 1}
    assert summary.avg_steps == pytest.approx((2 + 4 + 2) / 3)
    assert summary.avg_latency_seconds == 3.0
    assert summary.max_latency_seconds == 6.0


def test_empty_records_give_a_zero_summary():
    summary = summarise([])

    assert (summary.runs, summary.pass_rate, summary.avg_steps) == (0, 0.0, 0.0)


def test_report_round_trip_keeps_meta_summary_and_every_run(tmp_path: Path):
    # Arrange
    path = tmp_path / "nested" / "report.json"
    summary = summarise(_records())

    # Act
    write_report(path, META, summary, _records())
    meta, loaded = read_report(path)

    # Assert
    assert meta == META
    assert loaded == summary
    runs = json.loads(path.read_text(encoding="utf-8"))["runs"]
    assert len(runs) == 4
    assert runs[0]["trajectory"]["trace_id"] == "abc123"
    assert runs[3]["trajectory"] is None


@pytest.mark.parametrize("content", ["not json", '{"meta": {}}', '{"meta": {}, "summary": 1}'])
def test_unreadable_baselines_are_reported(tmp_path: Path, content: str):
    path = tmp_path / "base.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ReportError, match="not an eval report"):
        read_report(path)


def test_missing_baseline_is_reported(tmp_path: Path):
    with pytest.raises(ReportError, match="cannot read baseline"):
        read_report(tmp_path / "missing.json")


def test_render_shows_metrics_failures_and_trace_ids():
    text = render(META, summarise(_records()), _records())

    assert "tool_use v1 (abc) · ollama / qwen3:8b · temperature 0.0" in text
    assert "run pass rate" in text
    assert "50.0%" in text
    assert "tag: tickets" in text
    assert "Errors: LLMUnavailableError x1" in text
    assert "b #1: answer: missing ['x'] (trace abc123)" in text
    assert "b #2: error LLMUnavailableError" in text


def test_render_compares_with_a_baseline():
    current = summarise(_records())
    baseline = summarise([record("a", passed=False), record("b")])

    text = render(META, current, _records(), baseline)

    pass_rate_line = next(line for line in text.splitlines() if line.startswith("run pass rate"))
    assert pass_rate_line.split()[-3:] == ["50.0%", "50.0%", "+0.0"]
    assert "Changed cases vs baseline:" in text
    assert "a: 0.0% -> 100.0%" in text
    assert "b: 100.0% -> 0.0%" in text


def test_render_names_the_baseline_and_warns_when_its_dataset_differs():
    base_meta = {**META, "started_at": "2026-11-14T19:35:08+00:00"}
    summary = summarise(_records())

    same = render(META, summary, _records(), summary, base_meta)
    other = render(META, summary, _records(), summary, {**base_meta, "dataset_fingerprint": "x"})

    assert "Baseline: tool_use v1 (abc) · ollama / qwen3:8b" in same
    assert "WARNING" not in same
    assert "WARNING: the baseline was graded with a different dataset" in other


def test_header_shows_commit_and_regrade_origin_and_tolerates_old_reports():
    meta = {
        **META,
        "git_commit": "f810f03aaaa",
        "git_dirty": True,
        "regraded_from": {"dataset_version": 3},
    }

    header = render(meta, summarise([]), []).splitlines()[0]
    old = render({"dataset": "tool_use"}, summarise([]), []).splitlines()[0]

    assert "commit f810f03 (uncommitted changes)" in header
    assert "regraded from v3" in header
    assert old.startswith("Dataset tool_use v? (?) · ? / ?")
