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
4. **API key first, Microsoft Entra ID next.** The key is a `SecretStr` from the environment and
   is sent as a Bearer token, which the v1 API accepts. Keyless authentication followed on
   2026-11-26 (see "Keyless authentication" below).
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

## Keyless authentication (added 2026-11-26)

`FOUNDRY_AUTH=entra_id` replaces the API key with Microsoft Entra ID tokens:

- `azure-identity`'s `get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_TOKEN_SCOPE)`
  is passed to `ChatOpenAI` as the key. The client calls it before each request; the provider
  caches the token and refreshes it before it expires. The scope defaults to
  `https://ai.azure.com/.default`, the one in Microsoft's v1 API examples.
- **Synchronous credential.** langchain-openai runs a synchronous key provider in a worker
  thread, so a refresh does not block the event loop, and the `aio` credential would add an
  async HTTP stack (aiohttp) for no gain.
- **`DefaultAzureCredential`**: the same code signs in with `az login` on a laptop, a service
  principal from environment variables in CI, and a managed identity on Azure compute.
- **Access is a role, not a secret**: the caller needs *Cognitive Services OpenAI User* on the
  Foundry account (granted in the `azure-infra` repository). It allows inference only: no key
  listing, no deployment changes. Calls are attributed to an identity in the Azure logs and can
  be revoked per person.
- **Errors**: a failed sign-in (no `az login`, no managed identity) is reported as an
  authentication error, which the retry decorator does not retry. Without the adapter's
  translation it would surface as an unclassified error.
- **Fail fast**: `FOUNDRY_API_KEY` together with `FOUNDRY_AUTH=entra_id` is refused at startup,
  so a key meant to be retired does not linger.
- **Docker**: the container has no `az login`. It needs `FOUNDRY_AUTH=api_key`, a service
  principal through `AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_CLIENT_SECRET` (a secret again), or
  a managed identity once it runs on Azure.

**Verified live on 2026-11-27** (`FOUNDRY_AUTH=entra_id`, no key in the environment): the eval
ran against `gpt-6-luna` and its traces reached Application Insights and Foundry. Signing in was
the hard part, and is worth recording:

- The tenant has Microsoft Entra **security defaults** on. The Azure CLI **device code flow**
  (`az login --use-device-code`) from an unregistered device was refused with `AADSTS530035`
  ("blocked by security defaults"), even after a successful MFA prompt, for the management scope
  as well as for the AI Services scopes. `DefaultAzureCredential` reports it in its
  `AzureCliCredential` entry; the agent turns it into an authentication error, so the eval stops
  at the first case instead of retrying.
- The **interactive browser flow** (`az login --tenant <id>`, with MFA) is accepted. In WSL it
  needs a way to open the Windows browser with the full sign-in URL: `wslview` from the `wslu`
  package and `BROWSER=wslview`. Without it, the URL the CLI prints has no query string and the
  sign-in fails with `AADSTS900144` (missing `client_id`).
- Security defaults stay on: turning them off would remove required MFA for the whole tenant to
  save a developer one sign-in. The CLI session expires and asks for MFA again; when it does, the
  agent reports the authentication error and `az login` fixes it.

Once every client is keyless, the account can disable keys altogether (`disableLocalAuth` in
`azure-infra`). Not yet: the Docker image of the agent and the planned embeddings adapter of
`rag-ingestion-service` still use the key.

## Later

- The Responses API, when a model that needs it is adopted (see above).
