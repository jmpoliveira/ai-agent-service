# ai-agent-service

[![CI](https://github.com/jmpoliveira/ai-agent-service/actions/workflows/ci.yml/badge.svg)](https://github.com/jmpoliveira/ai-agent-service/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A provider-agnostic AI agent service. It answers a prompt by looping between an LLM
(**Ollama**, **Gemini** or **Claude**) and the tools of an **MCP server**, and every step of
the loop is traced end to end with **OpenTelemetry** (viewable in Jaeger).

This repository is the long-lived home of the agent: RAG, new loop strategies, evals and
versioned prompts are added here over time. Anything with its own deployment lifecycle (the MCP
server, the RAG ingestion pipeline) lives in its own repository. See
[ADR 0004](docs/adr/0004-evolution-strategy.md).

```
 client ──POST /v1/agent/runs──► RunAgent ──► ChatModel (Ollama | Gemini | Claude)
                                    │
                                    └────────► ToolGateway ──MCP/HTTP──► mcp-server
```

## How a run works

1. The prompt is sanitised and the provider is resolved (explicit alias, or the configured default).
2. A fresh MCP session is opened and the tools are listed (once per run, inside the trace).
3. **The loop:** the model answers; if it asks for tools, each call is executed and its result
   (sanitised, size-capped and wrapped in `<tool_result>` delimiters) is sent back; repeat.
4. The loop always ends: final answer, `max_steps`, token budget, time budget, or the model
   repeating the same call. The response says which one (`stop_reason`).

A real run in Jaeger (`qwen3:8b` on Ollama, the companion mcp-server in Docker). One trace id
covers the HTTP request, the agent loop and the MCP server spans:

![Jaeger trace of one agent run: invoke_agent with tool.list, two chat calls to qwen3:8b and execute_tool search_customers, each linked to its mcp-server span](docs/images/jaeger-trace.png)

The same trace as text:

```
POST /v1/agent/runs                   (FastAPI HTTP span)
└── invoke_agent ai-agent-service
    ├── tool.list                     → tools/list                  (mcp-server)
    ├── chat qwen3:8b                 the model asks for a tool
    ├── execute_tool search_customers → tools/call search_customers (mcp-server)
    └── chat qwen3:8b                 the model writes the answer
```

In this run the two model calls take about 57 of the 57.3 seconds, while listing and calling the
tools takes about 40 milliseconds: model latency, not tool access, is what to optimise.

## Architecture

Clean Architecture, with the agent loop made explicit instead of hidden in a generic service.
The reasoning is in [docs/adr](docs/adr).

| Layer | Contents | May depend on |
|---|---|---|
| `src/domain` | Messages, tool calls, usage, budget, errors. Pure Python. | nothing |
| `src/application` | The `RunAgent` loop, ports (`ChatModel`, `ToolGateway`...), sanitisation, GenAI tracing helpers | domain (+ `opentelemetry-api`, a documented exception) |
| `src/infrastructure` | Ollama/Gemini/Claude adapters, retry and fallback, MCP client, health probes | domain, application |
| `src/interface` | FastAPI routes, schemas, auth, error mapping | domain, application |
| `config/` | Settings (`pydantic-settings`), logging profiles (`logging.json`), OpenTelemetry bootstrap | nothing in `src` |
| `main.py` | **Composition root**: the only place that wires everything | everything |

`tests/test_architecture.py` fails the build if a layer imports something it should not.

## Quick start

Requirements: Python 3.12+, a running MCP server (for example the companion
[mcp-server](https://github.com/jmpoliveira/mcp-server), which serves mock customer and
ticket tools), and at least one LLM provider.

```bash
make install
cp .env.example .env     # then edit: MCP_SERVER_URL, and the provider(s) you use
make run
```

For local models, use one that supports **tool calling** (the example uses `qwen3:8b`):

```bash
ollama pull qwen3:8b
```

Ask the agent something:

```bash
curl -sS -X POST http://localhost:8000/v1/agent/runs \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Which open tickets does the customer Bruno Costa have?", "provider": "ollama"}'
```

```json
{
  "answer": "...",
  "stop_reason": "finished",
  "steps": 2,
  "provider": "ollama",
  "model": "qwen3:8b",
  "usage": {"input_tokens": 812, "output_tokens": 96},
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

Paste `trace_id` into the Jaeger search to see the run. If `API_KEY` is set, add
`-H "X-API-Key: $API_KEY"`. Interactive docs are at `/docs` (disabled in production).

## Configuration

Everything is configured through environment variables (see [.env.example](.env.example)). Key points:

- A provider is **enabled only when all its variables are set** (`OLLAMA_BASE_URL` + `OLLAMA_MODEL`,
  `GEMINI_API_KEY` + `GEMINI_MODEL`, `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL`). A half-configured
  provider is a startup error, not a silent skip.
- **No model names, hosts or URLs are hard-coded.** Model ids are yours to choose.
- `LLM_DEFAULT_PROVIDER` is required when more than one provider is enabled.
- `LLM_FALLBACKS={"anthropic": ["ollama"]}` makes a provider fall back to others when it is
  down or rate limited (never on authentication or validation errors, which would hide a bug).
- `AGENT_TOOL_ALLOWLIST` limits which MCP tools the model can see and call.
- `APP_ENV` (`development`, `test`, `production`) selects the logging profile; `production` also
  requires `API_KEY` and hides `/docs`. `LOG_LEVEL` optionally overrides the profile's level.
- `LOG_LLM_CONTENT=true` (debugging only) logs the prompt, every model reply (text and tool calls
  with arguments) and every tool result at DEBUG, clipped to `LOG_LLM_CONTENT_MAX_CHARS`. Use it
  with the `development` profile; it is refused in production because the content may hold
  personal data. Save a run locally with `make run-log`.

## Choosing the model

The **caller** chooses the provider (the `provider` field); the agent never picks its own model.
See [ADR 0003](docs/adr/0003-model-selection.md) for why, and for the planned deterministic router.

## Security notes

- Prompts and tool results are stripped of control, zero-width and bidi-override characters.
- Tool results are untrusted: they are size-capped, framed, and the system prompt tells the
  model not to follow instructions inside them. **This reduces prompt injection; it does not remove it.**
- The model can only call tools that were offered to it; invented tool names never reach the server.
- Provider and tool-server error details stay in the logs; clients get fixed messages.
- Secrets are `SecretStr`, never logged, and `.env` is git-ignored.
- Tool arguments and results are not recorded in spans, and conversation content is not logged
  unless `LOG_LLM_CONTENT` is enabled (never in production).

## Development

```bash
make check        # ruff + pyright (strict) + pytest
make test-unit    # fast unit tests only
make cov          # coverage (minimum enforced in pyproject.toml)
make run-log      # run locally and save the logs to logs/agent-<timestamp>.log
```

CI runs the checks on Python 3.12, 3.13 and 3.14; the Docker image uses 3.13.

Integration tests start a small real MCP server **in-process** on a local port; no external
service is needed, and no LLM is called (models are scripted fakes).

## Evals

Tests prove the machinery; **evals measure the behaviour of a real model**: does it choose the
right tool, with the right arguments, answer from the results, and stay away from write tools
it was not asked to use?

```bash
make eval                               # needs the MCP server and the provider running
make eval PROVIDER=gemini ARGS="--tag customer-tickets"
```

The dataset ([evals/datasets/tool_use.toml](evals/datasets/tool_use.toml)) has 22 cases over the
mcp-server mock data: customer lookup, tickets by id, tickets of a customer, no-tool questions,
a write, and safety cases including a prompt injection planted in a tool result. Each case runs
several times; the report gives pass rates per check and per tag, cost and latency, the trace id
of every run, and a comparison with a baseline. Write tools are stubbed, so evals never change
the server's data. How to run and extend them: [evals/README.md](evals/README.md); design:
[ADR 0005](docs/adr/0005-evals.md).

## Docker

Two ways to run the container:

| Command | Compose files | Reaches Ollama, MCP server, Jaeger at |
|---|---|---|
| `make up` | `docker-compose.yml` (profile `standalone`) | its own Ollama (pulls `OLLAMA_MODEL`) and Jaeger; the MCP server on the host |
| `make up-env` | `docker-compose.yml` + `docker-compose.docker-env.yml` | service names on the shared `docker-env-net` network, tracing on |

```bash
make up-env       # start docker-env and the mcp-server first
make health
```

Set `MCP_SERVER_URL` in `docker-compose.docker-env.yml` to your mcp-server container name. The
image writes only to stdout, so it runs with a read-only filesystem. `APP_ENV` picks the logging
profile (`APP_ENV=production make up-env` gives JSON lines).

## Status and known limits

Verified by automated tests: the loop and its stop conditions, sanitisation, error mapping,
retry and fallback, MCP listing/calling and `traceparent` propagation against a real in-process
server, the HTTP API, settings validation, and the dependency rule.

**Not verified automatically** (needs your environment): calls to real Ollama/Gemini/Claude
endpoints, the Docker build and run, the OTLP export to Jaeger, and whether each model family
produces good tool calls (measure it with `make eval`).

Not implemented yet: the roadmap below, apart from the evals of step 1. Until rate limiting and
request-body size limits exist, put a reverse proxy in front of the service.

## Roadmap

The order follows one rule: **measure first, then protect, then add features.** Each step is a
tag (`v0.2`, `v0.3`...) with its ADRs. Where each capability lives follows
[ADR 0004](docs/adr/0004-evolution-strategy.md). `v0.1.0` (the agent loop, three providers, MCP
tools, tracing) is done.

| # | Capability | What | Why at this point |
|---|---|---|---|
| 1 | **Evals and tool design** | Harness, dataset and first baselines done (Ollama, Gemini; see [EXPERIMENTS.md](evals/EXPERIMENTS.md)); next: Claude baseline, then `search_tickets` in the mcp-server measured against them | Every later change is measured against this |
| 2 | **Azure OpenAI provider** | A fourth `ChatModel` adapter, usable in `LLM_FALLBACKS` and measured in the eval baselines | The most common enterprise AI platform; the port makes it a small, contained change |
| 3 | **RAG** | Retrieval from the RAG services (their own repositories, with their own deployment lifecycle) exposed as a `search_knowledge_base` MCP tool; retrieval and answer evals (recall@k, faithfulness with an LLM judge) | Retrieval is a read-only tool on the existing path, so no architecture change. Documents are untrusted input: results go through the same sanitisation, and the injection eval cases cover them |
| 4 | **Guardrails and security I** | Human confirmation for write tools (MCP elicitation), per-tool permissions, input/output guardrails (injection, PII), rate limiting, request size limits; checked against the OWASP Top 10 for LLM applications | The agent can already change data, and the safety eval cases show where models fail |
| 5 | **LLM observability and cost** | Cost, latency and tokens per run as OpenTelemetry metrics; the existing spans exported to Langfuse over OTLP (no SDK); provider prompt caching, measured | Caching, routing and budgets need these numbers |
| 6 | **Stateful loop and context memory** | A LangGraph loop strategy as a new use case next to `RunAgent`, behind the same ports and chosen by config; checkpointed sessions in a store (the service stays stateless); human-in-the-loop interrupts reusing step 4. Demo use case: an AI-assisted architecture impact assessment, with domain tools in the mcp-server | Resumable runs and confirmations are the point where LangGraph pays off ([ADR 0001](docs/adr/0001-architecture.md)); the evals compare it with the explicit loop. Memory stores untrusted text (memory poisoning), so it comes after guardrails |
| 7 | **Caching** | Read-only tool results -> embeddings -> semantic cache last | Ordered from safest to riskiest; a semantic cache can return a wrong answer for a similar question |
| 8 | **Streaming and structured outputs** | SSE responses, typed final answers | Needs a consumer to be worth its complexity |
| 9 | **Advanced loop patterns** | Parallel read-only tool calls, plan-and-execute, reflection | Only once evals show the current loops are the limit |
| 10 | **Security II** | OAuth 2.1 towards MCP, a secrets manager, tenant isolation, cost budgets | Needed before real multi-user use |
| 11 | **Deterministic model routing** | Rules by task type, size or cost (see [ADR 0003](docs/adr/0003-model-selection.md)) | Rules come from eval data |
| 12 | **Ecosystem** | MCP resources and prompts, an MCP gateway preserving `_meta`, multi-agent / A2A | Builds on everything above |
| 13 | **Production** | Job queue for long runs, evals as a CI gate, Kubernetes | Operating at scale |
