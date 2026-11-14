"""Command line: ``python -m evals run --provider ollama --repeats 3``.

Uses the same settings (.env) and composition root as the service, so the agent under test
is the agent you deploy. Each run is a normal traced run: its trace id is in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from config.log_setup import configure_logging
from config.settings import Settings, get_settings
from config.telemetry import setup_telemetry
from evals.dataset import Dataset, DatasetError, load_dataset
from evals.report import ReportError, read_report, render, summarise, write_report
from evals.runner import Agent, AgentFactory, EvalAbortedError, EvalRunner, RunRecord
from evals.stubs import StubbingToolGatewayProvider
from main import build_chat_models, build_run_agent, build_tool_provider
from src.application.ports import ChatModel
from src.infrastructure.llm.resilience import RateLimitedChatModel

DEFAULT_DATASET = Path("evals/datasets/tool_use.toml")
DEFAULT_OUTPUT_DIR = Path("evals/results")

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_USAGE = 2


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


def parse_args(argv: Sequence[str]) -> EvalOptions:
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
    args = parser.parse_args(list(argv))
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
    )


async def run_evals(
    options: EvalOptions,
    dataset: Dataset,
    agent_for: AgentFactory,
    meta: Mapping[str, Any],
    out: TextIO,
) -> int:
    """Run, report and decide the exit code. Separate from ``main`` so it can be tested."""
    baseline = read_report(options.baseline)[1] if options.baseline else None
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
    print(render(full_meta, summary, records, baseline), file=out)
    print(f"\nReport: {path}", file=out)
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
    }


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
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
        return asyncio.run(run_evals(options, dataset, agent_for, meta, out))
    except (EvalAbortedError, ReportError) as err:
        print(f"Eval aborted: {err}", file=out)
        return EXIT_USAGE
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
