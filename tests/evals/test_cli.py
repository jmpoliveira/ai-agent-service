from __future__ import annotations

import asyncio
import io
import json
import sys
import warnings
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

import evals.__main__ as cli
from config.settings import PhoenixSettings, Settings
from evals.__main__ import (
    EXIT_BELOW_THRESHOLD,
    EXIT_OK,
    EXIT_PUBLISH_FAILED,
    EXIT_USAGE,
    parse_args,
    run_evals,
)
from evals.dataset import load_dataset
from evals.runner import Agent
from evals.sink import EvalResults, Published, ResultSink, SinkError, SinkFactory
from src.domain.messages import ToolCall
from src.infrastructure.llm.resilience import RateLimitedChatModel
from tests.evals.helpers import case, dataset, make_agent
from tests.evals.phoenix_fake import FakePhoenix
from tests.fakes import reply

META: dict[str, Any] = {
    "dataset": "test",
    "dataset_version": 1,
    "dataset_fingerprint": "abc",
    "provider": "fake",
    "model": "fake-model",
    "temperature": 0.0,
    "system_prompt_fingerprint": "def",
}
SEARCH = ToolCall("c1", "search_customers", {"query": "alice"})


def _agent_for(_: Mapping[str, str]) -> Agent:
    return make_agent([reply(tool_calls=[SEARCH]), reply("Alice is C-001.")])


def test_parse_args_defaults_and_validation():
    options = parse_args(["run"])

    assert options.repeats == 3
    assert options.dataset == Path("evals/datasets/tool_use.toml")
    with pytest.raises(SystemExit):
        parse_args(["run", "--repeats", "0"])
    with pytest.raises(SystemExit):
        parse_args(["run", "--fail-under", "1.5"])
    with pytest.raises(SystemExit):
        parse_args(["run", "--max-rpm", "0"])
    assert parse_args(["run", "--max-rpm", "14"]).max_rpm == 14


def test_run_evals_writes_a_report_and_compares_with_a_baseline(tmp_path: Path):
    # Arrange
    data = dataset(case("a", expect={"tools": ["search_customers"], "answer_contains": ["C-001"]}))
    out = io.StringIO()
    options = parse_args(["run", "--repeats", "1", "--output-dir", str(tmp_path)])

    # Act: a first run, then a second one compared with the first.
    assert asyncio.run(run_evals(options, data, _agent_for, META, out)) == EXIT_OK
    [first] = list(tmp_path.glob("*.json"))
    options = parse_args(
        ["run", "--repeats", "1", "--output-dir", str(tmp_path / "2"), "--baseline", str(first)]
    )
    code = asyncio.run(run_evals(options, data, _agent_for, META, out))

    # Assert
    assert code == EXIT_OK
    text = out.getvalue()
    assert "[1/1] a #1 pass" in text
    assert "100.0%" in text
    assert "Report:" in text
    report = json.loads(first.read_text(encoding="utf-8"))
    assert report["meta"]["cases"] == 1
    assert first.name.endswith("-fake-test.json")


async def test_fail_under_and_empty_selection_set_the_exit_code(tmp_path: Path):
    data = dataset(case("a", expect={"answer_contains": ["never"]}))
    out = io.StringIO()

    below = parse_args(
        ["run", "--repeats", "1", "--fail-under", "0.5", "--output-dir", str(tmp_path)]
    )
    empty = parse_args(["run", "--tag", "nothing", "--output-dir", str(tmp_path)])

    assert await run_evals(below, data, _agent_for, META, out) == EXIT_BELOW_THRESHOLD
    assert await run_evals(empty, data, _agent_for, META, out) == EXIT_USAGE
    assert "No cases match the selection." in out.getvalue()


def _no_logging(*_: object) -> None:
    """Keep the test runner's log handlers in place."""


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mcp_server_url": "http://mcp.test:3000/mcp",
        "ollama_base_url": "http://ollama.test:11434",
        "ollama_model": "local-model",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def test_main_reports_a_bad_dataset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "configure_logging", _no_logging)
    out = io.StringIO()

    code = cli.main(["run", "--dataset", str(tmp_path / "missing.toml")], out)

    assert code == EXIT_USAGE
    assert "cannot read dataset" in out.getvalue()


def test_main_rejects_a_provider_that_is_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "configure_logging", _no_logging)
    out = io.StringIO()

    code = cli.main(["run", "--provider", "anthropic"], out)

    assert code == EXIT_USAGE
    assert "Provider 'anthropic' is not configured; enabled: ['ollama']" in out.getvalue()


def test_main_reports_invalid_settings(monkeypatch: pytest.MonkeyPatch):
    def broken() -> Settings:
        return _settings(mcp_server_url="not a url")

    monkeypatch.setattr(cli, "get_settings", broken)
    out = io.StringIO()

    assert cli.main(["run"], out) == EXIT_USAGE
    assert "Invalid settings" in out.getvalue()


def test_main_runs_end_to_end_with_the_real_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange: real settings and wiring; only the agent factory output is faked.
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "configure_logging", _no_logging)
    built: list[dict[str, Any]] = []

    def fake_build_run_agent(settings: Settings, **kwargs: Any) -> Agent:
        built.append(kwargs)
        warnings.warn("temperature will be ignored", UserWarning, stacklevel=1)
        return make_agent([reply("Paris")], alias="ollama")

    monkeypatch.setattr(cli, "build_run_agent", fake_build_run_agent)
    data = tmp_path / "d.toml"
    data.write_text(
        'name = "d"\nversion = 1\n[stub_tools]\ncreate_ticket = "{}"\n'
        '[[cases]]\nid = "capital"\nprompt = "Capital of France?"\n'
        '[cases.expect]\nno_tools = true\nanswer_contains = ["Paris"]\n',
        encoding="utf-8",
    )
    out = io.StringIO()

    # Act
    args = ["run", "--dataset", str(data), "--repeats", "1", "--output-dir", str(tmp_path)]
    code = cli.main([*args, "--max-rpm", "14"], out)

    # Assert
    assert code == EXIT_OK, out.getvalue()
    assert "ollama / local-model" in out.getvalue()
    assert set(built[0]) == {"models", "tools"}
    assert all(isinstance(m, RateLimitedChatModel) for m in built[0]["models"].values())
    [report] = tmp_path.glob("*.json")
    meta = json.loads(report.read_text(encoding="utf-8"))["meta"]
    assert meta["max_rpm"] == 14
    assert meta["warnings"] == ["temperature will be ignored"]
    assert "Library warnings (may affect the results):" in out.getvalue()


def _write_report(path: Path, answer: str) -> None:
    run: dict[str, Any] = {
        "case": "missing",
        "repeat": 1,
        "passed": False,
        "error": None,
        "latency_seconds": 1.0,
        "checks": [],
        "trajectory": {
            "calls": [],
            "answer": answer,
            "stop_reason": "finished",
            "steps": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "trace_id": None,
        },
    }
    meta = {**META, "dataset_version": 1, "dataset_fingerprint": "old"}
    path.write_text(json.dumps({"meta": meta, "summary": {}, "runs": [run]}), encoding="utf-8")


def test_regrade_writes_a_versioned_report_next_to_the_original(tmp_path: Path):
    # Arrange: a v1 report, then a v3 dataset that accepts the answer.
    report = tmp_path / "20261114-ollama-tool_use.v1.json"
    _write_report(report, "It could not be found.")
    data = tmp_path / "d.toml"
    data.write_text(
        'name = "test"\nversion = 3\n[[cases]]\nid = "missing"\nprompt = "?"\n'
        '[cases.expect]\nanswer_contains_any = ["not be found"]\n',
        encoding="utf-8",
    )
    out = io.StringIO()

    # Act: no settings, model or server needed.
    code = cli.main(["regrade", str(report), "--dataset", str(data)], out)

    # Assert
    assert code == EXIT_OK, out.getvalue()
    regraded = tmp_path / "20261114-ollama-tool_use.v3.json"  # ".v1" replaced, not stacked
    document = json.loads(regraded.read_text(encoding="utf-8"))
    assert document["meta"]["dataset_version"] == 3
    assert document["runs"][0]["passed"] is True
    assert "100.0%" in out.getvalue()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot read report"),
        ("not json", "not an eval report"),
        ("{}", "not an eval report"),
    ],
)
def test_regrade_reports_unreadable_input(tmp_path: Path, content: str | None, message: str):
    report = tmp_path / "r.json"
    if content is not None:
        report.write_text(content, encoding="utf-8")
    out = io.StringIO()

    code = cli.main(["regrade", str(report)], out)

    assert code == EXIT_USAGE
    assert message in out.getvalue()


def test_each_parser_only_accepts_its_own_command():
    with pytest.raises(SystemExit):
        parse_args(["regrade", "r.json"])
    with pytest.raises(SystemExit):
        cli.parse_regrade_args(["run"])
    with pytest.raises(SystemExit):
        cli.parse_publish_args(["run"])
    assert cli.parse_publish_args(["publish", "r.json"]).dataset == Path(
        "evals/datasets/tool_use.toml"
    )


def _no_program(_: str) -> None:
    return None


def test_git_revision_describes_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Not a repository: unknown, not a crash.
    assert cli.git_revision(tmp_path) == {"git_commit": None, "git_dirty": None}
    # This checkout (CI and local runs are git repositories).
    revision = cli.git_revision(Path(__file__).resolve().parents[2])
    if revision["git_commit"] is not None:
        assert len(revision["git_commit"]) == 40
        assert isinstance(revision["git_dirty"], bool)
    # No git installed.
    monkeypatch.setattr(cli.shutil, "which", _no_program)
    assert cli.git_revision() == {"git_commit": None, "git_dirty": None}


# ---------------------------------------------------------------------------------------
# Publishing (ADR 0006)
# ---------------------------------------------------------------------------------------
DATASET_TOML = (
    'name = "d"\nversion = 1\n'
    '[[cases]]\nid = "capital"\nprompt = "Capital of France?"\ntags = ["geo"]\n'
    '[cases.expect]\nno_tools = true\nanswer_contains = ["Paris"]\n'
)


@dataclass
class RecordingSink:
    published: list[EvalResults] = field(default_factory=list[EvalResults])
    fail: bool = False

    async def publish(self, results: EvalResults) -> Published:
        if self.fail:
            raise SinkError("phoenix is down")
        self.published.append(results)
        return Published("http://phoenix.test/datasets/1/compare", created=True)


def _open(sink: RecordingSink) -> SinkFactory:
    @asynccontextmanager
    async def open_sink() -> AsyncGenerator[ResultSink, None]:
        yield sink

    return open_sink


def _paris_agent(_: Mapping[str, str]) -> Agent:
    return make_agent([reply("Paris"), reply("Paris")])  # one reply per repeat


def _saved_report(tmp_path: Path) -> tuple[Path, Path]:
    """A real report written by run_evals, and the dataset file that graded it."""
    data_file = tmp_path / "d.toml"
    data_file.write_text(DATASET_TOML, encoding="utf-8")
    data = load_dataset(data_file)
    meta = {**META, "dataset": "d", "dataset_fingerprint": data.fingerprint}
    options = parse_args(["run", "--repeats", "2", "--output-dir", str(tmp_path / "out")])
    assert asyncio.run(run_evals(options, data, _paris_agent, meta, io.StringIO())) == EXIT_OK
    [report] = (tmp_path / "out").glob("*.json")
    return report, data_file


def test_publish_sends_the_saved_runs_with_the_dataset_tags(tmp_path: Path):
    # Arrange
    report, data_file = _saved_report(tmp_path)
    sink = RecordingSink()
    out = io.StringIO()
    options = cli.parse_publish_args(["publish", str(report), "--dataset", str(data_file)])

    # Act
    code = cli.publish_report(options, _open(sink), out)

    # Assert
    assert code == EXIT_OK, out.getvalue()
    assert "Published: http://phoenix.test/datasets/1/compare" in out.getvalue()
    [results] = sink.published
    assert results.report_name == report.name
    assert [(r.case_id, r.repeat, r.tags, r.passed) for r in results.records] == [
        ("capital", 1, ("geo",), True),
        ("capital", 2, ("geo",), True),
    ]
    assert all(r.started_at is not None for r in results.records)


def test_publish_refuses_a_report_graded_by_another_dataset(tmp_path: Path):
    # Arrange: the dataset changed after the report was written.
    report, data_file = _saved_report(tmp_path)
    data_file.write_text(DATASET_TOML.replace('"Paris"', '"Paris", "France"'), encoding="utf-8")
    sink = RecordingSink()
    out = io.StringIO()
    options = cli.parse_publish_args(["publish", str(report), "--dataset", str(data_file)])

    # Act
    code = cli.publish_report(options, _open(sink), out)

    # Assert
    assert code == EXIT_USAGE
    assert "Regrade it first" in out.getvalue()
    assert sink.published == []


@pytest.mark.parametrize(
    ("report_content", "dataset_ok", "message"),
    [("not json", True, "not an eval report"), ("{}", False, "cannot read dataset")],
)
def test_publish_reports_unreadable_input(
    tmp_path: Path, report_content: str, dataset_ok: bool, message: str
):
    report = tmp_path / "r.json"
    report.write_text(report_content, encoding="utf-8")
    data_file = tmp_path / "d.toml"
    if dataset_ok:
        data_file.write_text(DATASET_TOML, encoding="utf-8")
    out = io.StringIO()
    options = cli.parse_publish_args(["publish", str(report), "--dataset", str(data_file)])

    assert cli.publish_report(options, _open(RecordingSink()), out) == EXIT_USAGE
    assert message in out.getvalue()


def test_a_failed_publish_has_its_own_exit_code(tmp_path: Path):
    report, data_file = _saved_report(tmp_path)
    out = io.StringIO()
    options = cli.parse_publish_args(["publish", str(report), "--dataset", str(data_file)])

    code = cli.publish_report(options, _open(RecordingSink(fail=True)), out)

    assert code == EXIT_PUBLISH_FAILED
    assert "Publishing failed: phoenix is down" in out.getvalue()


def test_run_with_publish_publishes_the_report_it_wrote(tmp_path: Path):
    data = dataset(case("a", expect={"answer_contains": ["Paris"]}))
    sink = RecordingSink()
    out = io.StringIO()
    options = parse_args(["run", "--repeats", "1", "--output-dir", str(tmp_path), "--publish"])

    code = asyncio.run(run_evals(options, data, _paris_agent, META, out, _open(sink)))

    assert code == EXIT_OK
    [report] = tmp_path.glob("*.json")
    assert [r.report_name for r in sink.published] == [report.name]
    assert sink.published[0].meta["repeats"] == 1


async def test_a_failed_publish_does_not_change_the_eval_result(tmp_path: Path):
    # The report is saved and the scores stand; publishing is only a view of them.
    data = dataset(case("a", expect={"answer_contains": ["Paris"]}))
    out = io.StringIO()
    options = parse_args(["run", "--repeats", "1", "--output-dir", str(tmp_path), "--publish"])

    code = await run_evals(options, data, _paris_agent, META, out, _open(RecordingSink(fail=True)))

    assert code == EXIT_OK
    assert "Publishing failed: phoenix is down" in out.getvalue()
    assert "Publish it later with: python -m evals publish" in out.getvalue()


def _phoenix(**values: Any) -> Any:
    def settings() -> PhoenixSettings:
        return PhoenixSettings(_env_file=None, **values)  # type: ignore[call-arg]

    return settings


@pytest.mark.parametrize("command", [["publish", "r.json"], ["run", "--publish"]])
def test_publishing_without_a_phoenix_url_fails_before_any_work(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
):
    # Arrange: a run would fail on these settings; it must never get that far.
    monkeypatch.setattr(cli, "get_phoenix_settings", _phoenix())
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "configure_logging", _no_logging)
    out = io.StringIO()

    # Act
    code = cli.main(command, out)

    # Assert
    assert code == EXIT_USAGE
    assert "Cannot publish: PHOENIX_BASE_URL is not set" in out.getvalue()


def test_the_sink_factory_explains_a_missing_extra_and_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
):
    # A missing optional dependency: importing the adapter fails.
    monkeypatch.setattr(cli, "get_phoenix_settings", _phoenix(base_url="http://phoenix.test"))
    monkeypatch.setitem(sys.modules, "evals.phoenix_sink", None)
    with pytest.raises(SinkError, match=r"pip install -e '\.\[evals\]'"):
        cli.phoenix_sink_factory()

    # A malformed URL in .env.
    monkeypatch.setattr(cli, "get_phoenix_settings", _phoenix(base_url="not a url"))
    with pytest.raises(SinkError, match="invalid Phoenix settings"):
        cli.phoenix_sink_factory()


def test_publish_end_to_end_through_the_real_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Arrange: main -> factory -> PhoenixSink -> phoenix.client -> HTTP -> fake Phoenix.
    report, data_file = _saved_report(tmp_path)
    fake = FakePhoenix()
    real_client = httpx.AsyncClient

    def client_on_fake(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(**kwargs, transport=httpx.MockTransport(fake.handle))

    monkeypatch.setattr("evals.phoenix_sink.httpx.AsyncClient", client_on_fake)
    monkeypatch.setattr(cli, "get_phoenix_settings", _phoenix(base_url="http://phoenix.test:6006"))
    out = io.StringIO()

    # Act
    code = cli.main(["publish", str(report), "--dataset", str(data_file)], out)
    again = cli.main(["publish", str(report), "--dataset", str(data_file)], out)

    # Assert
    assert (code, again) == (EXIT_OK, EXIT_OK), out.getvalue()
    assert "Published: http://phoenix.test:6006/datasets/ds-1/compare?experimentId=exp-1" in (
        out.getvalue()
    )
    assert "Already published" in out.getvalue()
    assert len(fake.runs) == 2
    assert {e["name"] for e in fake.evaluations} == {"no_tools", "answer", "stop_reason", "passed"}
