# ADR 0004: One evolving agent repository; separate repositories per deployable

Status: accepted

## Context

The service started as an agent integrated with an MCP server (`mcp-tool-agent`). The roadmap adds
RAG, other loop strategies, evals and prompt engineering. The question is whether to grow this
repository or start a new project for each step.

## Decision

1. **Rename to `ai-agent-service`** and keep evolving it. RAG, loop strategies, evals and prompts are
   capabilities of the same agent, not separate products.
2. **New capabilities go behind the existing seams:**
   - **RAG retrieval is a tool**, exposed over MCP (by the mcp-server or a dedicated RAG MCP server)
     and consumed through the `ToolGateway` port. The agent loop does not change.
   - **New loop strategies** (plan-and-execute, reflection) are new application use cases next to
     `RunAgent`, selected by configuration, not by the model.
   - **Evals** live in this repository (`evals/`), so every result is tied to a specific commit of
     code and prompts.
   - **Prompts** become versioned files in the repository, and changing one runs the evals.
3. **A separate repository only for something with its own deployment lifecycle:** the MCP server,
   and the RAG ingestion pipeline (batch loading, chunking, embeddings, vector store writes).
4. **Milestones are tags** (`v0.1` MCP tool calling, `v0.2` RAG, `v0.3` evals...), each with its ADRs.

## Trade-offs

- A generic name says less at first glance than "mcp"; the description and GitHub topics carry it.
- RAG as a tool costs an extra model turn, and the model may skip retrieval. If a use case needs
  retrieval every time, add a pre-retrieval step as an application port; the tool stays.
- One repository can grow too large. The dependency rule test keeps layers clean, and anything
  independently deployable moves out.
