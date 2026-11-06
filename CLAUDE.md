# CLAUDE.md

Guidance for Claude Code and other AI assistants working in this repository.

## What this is

An AI agent service: `RunAgent` loops between an LLM (Ollama, Gemini or Claude, behind one
`ChatModel` port) and an MCP server (behind the `ToolGateway` port). Read `README.md` and
`docs/adr/` before changing the architecture.

This repository grows over time (RAG, loop strategies, evals, versioned prompts); follow
ADR 0004: new capabilities go behind existing ports (RAG retrieval is a tool), and anything
with its own deployment lifecycle belongs in a separate repository. The ordered list of
capabilities to add (evals, guardrails, observability, RAG, memory, caching...) is the Roadmap
section of `README.md`; keep that order unless there is a reason to change it.

## Commands

```bash
make install      # create .venv and install with dev dependencies
make check        # ruff + pyright (strict) + pytest. Run before saying a task is done.
make test-unit    # fast tests, no local server
make cov          # coverage, fails under the minimum in pyproject.toml
```

## Language and style

- All code, docstrings, comments, identifiers, test names, commit and PR text: **English**.
- Python 3.12+, full type hints (pyright strict), `async/await` for all I/O, context managers
  for resources, `pathlib` for paths, ruff for lint and format.

## Architecture rules (enforced by `tests/test_architecture.py`)

- `domain` -> nothing. `application` -> domain (+ `opentelemetry-api`). `infrastructure` -> domain,
  application. `interface` -> domain, application. `config/` never imports `src`.
- `main.py` is the **composition root**: it is the only module that builds and wires concrete classes.
- Ports (Protocols) live in `src/application/ports.py`; adapters in `src/infrastructure`.
- Services are stateless. Per-run state lives inside `RunAgent.execute`.
- GenAI span and attribute names live **only** in `src/application/genai_telemetry.py`.

## Configuration

- All configuration is in `config/settings.py` (`pydantic-settings`). Never hard-code hosts,
  ports, URLs or model names. Update `.env.example` whenever a setting is added.
- Secrets are `SecretStr`. Never log them, never commit `.env`, never ask for them in chat.

## Dependencies

- Official, stable libraries only: no pre-releases, no `langchain-community` or
  `langchain-experimental`. Pin the **major** version. Check the current stable version
  (PyPI) before adding or bumping a dependency; do not rely on memory.
- `mcp` is pinned `<2`: 2.x renames the client API (`streamablehttp_client`, `McpError`,
  `isError`...). Upgrade deliberately, not through a bot PR.

## Errors, security and logging

- Validate inputs before use. Catch **specific** exceptions; no bare `except:`; no unlogged
  `except Exception:`; chain with `raise ... from err`.
- Domain errors carry safe messages; upstream details stay in logs, not in HTTP responses.
- Text that enters a prompt (user input, tool results) goes through `application/sanitization.py`.
- Standard `logging` only, configured by `config/logging.json` (a `base` plus one profile per
  `APP_ENV`; JSON lines in production). Every line carries `trace_id` and `span_id`; stdout only.
  Conversation content (prompts, replies, tool arguments and results) is logged only by
  `application/conversation_log.py`, at DEBUG, when `LOG_LLM_CONTENT` is on; never log it elsewhere.
  Secret redaction matches exact key names (`_SENSITIVE_KEYS`), not substrings.
- Anything crossing an `async with` of the MCP SDK may arrive wrapped in an `ExceptionGroup`;
  `McpToolGatewayProvider.open()` unwraps it. Keep integration tests covering that.

## Tests

- `pytest`, mirroring `src/` under `tests/`, Arrange-Act-Assert, runnable from the repo root.
- Mock external services (Ollama, Gemini, Claude, MCP). The only real server in tests is the
  tiny in-process FastMCP one in `tests/integration`.
- Cover edge cases: empty input, oversized input, malformed provider output, missing settings.
- Do not commit or push unless asked.
