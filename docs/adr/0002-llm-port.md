# ADR 0002: One `ChatModel` port for Ollama, Gemini and Claude

Status: accepted

## Context

The three providers differ in tool-call format, content blocks, ids, usage reporting, finish
reasons and error types. The agent loop must not know any of that.

## Decision

- **The port speaks domain types** (`Message`, `ToolSpec`, `ToolCall`, `ChatResult`, `Usage`), never
  LangChain or vendor objects. A conversation can therefore be replayed on another provider.
- **Adapters sit on the official LangChain partner packages** (`langchain-ollama`,
  `langchain-google-genai`, `langchain-anthropic`, over `langchain-core`). They already normalise
  `bind_tools`, `tool_calls` and `usage_metadata`. One shared class (`LangChainChatModel`) does the
  conversion; each provider module only builds its client and lists the exceptions it handles.
  If LangChain quirks leak too much, one adapter at a time can move to the native SDK without
  touching the application layer.
- **Tool-call ids are guaranteed unique.** Missing or duplicated ids are replaced; the
  conversation stays valid for providers that match results to calls by id.
- **`ProviderState`** carries opaque assistant-turn data some providers need replayed verbatim
  (reasoning blocks, signatures). Only the provider that produced it reads it; any other provider
  rebuilds the turn from text and tool calls. This is what makes cross-provider fallback safe.
  Whether each provider needs it is **not verified against live APIs**; the mechanism is cheap insurance.
- **Errors are normalised** into `LLMUnavailableError`, `LLMRateLimitError`, `LLMAuthError`,
  `LLMInvalidRequestError`, `ToolCallingNotSupportedError`, `LLMResponseError`. Two families are
  translated: LangChain's `ModelError` hierarchy (Claude and Gemini integrations raise classes that
  are both the SDK error and a `ModelError`) and native errors by HTTP status. Upstream text is never
  copied into the domain error.
- **Retry and fallback are decorators** (`RetryingChatModel`, `FallbackChatModel`), written once and
  tested once. Only `LLMUnavailableError` and `LLMRateLimitError` are retried or fallen back on.
  Provider-side retries are disabled (`max_retries=0`) so attempts are not multiplied.
  The retry is hand-written (backoff with equal jitter, honours a short `Retry-After`) rather than
  using `tenacity`: `google-genai` pins `tenacity<9.2`, and 25 lines avoid a dependency conflict.
- **No streaming yet.** Streaming combined with tool calls is the hardest part of the problem and
  there is no consumer for it yet.

## Consequences

- Adding a provider = one factory module, one set of handled exceptions, one config block.
- Tool schemas from MCP are passed as JSON Schema; providers accept different subsets, so a
  schema one provider rejects surfaces as `LLMInvalidRequestError`. Verify each real tool set.
