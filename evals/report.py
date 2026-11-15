"""Aggregate run records into metrics, save them as JSON and compare with a baseline."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, cast

from evals.runner import RunRecord


@dataclass(frozen=True, slots=True)
class Summary:
    runs: int
    passed_runs: int
    # case id -> fraction of its repeats that passed
    case_rates: dict[str, float]
    # check name -> fraction of runs where that check passed (only runs where it applied)
    check_rates: dict[str, float]
    tag_rates: dict[str, float]
    errors: dict[str, int] = field(default_factory=dict[str, int])
    avg_steps: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_latency_seconds: float = 0.0
    max_latency_seconds: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed_runs / self.runs if self.runs else 0.0

    @property
    def consistent_cases(self) -> int:
        """Cases that passed on every repeat: what "reliably works" means for an LLM."""
        return sum(1 for rate in self.case_rates.values() if rate == 1.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Summary:
        return cls(**dict(data))


def summarise(records: Sequence[RunRecord]) -> Summary:
    by_case: dict[str, list[bool]] = defaultdict(list)
    by_tag: dict[str, list[bool]] = defaultdict(list)
    by_check: dict[str, list[bool]] = defaultdict(list)
    for record in records:
        by_case[record.case_id].append(record.passed)
        for tag in record.tags:
            by_tag[tag].append(record.passed)
        for check in record.checks:
            by_check[check.name].append(check.passed)

    finished = [record.trajectory for record in records if record.trajectory is not None]
    latencies = [record.latency_seconds for record in records]
    return Summary(
        runs=len(records),
        passed_runs=sum(record.passed for record in records),
        case_rates={case: _rate(results) for case, results in by_case.items()},
        check_rates={name: _rate(results) for name, results in sorted(by_check.items())},
        tag_rates={tag: _rate(results) for tag, results in sorted(by_tag.items())},
        errors=dict(Counter(record.error for record in records if record.error)),
        avg_steps=_mean([t.steps for t in finished]),
        avg_input_tokens=_mean([t.input_tokens for t in finished]),
        avg_output_tokens=_mean([t.output_tokens for t in finished]),
        avg_latency_seconds=_mean(latencies),
        max_latency_seconds=max(latencies, default=0.0),
    )


def write_report(
    path: Path, meta: Mapping[str, Any], summary: Summary, records: Sequence[RunRecord]
) -> None:
    """Everything needed to understand a result later: settings, metrics and every run."""
    document = {
        "meta": dict(meta),
        "summary": summary.to_dict(),
        "runs": [_record_to_dict(record) for record in records],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ReportError(ValueError):
    """A baseline report could not be read."""


def read_report(path: Path) -> tuple[dict[str, Any], Summary]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        meta = cast(dict[str, Any], document["meta"])
        summary = Summary.from_dict(cast(dict[str, Any], document["summary"]))
    except OSError as err:
        raise ReportError(f"cannot read baseline {path}: {err.strerror}") from err
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        raise ReportError(f"baseline {path} is not an eval report: {err}") from err
    return meta, summary


def render(
    meta: Mapping[str, Any],
    summary: Summary,
    records: Sequence[RunRecord],
    baseline: Summary | None = None,
    baseline_meta: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        _header(meta),
        *_baseline_lines(meta, baseline_meta),
        "",
        _row("Metric", "Value", "Baseline", "Delta"),
        _row("-" * 30, "-" * 9, "-" * 9, "-" * 7),
    ]
    base = baseline

    def pct(name: str, value: float, before: float | None) -> None:
        lines.append(_row(name, _pct(value), _pct(before), _delta(value, before, percent=True)))

    def num(name: str, value: float, before: float | None, unit: str = "") -> None:
        lines.append(
            _row(name, _num(value, unit), _num(before, unit), _delta(value, before, percent=False))
        )

    pct("run pass rate", summary.pass_rate, base.pass_rate if base else None)
    consistent = summary.consistent_cases / len(summary.case_rates) if summary.case_rates else 0.0
    base_consistent = (
        base.consistent_cases / len(base.case_rates) if base and base.case_rates else None
    )
    pct("cases passing every run", consistent, base_consistent)
    for name, rate in summary.check_rates.items():
        pct(f"  check: {name}", rate, base.check_rates.get(name) if base else None)
    for tag, rate in summary.tag_rates.items():
        pct(f"  tag: {tag}", rate, base.tag_rates.get(tag) if base else None)
    num("avg steps", summary.avg_steps, base.avg_steps if base else None)
    num("avg input tokens", summary.avg_input_tokens, base.avg_input_tokens if base else None)
    num("avg output tokens", summary.avg_output_tokens, base.avg_output_tokens if base else None)
    num(
        "avg latency",
        summary.avg_latency_seconds,
        base.avg_latency_seconds if base else None,
        "s",
    )
    if summary.errors:
        lines += ["", "Errors: " + ", ".join(f"{k} x{v}" for k, v in summary.errors.items())]
    if meta.get("warnings"):
        lines += ["", "Library warnings (may affect the results):"]
        lines += [f"  {warning}" for warning in meta["warnings"]]

    failures = [record for record in records if not record.passed]
    if failures:
        lines += ["", "Failed runs:"]
        lines += [f"  {_failure(record)}" for record in failures]
    if base:
        lines += _case_changes(summary, base)
    return "\n".join(lines)


def _header(meta: Mapping[str, Any]) -> str:
    # .get: reports written by older versions of the harness may lack newer fields.
    def field(key: str) -> Any:
        return meta.get(key, "?")

    header = (
        f"Dataset {field('dataset')} v{field('dataset_version')} ({field('dataset_fingerprint')})"
        f" · {field('provider')} / {field('model')} · temperature {field('temperature')}"
        f" · {field('cases')} cases x {field('repeats')} runs"
        f" · system prompt {field('system_prompt_fingerprint')}"
    )
    if meta.get("git_commit"):
        dirty = " (uncommitted changes)" if meta.get("git_dirty") else ""
        header += f" · commit {str(meta['git_commit'])[:7]}{dirty}"
    if meta.get("regraded_from"):
        header += f" · regraded from v{meta['regraded_from'].get('dataset_version', '?')}"
    return header


def _baseline_lines(meta: Mapping[str, Any], base: Mapping[str, Any] | None) -> list[str]:
    if base is None:
        return []
    lines = [
        f"Baseline: {base.get('dataset')} v{base.get('dataset_version')}"
        f" ({base.get('dataset_fingerprint')}) · {base.get('provider')} / {base.get('model')}"
        f" · system prompt {base.get('system_prompt_fingerprint')}"
        f" · {base.get('started_at', '?')}"
    ]
    if base.get("dataset_fingerprint") != meta.get("dataset_fingerprint"):
        # The deltas would mix two changes: the system's behaviour and the scoring rules.
        lines.append(
            "WARNING: the baseline was graded with a different dataset; deltas mix behaviour "
            "changes with expectation changes. Regrade it first: python -m evals regrade <report>"
        )
    return lines


def _case_changes(summary: Summary, base: Summary) -> list[str]:
    changes = [
        f"  {case}: {_pct(base.case_rates[case])} -> {_pct(rate)}"
        for case, rate in summary.case_rates.items()
        if case in base.case_rates and base.case_rates[case] != rate
    ]
    return ["", "Changed cases vs baseline:", *changes] if changes else []


def _failure(record: RunRecord) -> str:
    where = f"{record.case_id} #{record.repeat}"
    if record.error:
        return f"{where}: error {record.error}"
    failed = "; ".join(f"{c.name}: {c.detail}" for c in record.checks if not c.passed)
    trace = record.trajectory.trace_id if record.trajectory else None
    return f"{where}: {failed}" + (f" (trace {trace})" if trace else "")


def _record_to_dict(record: RunRecord) -> dict[str, Any]:
    trajectory = record.trajectory
    return {
        "case": record.case_id,
        "repeat": record.repeat,
        "passed": record.passed,
        "error": record.error,
        "latency_seconds": round(record.latency_seconds, 3),
        "checks": [asdict(check) for check in record.checks],
        "trajectory": None
        if trajectory is None
        else {
            "prompt": trajectory.prompt,
            # Each call keeps its result: needed to re-grade checks such as grounded ids.
            "calls": [asdict(call) for call in trajectory.calls],
            "answer": trajectory.answer,
            "stop_reason": trajectory.stop_reason,
            "steps": trajectory.steps,
            "input_tokens": trajectory.input_tokens,
            "output_tokens": trajectory.output_tokens,
            "trace_id": trajectory.trace_id,
        },
    }


def _rate(results: Sequence[bool]) -> float:
    return sum(results) / len(results) if results else 0.0


def _mean(values: Sequence[float]) -> float:
    return fmean(values) if values else 0.0


def _row(name: str, value: str, before: str, delta: str) -> str:
    return f"{name:<30} {value:>9} {before:>9} {delta:>7}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:.1f}{unit}"


def _delta(value: float, before: float | None, *, percent: bool) -> str:
    if before is None:
        return ""
    diff = (value - before) * (100 if percent else 1)
    return f"{diff:+.1f}"
