from __future__ import annotations

import asyncio
import io
import json
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import evals.__main__ as cli
from config.settings import Settings
from evals.__main__ import EXIT_BELOW_THRESHOLD, EXIT_OK, EXIT_USAGE, parse_args, run_evals
from evals.runner import Agent
from src.domain.messages import ToolCall
from src.infrastructure.llm.resilience import RateLimitedChatModel
from tests.evals.helpers import case, dataset, make_agent
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
