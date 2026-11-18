# ADR 0006: Explore eval results in Arize Phoenix, published from the saved reports

Status: accepted

## Context

ADR 0005 gave us offline evals whose JSON reports are complete (every run, its checks, its
trajectory and its trace id) but hard to *read*. To understand one failed case end to end
(what was asked, what was expected, which tools the model called with which arguments, what
came back, which check failed and why, and the trace with its latency and tokens) we had to
cross a JSON file with Jaeger by hand. Comparing two runs case by case (qwen3 against Gemini,
or before and after a tool description change) was harder still.

Jaeger already plays this role for traces. We needed the same for evals: a UI that links
**dataset -> case -> run -> checks -> trace**, and compares runs side by side.

The shared `docker-env` routes every trace through an OpenTelemetry Collector, so a new trace
backend is one more exporter there, with no change in this service.

## Decision

1. **Arize Phoenix, self-hosted in `docker-env`.** One container (SQLite volume) in the
   `observability` profile. The collector fans traces out to Jaeger and Phoenix; the service
   still only knows `OTEL_EXPORTER_ENDPOINT`. Phoenix converts the `gen_ai.*` attributes we
   emit to its own conventions, so no re-instrumentation. Its datasets and experiments map
   almost one to one onto our dataset, runner and graders.
2. **Publish, don't delegate.** Phoenix's usual flow (`run_experiment`) makes Phoenix call a
   task per example. We keep our runner (repeats, stubs, rate limits, abort rules) and
   *publish* its results through the official client's `create` / `log_run` /
   `log_evaluation`. Any saved report can be published, including the existing baselines,
   and an eval never depends on Phoenix being up.
3. **The JSON report stays the source of truth.** It is written first, committed as a
   baseline and re-gradable (ADR 0005). Phoenix is a view of it: a failed publish never
   changes an eval's result or exit code.
4. **Only comparable runs share a Phoenix dataset.** The dataset is named
   `<name>-v<version>-<fingerprint>`, so Phoenix's comparison view never puts runs graded by
   different rules side by side (the same rule as `--baseline`). Publishing a report graded by
   another dataset is refused: regrade it first. Publishing the same report twice is detected
   (the report name is stored on the experiment) and does nothing.
5. **Mapping.** Case -> example (input: prompt; output: the expectation with defaults applied;
   metadata: case id, tags, stubbed tools). Report -> experiment (provider, model, prompt
   fingerprint, commit...). Run -> experiment run (answer, tool calls and results, trace id).
   Check -> code evaluation (score 1/0, label, the check's detail), plus an overall `passed`.
6. **Behind a port.** `evals/sink.py` defines `ResultSink`; `evals/phoenix_sink.py` is the
   only module allowed to import the Phoenix client (enforced by `tests/test_architecture.py`).
   The client is an optional extra (`.[evals]`): neither the service nor its Docker image
   installs it, and `run` and `regrade` work without it.
7. **Explicit configuration.** `PHOENIX_BASE_URL` and an optional `PHOENIX_API_KEY` in a
   separate `PhoenixSettings`, read only by the evals. The HTTP client is built from them, so
   Phoenix's own environment variables and config files cannot redirect it.

## Alternatives considered

- **Langfuse (self-hosted).** Equally good datasets and experiments, plus prompt management,
  but six containers (Postgres, ClickHouse, Redis, MinIO, web, worker) for a laptop
  environment. Still an option for production-style LLM observability (roadmap step 5).
- **LangSmith.** Strongest with LangChain/LangGraph, but closed source and self-hosting is
  Enterprise only: traces would leave the machine. Our loop is our own, so its main advantage
  does not apply.
- **A static HTML report generated from the JSON.** No infrastructure, but we would rebuild
  filtering, run comparison and trace links by hand: a product, not a feature.
- **Azure AI Foundry evaluations + Application Insights.** The managed equivalent, and common
  in enterprises. Designed around cloud-run evaluators (LLM judges); several pieces are in
  preview; needs a subscription and costs per GB ingested. Kept as a **second sink later**: an
  Azure Monitor exporter in the collector plus a `ResultSink` implementation, with no change to
  the runner or the service.

## Consequences

- `make eval ARGS="--publish"` or `make publish REPORT=<path>` shows a run in Phoenix; the
  printed URL opens the experiment's comparison view.
- Phoenix is licensed under the Elastic License 2.0 (source-available, not OSI open source):
  fine for local and internal use, not for offering Phoenix itself as a service. The client
  library is Apache-2.0.
- Authentication is disabled in the local `docker-env` (Phoenix 21 enables it by default in
  Docker), as for Jaeger. Anywhere else: enable it, set `PHOENIX_API_KEY`, and give the
  collector's Phoenix exporter an `Authorization` header.
- Reports now record when each run started (`started_at`), to place runs on a timeline.
  Older reports get start times estimated from the eval's start and the latencies (runs are
  sequential).
- Traces appear in Phoenix only when `OTEL_ENABLED=true` during the eval and the collector
  exports to Phoenix; without them the experiment is still complete, just without trace
  links.
- A run links to its trace by trace id, in any Phoenix project (checked in the Phoenix 21.0.0
  server source: the trace is looked up by id with no project filter), so traces that arrive
  through the collector in the `default` project are found. The run's tokens and cost come from
  that trace. Runs whose traces never reached Phoenix (for example, evals run before it was
  added to `docker-env`) show the results and checks, but no trace, tokens or cost.
- Phoenix also creates a project per experiment (`Experiment-<id>`) for the spans of tasks it
  runs itself. With publishing it stays empty, by design: our traces live in the project the
  collector sends them to, and each run reaches its trace through the run's trace link.
