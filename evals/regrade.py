"""Re-grade a saved report with another version of the dataset, without calling any model.

A report keeps every trajectory (tool calls, answer, steps, tokens), so when only the
*expectations* change (a dataset fix, a stricter check) the old runs can be scored again under
the new rules. That turns an old baseline into one that is comparable with new runs, for free.

What it cannot do: run cases that did not exist when the report was made, or show how the model
would behave with a different prompt, model or tool set. Those need a real run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from evals.dataset import Dataset
from evals.graders import grade
from evals.report import ReportError
from evals.runner import RunRecord
from evals.trajectory import trajectory_from_report


@dataclass(frozen=True, slots=True)
class Regraded:
    meta: dict[str, Any]
    records: list[RunRecord]
    # Human-readable notes: cases dropped or missing because the dataset changed.
    notes: list[str]


def regrade(document: Mapping[str, Any], dataset: Dataset, source: str) -> Regraded:
    try:
        meta = cast(dict[str, Any], document["meta"])
        runs = cast(list[dict[str, Any]], document["runs"])
        provider, model = str(meta["provider"]), str(meta["model"])
    except (KeyError, TypeError) as err:
        raise ReportError(f"{source} is not an eval report: {err}") from err

    cases = {case.id: case for case in dataset.cases}
    records: list[RunRecord] = []
    dropped: set[str] = set()
    for run in runs:
        try:
            case = cases.get(run["case"])
            if case is None:
                dropped.add(str(run["case"]))
                continue
            saved = run["trajectory"]
            if saved is None:  # the run failed before finishing: nothing to grade
                records.append(
                    RunRecord(
                        case_id=case.id,
                        repeat=int(run["repeat"]),
                        tags=case.tags,
                        checks=(),
                        trajectory=None,
                        latency_seconds=float(run["latency_seconds"]),
                        error=run["error"],
                    )
                )
                continue
            trajectory = trajectory_from_report(saved, provider, model)
            records.append(
                RunRecord(
                    case_id=case.id,
                    repeat=int(run["repeat"]),
                    tags=case.tags,
                    checks=tuple(grade(dataset.expectation_for(case), trajectory)),
                    trajectory=trajectory,
                    latency_seconds=float(run["latency_seconds"]),
                )
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ReportError(f"{source} has a malformed run: {err}") from err

    graded = {record.case_id for record in records}
    notes = [
        f"case '{case}' is not in dataset v{dataset.version}: dropped" for case in sorted(dropped)
    ]
    notes += [
        f"case '{case.id}' has no runs in this report (new in the dataset): needs a real run"
        for case in dataset.cases
        if case.id not in graded
    ]
    new_meta = {
        **meta,
        "dataset": dataset.name,
        "dataset_version": dataset.version,
        "dataset_fingerprint": dataset.fingerprint,
        "cases": len(graded),
        "regraded_from": {
            "report": source,
            "dataset_version": meta.get("dataset_version"),
            "dataset_fingerprint": meta.get("dataset_fingerprint"),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }
    return Regraded(new_meta, records, notes)
