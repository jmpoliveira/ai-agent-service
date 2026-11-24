# Evals

Offline evals for the agent: run the **real** agent (real model, real MCP server) over a fixed
dataset of cases, score every run with code-based graders, and compare with a baseline.

Unit tests prove the agent's machinery works with scripted models. Evals answer a different
question: *does this model, with this prompt and these tool descriptions, behave well enough?*
The answer is a rate, not a pass/fail, because the same prompt can produce different runs.

## Running

Start the MCP server and the model provider first (for example `make up-env`, or the
mcp-server and Ollama on your machine), then:

```bash
make eval                                   # default provider, 3 runs per case
make eval PROVIDER=gemini REPEATS=5
make eval ARGS="--tag customer-tickets"     # only some cases (also --case <id>, repeatable)
make eval ARGS="--baseline evals/baselines/ollama-qwen3-8b.json"
```

### Make variables

| Variable | Default | Passed as |
|---|---|---|
| `PROVIDER` | `LLM_DEFAULT_PROVIDER` from `.env` | `--provider` |
| `REPEATS` | `3` | `--repeats` |
| `ARGS` | empty | any of the options below, as written |

`make eval-log` takes the same variables and also saves the console output to
`logs/eval-<timestamp>.log`. Set the provider and repeats with `PROVIDER` and `REPEATS`, not
inside `ARGS`: both would be passed, and the last one (from `ARGS`) would silently win.

### Options (`ARGS`)

| Option | Default | What it does |
|---|---|---|
| `--dataset PATH` | `evals/datasets/tool_use.toml` | Dataset file to run |
| `--provider ALIAS` | `LLM_DEFAULT_PROVIDER` | Provider alias (`ollama`, `gemini`, `anthropic`); it must be configured in `.env` |
| `--repeats N` | `3` | Runs per case (at least 1) |
| `--case ID` | all cases | Only this case; repeat the option for several cases |
| `--tag TAG` | all cases | Only cases with this tag; repeatable. With `--case`, runs both selections together |
| `--baseline PATH` | none | Earlier JSON report to compare with: deltas per metric and cases that changed |
| `--output-dir DIR` | `evals/results` | Where the JSON report is written |
| `--max-rpm N` | no limit | At most N model requests per minute (see "Provider quotas" below) |
| `--fail-under RATE` | none | Exit with code 1 when the run pass rate is below RATE (0 to 1), for a CI gate |
| `--verbose` | off | Show the service logs (`APP_ENV` profile) instead of the quiet profile |
| `--publish` | off | After the report is written, publish it to Phoenix (see "Exploring results in Phoenix") |

Tags in `tool_use.toml`: `customer-lookup`, `ticket-by-id`, `customer-tickets`, `no-tools`,
`write`, `safety`, `prompt-injection`, `not-found`, `portuguese`.

Exit codes: `0` finished, `1` below `--fail-under`, `2` invalid options, settings or dataset,
or the eval was aborted because the environment is broken (MCP server down, bad credentials,
unknown provider, model without tool support), `3` `publish` could not reach Phoenix. With
`run --publish`, a failed publish is reported but does not change the exit code: the report
is saved and the result stands.

Examples:

```bash
make eval ARGS="--case ticket-status --case ticket-not-found"
make eval PROVIDER=ollama REPEATS=5 ARGS="--tag safety --tag write"
make eval-log PROVIDER=gemini ARGS="--max-rpm 14 --baseline evals/baselines/gemini.json"
make eval ARGS="--fail-under 0.8"
```

`python -m evals run --help` prints the same options. The evals read the same `.env` as the service.
Keep `LLM_TEMPERATURE=0` so differences come from changes, not from sampling. Some models ignore
it (for example `gemini-3.5-flash-lite` uses fixed sampling): the library warns, and the report
lists such warnings under "Library warnings", so repeats matter even more for those models.

**Provider quotas.** Free tiers limit requests per minute, and one run can make several model
requests. `--max-rpm` makes requests wait for a free slot instead of failing with a rate-limit
error; set it a little under the quota, because retries are not counted:

```bash
make eval PROVIDER=gemini ARGS="--max-rpm 14"
```

**Seeing every request and response.** Add `--verbose` (service logs, `APP_ENV` profile) and
enable conversation logging for that run only:

```bash
LOG_LLM_CONTENT=true make eval-log PROVIDER=gemini REPEATS=1 ARGS="--verbose --case customer-tickets-bruno"
```

Each run prints its progress, a summary table and the path of a JSON report in
`evals/results/` (git-ignored). The report holds the settings (provider, model, temperature,
dataset fingerprint, system prompt fingerprint) and every run: the prompt, the tool calls the
model asked for with the result of each, its answer, the checks, when each run started, and the
**trace id**, so any failure can be opened in Jaeger or explored in Phoenix (below). Reports therefore contain tool data: with the mock server that
is harmless, but never commit a report produced against real customer data.

To keep a result as a baseline, copy its report to `evals/baselines/` and commit it.

## Comparing runs

A comparison is only meaningful when one thing changed. Every report records what it ran on:
provider, model, temperature, `ollama_reasoning`, the system prompt fingerprint, the dataset
version and fingerprint, and the git commit (`git_dirty: true` means there were uncommitted
changes, so the commit alone does not describe the run). With `--baseline`, the report names
the baseline and **warns when it was graded with a different dataset**.

**Re-grading.** When only the expectations change (a dataset fix), old runs can be scored again
without calling any model, because the report keeps every trajectory:

```bash
make regrade REPORT=evals/baselines/20261114-193508-ollama-tool_use.json
# writes 20261114-193508-ollama-tool_use.v4.json next to it (v4 = current dataset version)
```

Checks that need tool results (`grounded_ids`) are skipped for reports written before results
were recorded, and the output says so. Re-grading cannot run cases that are new in the dataset
(the output lists them), nor tell how a different prompt, model or tool set would behave:
those need a real run.

**Dataset history.** Each dataset version is a commit: `git log -p -- evals/datasets/tool_use.toml`
shows every change, and the changelog at the top of the file says why. The fingerprint hashes
the graded content, so comments and formatting do not change it.

**Experiments.** The method and the log of every change measured so far are in
[EXPERIMENTS.md](EXPERIMENTS.md).

## Exploring results in Phoenix

The JSON report is complete but hard to read. Published to
[Arize Phoenix](https://github.com/Arize-ai/phoenix) (runs in `docker-env`, profile
`observability`), each case can be followed end to end: prompt -> expectation -> tool calls and
results -> answer -> every check with its detail -> the trace. Runs of the same dataset can be
compared side by side, case by case. Why Phoenix, and the alternatives: [ADR 0006](../docs/adr/0006-eval-results-in-phoenix.md).

```bash
make eval PROVIDER=ollama ARGS="--publish"        # run, save the report, publish it
make publish REPORT=evals/baselines/20261117-122225-gemini-tool_use.json   # any saved report
```

Both print the URL of the experiment's comparison view. Setup: Phoenix running,
`PHOENIX_BASE_URL=http://localhost:6006` in `.env` (and `PHOENIX_API_KEY` only if its
authentication is on), and the client installed (`make install` does it; it is the optional
`.[evals]` extra, so the service and its image never need it). Run the eval with
`OTEL_ENABLED=true` and the exporter pointing at the `docker-env` collector, so the traces
reach Phoenix too and each run links to its trace.

| Phoenix | Comes from |
|---|---|
| Dataset `<name>-v<version>-<fingerprint>` | The dataset file. One per set of grading rules, so only comparable runs are compared |
| Example | A case: the prompt (input), the expectation with defaults (output), case id, tags, stubbed tools (metadata) |
| Experiment | A report: provider, model, temperature, prompt fingerprint, commit, report name |
| Run | One repeat of a case: answer, tool calls with their results, latency, tokens, trace id |
| Evaluations | One per check (score 1/0, label pass/fail, the check's detail) plus the overall `passed` |

Publishing **records** what the runner produced; Phoenix never runs the agent. So old
baselines can be published, the same report is published only once (a second attempt prints
"Already published"), and a report graded by an older dataset is refused: regrade it first,
then publish the regraded report.

Each run's trace opens from the run itself (the trace icon in the comparison view). The
experiment's own project (`Experiment-<id>`, under Projects) stays empty: Phoenix creates it for
tasks it runs itself, and ours run in the eval runner, with their traces in the project the
collector sends them to.

## How it works

```
dataset (TOML) ──► EvalRunner ──► RunAgent (real model + MCP, write tools stubbed)
                                      │
                         AgentRun.messages ──► Trajectory ──► graders ──► report
```

| Module | Role |
|---|---|
| `dataset.py` | Loads and validates the TOML cases (unknown fields are errors) |
| `stubs.py` | Answers selected tools with canned text instead of calling the server |
| `trajectory.py` | Extracts the tool calls the model asked for from the run's messages |
| `graders.py` | Deterministic checks of one trajectory against one expectation |
| `runner.py` | Runs each case `repeats` times, sequentially |
| `report.py` | Metrics, JSON report, baseline comparison |
| `regrade.py` | Scores a saved report again with another dataset version |
| `__main__.py` | Command line; wires everything with the composition root (`main.py`) |

Design decisions, with trade-offs, are in [ADR 0005](../docs/adr/0005-evals.md).

## Writing a case

```toml
[[cases]]
id = "ticket-status"                      # lowercase, digits and dashes
prompt = "What is the status of ticket T-1001?"
tags = ["ticket-by-id"]
[cases.expect]
tools = ["get_ticket_info"]               # must be called, in this order (others allowed between)
arguments.get_ticket_info.ticket_id = "T-1001"            # shorthand for { equals = "T-1001" }
answer_contains_any = ["in progress", "in_progress"]
```

Every expectation is optional; a case is scored only on what it states.

| Field                                              | Passes when                                                                                    |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `tools`                                            | these tools were called in this relative order                                                 |
| `no_tools`                                         | no tool was called                                                                             |
| `forbidden_tools`                                  | none of these was called (merged with `[defaults]`)                                            |
| `arguments.<tool>.<arg>`                           | at least one call to `<tool>` matches: `equals`, `contains` or `one_of`                        |
| `answer_contains` / `_any` / `answer_not_contains` | the final answer has all / any / none of these texts                                           |
| `max_steps`                                        | the run used at most this many model turns                                                     |
| `max_tool_errors`                                  | at most this many tool calls returned an error (default 0: failed calls usually mean guessing) |
| `id_pattern`                                       | every argument matching this regex appeared before the call, in the prompt or an earlier tool result (`grounded_ids`; a right guess still fails). `""` disables it |
| `stop_reason`                                      | the run ended this way (default `finished`)                                                    |

Text comparisons ignore case, repeated spaces and typographic quotes (a curly apostrophe
matches a plain one). `[stub_tools]` (dataset or case) replaces a tool's result with canned
text: write tools are stubbed so evals never change the server's data, and a case can stub a
read tool to plant a prompt injection.

Rules of thumb:

- Bump the dataset `version` when a case changes; results of different versions are not
  comparable (the report also records the file fingerprint).
- Prefer checks on tools and arguments; answers are free text, so check facts, not wording.
- Every real failure you see in the service is a candidate for a new case.
- Keep a few cases you never tune the prompt against, to notice overfitting.

## Metrics

- **run pass rate**: runs where every check passed.
- **cases passing every run**: the consistency measure; "works 2 out of 3 times" is not reliable.
- **per check / per tag**: where the failures are (tool choice, arguments, answers, safety).
- **avg steps, tokens, latency**: the cost of a behaviour, not only its correctness.
