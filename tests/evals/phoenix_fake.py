"""An in-memory stand-in for the Phoenix REST API endpoints the sink uses.

The real ``phoenix.client.AsyncClient`` talks to it through ``httpx.MockTransport``, so the
tests exercise the client's actual requests and response parsing, not a mock of our own code.
Shapes follow arize-phoenix-client 3.7 (``phoenix.client.__generated__.v1``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

NOW = "2026-11-17T12:00:00+00:00"


@dataclass
class FakePhoenix:
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    experiments: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    runs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    evaluations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    uploads: int = 0
    # Set to make every request fail with this status (server error, auth...).
    fail_with: int | None = None

    def client(self, base_url: str = "http://phoenix.test:6006/") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"detail": "boom"})
        path = request.url.path.strip("/").split("/")
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        route: dict[tuple[str, ...], Callable[[], httpx.Response]] = {
            ("GET", "arize_phoenix_version"): lambda: httpx.Response(200, text="21.0.0"),
            ("GET", "v1", "datasets"): self._list_datasets,
            ("POST", "v1", "datasets", "upload"): lambda: self._upload(body),
            ("POST", "v1", "experiment_evaluations"): lambda: self._evaluate(body),
        }
        key = (request.method, *path)
        if key in route:
            return route[key]()
        match request.method, path:
            case "GET", ["v1", "datasets", dataset_id]:
                return _data(self.datasets[dataset_id])
            case "GET", ["v1", "datasets", dataset_id, "examples"]:
                return _data(
                    {
                        "dataset_id": dataset_id,
                        "version_id": f"{dataset_id}-v1",
                        "examples": self.examples[dataset_id],
                    }
                )
            case "GET", ["v1", "datasets", dataset_id, "experiments"]:
                found = [e for e in self.experiments if e["dataset_id"] == dataset_id]
                return httpx.Response(200, json={"data": found, "next_cursor": None})
            case "POST", ["v1", "datasets", dataset_id, "experiments"]:
                return self._create_experiment(dataset_id, body)
            case "POST", ["v1", "experiments", experiment_id, "runs"]:
                run_id = f"run-{len(self.runs) + 1}"
                self.runs.append({**body, "id": run_id, "experiment_id": experiment_id})
                return _data({"id": run_id})
            case _:
                return httpx.Response(404, json={"detail": f"no route {request.method} {path}"})

    def _list_datasets(self) -> httpx.Response:
        return httpx.Response(200, json={"data": list(self.datasets.values()), "next_cursor": None})

    def _upload(self, body: dict[str, Any]) -> httpx.Response:
        self.uploads += 1
        dataset_id = f"ds-{len(self.datasets) + 1}"
        self.datasets[dataset_id] = {
            "id": dataset_id,
            "name": body["name"],
            "description": body.get("description"),
            "metadata": {},
            "created_at": NOW,
            "updated_at": NOW,
            "example_count": len(body["inputs"]),
        }
        self.examples[dataset_id] = [
            {
                "id": f"{dataset_id}-ex-{i}",
                "node_id": f"{dataset_id}-ex-{i}",
                "input": body["inputs"][i],
                "output": body["outputs"][i],
                "metadata": body["metadata"][i],
                "updated_at": NOW,
            }
            for i in range(len(body["inputs"]))
        ]
        return _data({"dataset_id": dataset_id, "version_id": f"{dataset_id}-v1"})

    def _create_experiment(self, dataset_id: str, body: dict[str, Any]) -> httpx.Response:
        experiment = {
            "id": f"exp-{len(self.experiments) + 1}",
            "dataset_id": dataset_id,
            "dataset_version_id": body.get("version_id", f"{dataset_id}-v1"),
            "name": body.get("name", ""),
            "description": body.get("description"),
            "repetitions": body["repetitions"],
            "metadata": body.get("metadata", {}),
            "project_name": None,
            "created_at": NOW,
            "updated_at": NOW,
            "example_count": len(self.examples[dataset_id]),
            "successful_run_count": 0,
            "failed_run_count": 0,
            "missing_run_count": 0,
        }
        self.experiments.append(experiment)
        return _data(experiment)

    def _evaluate(self, body: dict[str, Any]) -> httpx.Response:
        self.evaluations.append(body)
        return _data({"id": f"eval-{len(self.evaluations)}"})

    def evaluations_for(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {e["name"]: e for e in self.evaluations if e["experiment_run_id"] == run_id}


def _data(payload: object) -> httpx.Response:
    return httpx.Response(200, json={"data": payload})
