# ADR 0001: Clean Architecture with an explicit agent loop

Status: accepted

## Context

An agent is not a request/response service. It runs a non-deterministic loop (model turn, tool
calls, model turn...) that must always terminate, call a remote tool server, and stay observable.
Plain layering says where code lives but nothing about how that control flow is expressed.

## Decision

1. **Clean Architecture for dependency direction**: `domain` <- `application` <- `infrastructure` /
   `interface`, with `main.py` as the composition root. `tests/test_architecture.py` enforces it.
2. **The agent loop is an explicit use case** (`RunAgent`) in the application layer, with its
   stop conditions modelled as data (`AgentBudget`, `StopReason`): max steps, token budget,
   time budget, repeated identical tool call, truncated output.
3. **Ports** for what changes independently: `ChatModel`, `ToolGateway` (+ `ToolGatewayProvider`
   so each run opens its own session and the service stays stateless), `HealthCheck`.
4. **`opentelemetry-api` is allowed in `application`.** The API/SDK split already makes it
   vendor-neutral; a tracing port would add code without adding independence. All GenAI
   attribute names are isolated in one module because those conventions are still in development.
5. **Trace context travels in MCP `params._meta`**, not in HTTP headers. The MCP client sends
   requests from a task created when the connection opens, so header propagation would parent
   every call to the same span. The client also calls `list_tools` explicitly, inside a span: the
   SDK would otherwise list tools implicitly on the first call, without `_meta`, creating an
   orphan trace.
6. **`mcp` is pinned `>=1.9,<2`**, the same major as the mcp-server project. Version 2 renames the
   client API (`streamable_http_client` takes an `httpx.AsyncClient`, `McpError` becomes
   `MCPError`, `call_tool` raises instead of returning `isError`). Upgrading is a deliberate task.

## Alternatives considered

- **Plain layered service** (what the previous project did): hides the loop, hard to test without a network.
- **LangGraph**: valuable for checkpoints and human-in-the-loop interrupts. Not needed yet, and it
  hides the mechanics this project exists to teach. Revisit when write tools need human
  confirmation or runs must be resumable. Because the loop sits behind `RunAgent.execute`, the
  swap would be local to the application layer.

## Consequences

- The loop is testable with a scripted fake model and a fake tool gateway, no network.
- Tools run sequentially inside a step. Parallel execution of read-only tools is a later optimisation.
- The one real-network exception in tests is a tiny in-process MCP server, which caught a real bug
  (domain errors wrapped in an `ExceptionGroup` by the SDK's task group).
