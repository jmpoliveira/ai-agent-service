"""Command line: ``python -m evals run --provider ollama --repeats 3``,
``python -m evals regrade <report>`` to score a saved report again with the current dataset, and
``python -m evals publish <report>`` to explore a report in Phoenix (ADR 0006).

Uses the same settings (.env) and composition root as the service, so the agent under test
is the agent you deploy. Each run is a normal traced run: its trace id is in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from config.log_setup import configure_logging
from config.settings import Settings, get_phoenix_settings, get_settings
from config.telemetry import setup_telemetry
from evals.dataset import Dataset, DatasetError, load_dataset
from evals.regrade import regrade
from evals.report import (
    ReportError,
    load_saved_report,
    read_report,
    render,
    summarise,
    write_report,
)
from evals.runner import Agent, AgentFactory, EvalAbortedError, EvalRunner, RunRecord
from evals.sink import EvalResults, SinkError, SinkFactory
from evals.stubs import StubbingToolGatewayProvider
from main import build_chat_models, build_run_agent, build_tool_provider
from src.application.ports import ChatModel
from src.infrastructure.llm.resilience import RateLimitedChatModel

DEFAULT_DATASET = Path("evals/datasets/tool_use.toml")
DEFAULT_OUTPUT_DIR = Path("evals/results")

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_USAGE = 2
EXIT_PUBLISH_FAILED = 3


@dataclass(frozen=True, slots=True)
class EvalOptions:
    dataset: Path
    provider: str | None
    repeats: int
    cases: frozenset[str]
    tags: frozenset[str]
    baseline: Path | None
    output_dir: Path
    fail_under: float | None
    verbose: bool
    max_rpm: int | None = None
    publish: bool = False


@dataclass(frozen=True, slots=True)
class PublishOptions:
    report: Path
    dataset: Path


@dataclass(frozen=True, slots=True)
class RegradeOptions:
    report: Path
    dataset: Path
    output: Path | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a dataset against the agent")
    run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--provider", help="provider alias; defaults to LLM_DEFAULT_PROVIDER")
    run.add_argument("--repeats", type=int, default=3, help="runs per case (default 3)")
    run.add_argument("--case", action="append", default=[], help="only this case id")
    run.add_argument("--tag", action="append", default=[], help="only cases with this tag")
    run.add_argument("--baseline", type=Path, help="earlier report to compare with")
    run.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    run.add_argument(
        "--fail-under", type=float, help="exit 1 if the run pass rate is below this (0-1)"
    )
    run.add_argument("--verbose", action="store_true", help="show the service logs")
    run.add_argument(
        "--max-rpm",
        type=int,
        help="at most this many model requests per minute (stay under a provider quota)",
    )
    run.add_argument(
        "--publish",
        action="store_true",
        help="also publish the report to Phoenix (PHOENIX_BASE_URL), see `publish`",
    )
    again = commands.add_parser(
        "regrade", help="score a saved report again with the dataset (no model calls)"
    )
    again.add_argument("report", type=Path, help="report JSON written by `run`")
    again.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    again.add_argument(
        "--output", type=Path, help="where to write it (default: next to the report, .v<N>.json)"
    )
    share = commands.add_parser(
        "publish", help="publish a saved report to Phoenix to explore it case by case"
    )
    share.add_argument("report", type=Path, help="report JSON written by `run` or `regrade`")
    share.add_argument(
        "--dataset", type=Path, default=DEFAULT_DATASET, help="the dataset that graded it"
    )
    return parser


def parse_args(argv: Sequence[str]) -> EvalOptions:
    parser = _parser()
    args = parser.parse_args(list(argv))
    if args.command != "run":
        parser.error("expected the run command")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.max_rpm is not None and args.max_rpm < 1:
        parser.error("--max-rpm must be at least 1")
    if args.fail_under is not None and not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be between 0 and 1")
    return EvalOptions(
        dataset=args.dataset,
        provider=args.provider,
        repeats=args.repeats,
        cases=frozenset(args.case),
        tags=frozenset(args.tag),
        baseline=args.baseline,
        output_dir=args.output_dir,
        fail_under=args.fail_under,
        verbose=args.verbose,
        max_rpm=args.max_rpm,
        publish=args.publish,
    )


def parse_publish_args(argv: Sequence[str]) -> PublishOptions:
    parser = _parser()
    args = parser.parse_args(list(argv))
    if args.command != "publish":
        parser.error("expected the publish command")
    return PublishOptions(report=args.report, dataset=args.dataset)


def parse_regrade_args(argv: Sequence[str]) -> RegradeOptions:
    parser = _parser()
    args = parser.parse_args(list(argv))
    if args.command != "regrade":
        parser.error("expected the regrade command")
    return RegradeOptions(report=args.report, dataset=args.dataset, output=args.output)


def regrade_report(options: RegradeOptions, out: TextIO) -> int:
    """Score a saved report with the current dataset. Needs no settings, model or server."""
    try:
        dataset = load_dataset(options.dataset)
        document = json.loads(options.report.read_text(encoding="utf-8"))
        result = regrade(document, dataset, options.report.name)
    except (DatasetError, ReportError) as err:
        print(str(err), file=out)
        return EXIT_USAGE
    except OSError as err:
        print(f"cannot read report {options.report}: {err.strerror}", file=out)
        return EXIT_USAGE
    except json.JSONDecodeError as err:
        print(f"{options.report} is not an eval report: {err}", file=out)
        return EXIT_USAGE

    # Strip a previous ".vN" so regrading a regraded report does not stack suffixes.
    stem = re.sub(r"\.v\d+$", "", options.report.stem)
    path = options.output or options.report.with_name(f"{stem}.v{dataset.version}.json")
    summary = summarise(result.records)
    write_report(path, result.meta, summary, result.records)
    for note in result.notes:
        print(f"Note: {note}", file=out)
    print(render(result.meta, summary, result.records), file=out)
    print(f"\nRegraded report: {path}", file=out)
    return EXIT_OK


def phoenix_sink_factory() -> SinkFactory:
    """How to open the Phoenix sink, checked before any work is done.

    Imported here and not at the top: the Phoenix client is an optional dependency (the
    ``evals`` extra), and ``run`` and ``regrade`` must work without it.
    """
    try:
        settings = get_phoenix_settings()
    except ValidationError as err:
        raise SinkError(f"invalid Phoenix settings (check your .env):\n{err}") from err
    if settings.base_url is None:
        raise SinkError("PHOENIX_BASE_URL is not set: there is nowhere to publish to")
    try:
        from evals.phoenix_sink import open_phoenix_sink
    except ImportError as err:
        raise SinkError(
            f"the Phoenix client is not installed ({err.name}): pip install -e '.[evals]'"
        ) from err
    return lambda: open_phoenix_sink(settings)


async def publish_results(results: EvalResults, open_sink: SinkFactory, out: TextIO) -> bool:
    """Publish one report. A failure is reported, never raised: the report is already saved."""
    try:
        async with open_sink() as sink:
            published = await sink.publish(results)
    except SinkError as err:
        print(f"Publishing failed: {err}", file=out)
        return False
    verb = "Published" if published.created else "Already published"
    print(f"{verb}: {published.url}", file=out)
    return True


def publish_report(options: PublishOptions, open_sink: SinkFactory, out: TextIO) -> int:
    """Publish a saved report. Needs no model, MCP server or service settings."""
    try:
        dataset = load_dataset(options.dataset)
        saved = load_saved_report(options.report)
    except (DatasetError, ReportError) as err:
        print(str(err), file=out)
        return EXIT_USAGE
    graded_with = saved.meta.get("dataset_fingerprint")
    if graded_with != dataset.fingerprint:
        # Phoenix would show these runs next to the dataset's current expectations, which did
        # not grade them. Regrading first makes the checks and the expectations agree.
        print(
            f"{options.report} was graded with dataset fingerprint {graded_with}, but "
            f"{options.dataset} is {dataset.fingerprint}. Regrade it first "
            f"(python -m evals regrade {options.report}) and publish the regraded report.",
            file=out,
        )
        return EXIT_USAGE
    tags = {case.id: case.tags for case in dataset.cases}
    records = [replace(r, tags=tags.get(r.case_id, ())) for r in saved.records]
    results = EvalResults(dataset, saved.meta, records, options.report.name)
    published = asyncio.run(publish_results(results, open_sink, out))
    return EXIT_OK if published else EXIT_PUBLISH_FAILED


async def run_evals(
    options: EvalOptions,
    dataset: Dataset,
    agent_for: AgentFactory,
    meta: Mapping[str, Any],
    out: TextIO,
    open_sink: SinkFactory | None = None,
) -> int:
    """Run, report and decide the exit code. Separate from ``main`` so it can be tested."""
    baseline_meta, baseline = read_report(options.baseline) if options.baseline else (None, None)
    cases = dataset.select(set(options.cases), set(options.tags))
    if not cases:
        print("No cases match the selection.", file=out)
        return EXIT_USAGE

    total = len(cases) * options.repeats
    done = 0

    def progress(record: RunRecord) -> None:
        nonlocal done
        done += 1
        status = "ERROR" if record.error else ("pass" if record.passed else "FAIL")
        print(
            f"[{done}/{total}] {record.case_id} #{record.repeat} {status} "
            f"{record.latency_seconds:.1f}s",
            file=out,
            flush=True,
        )

    started = datetime.now(UTC)
    runner = EvalRunner(
        dataset, agent_for, provider=options.provider, repeats=options.repeats, on_record=progress
    )
    # Library warnings (for example "temperature will be ignored") repeat on every request;
    # collect them once and put them in the report, where they explain the results.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        records = await runner.run(cases)
    summary = summarise(records)
    library_warnings = sorted({str(warning.message) for warning in caught})

    full_meta = {
        **meta,
        "cases": len(cases),
        "repeats": options.repeats,
        "started_at": started.isoformat(timespec="seconds"),
        "duration_seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
        "warnings": library_warnings,
    }
    stamp = started.strftime("%Y%m%d-%H%M%S")
    path = options.output_dir / f"{stamp}-{meta['provider']}-{dataset.name}.json"
    write_report(path, full_meta, summary, records)

    print("", file=out)
    print(render(full_meta, summary, records, baseline, baseline_meta), file=out)
    print(f"\nReport: {path}", file=out)
    if options.publish and open_sink is not None:
        results = EvalResults(dataset, full_meta, records, path.name)
        if not await publish_results(results, open_sink, out):
            # The eval result stands; publishing is only a view of it.
            print(f"Publish it later with: python -m evals publish {path}", file=out)
    if options.fail_under is not None and summary.pass_rate < options.fail_under:
        print(f"Run pass rate is below --fail-under {options.fail_under:.2f}", file=out)
        return EXIT_BELOW_THRESHOLD
    return EXIT_OK


def build_meta(settings: Settings, dataset: Dataset, provider: str, model: str) -> dict[str, Any]:
    return {
        "dataset": dataset.name,
        "dataset_version": dataset.version,
        "dataset_fingerprint": dataset.fingerprint,
        "provider": provider,
        "model": model,
        "temperature": settings.llm_temperature,
        "ollama_reasoning": settings.ollama_reasoning,
        # Prompts are not versioned files yet; the hash ties a result to the exact prompt text.
        "system_prompt_fingerprint": hashlib.sha256(
            settings.agent_system_prompt.encode("utf-8")
        ).hexdigest()[:12],
        **git_revision(),
    }


def git_revision(cwd: Path | None = None) -> dict[str, Any]:
    """The commit the eval ran on, so any result can be traced back to the exact code, prompt
    and dataset (``git show <commit>:evals/datasets/tool_use.toml``). ``git_dirty`` is true
    when there were uncommitted changes, i.e. the commit alone does not describe the run."""
    unknown: dict[str, Any] = {"git_commit": None, "git_dirty": None}
    git = shutil.which("git")
    if git is None:
        return unknown
    try:
        # Fixed arguments, no shell: nothing user-controlled reaches the command line.
        commit = subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return unknown
    return {"git_commit": commit, "git_dirty": bool(status.strip())}


def _publish_command(arguments: Sequence[str], out: TextIO) -> int:
    options = parse_publish_args(arguments)
    try:
        open_sink = phoenix_sink_factory()
    except SinkError as err:
        print(f"Cannot publish: {err}", file=out)
        return EXIT_USAGE
    return publish_report(options, open_sink, out)


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["regrade"]:
        return regrade_report(parse_regrade_args(arguments), out)
    if arguments[:1] == ["publish"]:
        return _publish_command(arguments, out)
    options = parse_args(arguments)
    open_sink: SinkFactory | None = None
    if options.publish:
        # Fail before a long run, not after it.
        try:
            open_sink = phoenix_sink_factory()
        except SinkError as err:
            print(f"Cannot publish: {err}", file=out)
            return EXIT_USAGE
    try:
        settings = get_settings()
    except ValidationError as err:
        print(f"Invalid settings (check your .env):\n{err}", file=out)
        return EXIT_USAGE
    # The quiet "test" profile keeps the console for the eval output unless --verbose.
    configure_logging(settings.app_env if options.verbose else "test")
    try:
        dataset = load_dataset(options.dataset)
    except DatasetError as err:
        print(str(err), file=out)
        return EXIT_USAGE

    models: dict[str, ChatModel] = dict(build_chat_models(settings))
    provider = options.provider or settings.default_provider
    if provider not in models:
        print(f"Provider '{provider}' is not configured; enabled: {sorted(models)}", file=out)
        return EXIT_USAGE
    if options.max_rpm is not None:
        # One limiter per model, shared by every case: the quota is per provider, not per run.
        models = {
            alias: RateLimitedChatModel(model, options.max_rpm) for alias, model in models.items()
        }
    tools = build_tool_provider(settings)

    def agent_for(stubs: Mapping[str, str]) -> Agent:
        return build_run_agent(
            settings, models=models, tools=StubbingToolGatewayProvider(tools, stubs)
        )

    meta = {
        **build_meta(settings, dataset, provider, models[provider].model),
        "max_rpm": options.max_rpm,
    }
    options = replace(options, provider=provider)  # the report always names the real provider
    shutdown_telemetry = setup_telemetry(settings)
    try:
        return asyncio.run(run_evals(options, dataset, agent_for, meta, out, open_sink))
    except (EvalAbortedError, ReportError) as err:
        print(f"Eval aborted: {err}", file=out)
        return EXIT_USAGE
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
