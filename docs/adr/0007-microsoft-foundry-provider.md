# ADR 0007: Microsoft Foundry through its OpenAI-compatible v1 endpoint

Status: accepted

## Context

Roadmap step 2 adds an Azure-hosted provider, the most common enterprise AI platform. Models
deployed in a Microsoft Foundry resource are reached through the Azure OpenAI **v1 API**
(`https://<resource>.openai.azure.com/openai/v1`, or the same path on `services.ai.azure.com`),
which is OpenAI-compatible and needs no `api-version`. The first model is `gpt-6-luna` (the
cheapest GPT-6 tier, with function calling). The deployment is **Global Standard**: requests may
be processed in any Azure region. Moving it to Data Zone Standard (EU processing) is planned in
the `azure-infra` repository (its ADR 0002), which manages the Foundry resources as code.
(Corrected 2026-11-26: this ADR first said Data Zone Standard; the portal deployment was Global.)

The agent's tasks are short tool-routing turns. The eval baselines show that small models already
pass them when the tool descriptions are good, so a high-volume, low-cost model is the right
first choice; a larger tier is a configuration change measured with `make eval`.

## Decision

1. **Client: `ChatOpenAI` from `langchain-openai`**, with `base_url` set to the v1 endpoint and
   the **deployment name** as `model`. A new adapter, `foundry_chat.py`, builds it; the shared
   `LangChainChatModel` does the rest, as for the other providers (ADR 0002). The provider alias
   is `foundry`, traced as `gen_ai.provider.name = azure.ai.openai`.
2. **Chat Completions, not the Responses API, for now.** The adapter's message conversion and the
   eval baselines are built on the Chat Completions shape, and Chat Completions carries no opaque
   state to replay (`preserve_provider_state=False`).
3. **`FOUNDRY_REASONING_EFFORT` defaults to `none`.** GPT-6 models reason by default (`medium`).
   With reasoning on, they reject `temperature`, and OpenAI reasoning models have rejected
   function tools combined with `reasoning_effort` on Chat Completions (seen on GPT-5.6). With
   `none`, temperature 0 is accepted, the tool loop works on Chat Completions, and each step is
   faster and cheaper. Any other value is allowed, and the adapter then **omits** the
   temperature. The effort is recorded in each eval report, because it changes the results.
4. **API key first, Microsoft Entra ID later.** The key is a `SecretStr` from the environment and
   is sent as a Bearer token, which the v1 API accepts.
5. **Fail fast on the wrong URL.** `FOUNDRY_BASE_URL` must end with `/openai/v1`. Pasting the
   project endpoint (`/api/projects/<name>`) or the resource root is the usual mistake, and would
   otherwise show up only as a 404 on every request.

## Alternatives considered

- **`langchain-azure-ai` (`AzureAIOpenAIApiChatModel`)**, the package Microsoft's Foundry
  LangChain guide recommends. Rejected for now: version 1.4.0 depends on pre-release packages
  (`azure-ai-contentunderstanding>=1.2.0b2`, `azure-ai-contentsafety>=1.1.0b1`), which the
  project's dependency policy does not allow, and pulls in LangGraph, Azure AI Search, numpy and
  others that a chat adapter does not need. Revisit when its core dependencies are all stable,
  or when Foundry-specific features (Foundry project endpoint, agent service) are needed.
- **`AzureChatOpenAI`**: built for the older, dated API versions (`azure_endpoint` plus
  `api_version`). The v1 endpoint removes that versioning, and the same `ChatOpenAI` class keeps
  working when the endpoint changes.
- **Responses API** (`use_responses_api=True`). Microsoft recommends it for Azure OpenAI models,
  and some models need it for tool calls with reasoning (GPT-6.1 Sol requires it for tool calls).
  It returns reasoning items that must be replayed, which needs `ProviderState` support and its
  own eval baseline. Deferred until a model that needs it is chosen.

## Consequences

- Two dependencies: `langchain-openai` (stable, the same package the RAG ingestion service plans
  for Azure embeddings) and `openai`, whose errors the adapter handles. `langchain-openai` already
  raises LangChain `ModelError` subclasses, so the existing error translation applies, including
  the `Retry-After` hint on HTTP 429.
- Quota is per subscription, region, model and deployment type (tokens per minute). A deployment
  with no quota fails at deploy time; a deployment with too little quota returns 429, which the
  retry decorator handles. `make eval ARGS="--max-rpm N"` keeps eval runs under the quota.
- Tests run the real adapter against a mock HTTP transport (`httpx2`, the `openai` SDK's HTTP
  library): URL, Bearer header, payload (`reasoning_effort`, temperature rule,
  `max_completion_tokens`, tools), tool-call parsing, usage and error mapping. A live call is
  **not** part of the test suite.

## Tracing in Foundry (added 2026-11-26)

Traces reach Foundry through the shared OpenTelemetry collector (`docker-env`, optional
Azure Monitor pipeline) and the Application Insights resource connected to the Foundry account
(`azure-infra`). An agent that runs outside Foundry is listed once it is registered in the
project as an **external agent** (name and OpenTelemetry ID). Microsoft's external-agent page says
the spans must carry `gen_ai.agent.id` equal to that OpenTelemetry ID. Observed on 2026-11-26:
registering `ai-agent-service` (name and ID) showed traces sent *before* the agent had
`gen_ai.agent.id`, matched through `gen_ai.agent.name` on the `invoke_agent` span, which the
Control Plane documentation mentions as a fallback.

The agent sends `gen_ai.agent.id` anyway, on its `invoke_agent`, `chat` and `execute_tool` spans,
from `AGENT_ID` (default: `AGENT_NAME`): it is the documented contract of a preview feature, it
lets one agent name have several ids (for example one per environment, each registered
separately), and it makes every span filterable by agent in Application Insights. The attribute
belongs to the GenAI semantic conventions and describes the agent, so it is set in the
application rather than added by the collector. Spans still carry no prompts, replies or tool
arguments.

## Later

- **Keyless authentication with Microsoft Entra ID**: `azure-identity`'s
  `get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")` passed as
  the API key (`ChatOpenAI` accepts a callable), role `Cognitive Services OpenAI User`. This
  removes the long-lived key and fits the Entra ID work planned in Security II.
- The Responses API, when a model that needs it is adopted (see above).
