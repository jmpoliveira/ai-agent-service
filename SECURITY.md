# Security policy

## Supported versions

This is a personal learning project. Only the latest release and the `main` branch receive
security fixes.

| Version | Supported |
|---|---|
| Latest release / `main` | Yes |
| Older releases | No |

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for security problems.**

Report it privately through GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/jmpoliveira/ai-agent-service/security/advisories/new)
(also reachable from the **Security** tab of this repository).

Please include:

- what the problem is and where (file, endpoint or setting);
- the steps or a minimal input to reproduce it;
- what an attacker could do with it;
- the version or commit you tested.

Do not include real API keys, personal data or other secrets in the report.

## What to expect

This project is maintained in spare time, so responses are best effort:

- an acknowledgement within about one week;
- a fix, or an explanation of why it will not be fixed, as soon as is reasonable;
- credit in the advisory and release notes, if you want it.

Please give a reasonable amount of time for a fix before disclosing the problem publicly.

## Scope

In scope: the code and configuration in this repository (the agent service, its HTTP API,
the MCP client, the Docker and CI files).

Out of scope:

- vulnerabilities in dependencies (LangChain, the MCP SDK, FastAPI...): report them to the
  upstream project; Dependabot alerts track known issues here;
- the behaviour of the LLM providers themselves (Ollama, Gemini, Claude);
- the companion [mcp-server](https://github.com/jmpoliveira/mcp-server), which has its own
  repository.

Prompt injection through tool results is a known, documented limitation: the service reduces
it (sanitisation, size caps, framing, tool allowlists) but cannot remove it. See the
[security notes in the README](README.md#security-notes). A concrete way to bypass these
mitigations is still a welcome report.