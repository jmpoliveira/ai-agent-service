# ADR 0003: The caller chooses the model; the agent does not

Status: accepted

## Decision

The provider is an **input**: an alias (`ollama`, `gemini`, `anthropic`) validated against the
configured providers, defaulting to `LLM_DEFAULT_PROVIDER`. Raw model names are not accepted from
callers; model ids live in configuration.

The agent **does not** choose its own model.

## Reasons

1. **Evals.** Comparing models requires holding everything else constant. If routing varies inside
   a run, a failure cannot be attributed to a model.
2. **Observability.** `gen_ai.request.model` is constant for a run; the model that actually answered
   is reported separately (`gen_ai.response.model`, and in the response body).
3. **Cost and safety.** Free-form model names are a cost vector. A model that can select its own
   (more expensive) model, or be steered to by text in a tool result, is a prompt-injection target.
4. **Circularity.** Picking a model needs a model; the router can cost more than it saves.

## What exists instead

- **Availability fallback** (`LLM_FALLBACKS`): a deterministic chain used only when a provider is
  unreachable or rate limited. It is logged, recorded as a span event, and never used for
  authentication or validation errors (a bad key must not be masked).

## Later

A deterministic `ModelRouter` port (by task type, estimated size, or cost) can be added once evals
provide data to justify its rules. It stays outside the agent loop, as policy, not as a model decision.
