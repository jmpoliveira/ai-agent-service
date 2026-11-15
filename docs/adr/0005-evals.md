# ADR 0005: Offline tool-use evals with a small in-repo harness

Status: accepted

## Context

The tests prove the agent's machinery with scripted models, but say nothing about how a real
model behaves: whether it picks the right tool, passes the right arguments, answers from the
tool results, or calls a write tool it should not. Prompts, tool descriptions and models will
change; each change needs a measurement, and comparisons between models (ADR 0003) need the
same cases.

## Decision

1. **A small harness in this repository (`evals/`), no framework yet.** Under 1,000 lines with
   docstrings: dataset, stubs, trajectory, graders, runner, report. It reuses the domain types and the composition
   root, so the agent under test is the agent that is deployed, and it shows the mechanics that
   frameworks (Inspect AI, DeepEval, promptfoo, Langfuse) hide. Revisit when online evals,
   dashboards or LLM-as-judge at scale are needed.
2. **A separate entry point, outside the service package.** `python -m evals` sits next to the
   HTTP API as another consumer of `main.py`. The service never imports `evals` (enforced by
   `tests/test_architecture.py`) and the Docker image does not contain it.
3. **Dataset in TOML.** Read by the standard library (no new dependency), already the format of
   `pyproject.toml`, validated by pydantic with unknown fields rejected. YAML is more common in
   eval tools but would add a dependency and its type stubs.
4. **Grade what the model asked for.** The trajectory comes from `AgentRun.messages`, which
   includes calls the agent blocked (a tool that was not offered). Reading the conversation needs
   no change to the agent loop.
5. **Code-based graders first.** Tool sequence (ordered subsequence), arguments, forbidden tools,
   steps, stop reason and answer facts. Deterministic, cheap and explainable. An LLM judge is a
   later addition for open-ended answers, calibrated against human review.
6. **Evals never change the server's data.** Write tools are stubbed: the model sees the real
   tool definition, the call is answered with canned text. Cases stay independent and
   repeatable. The same mechanism plants prompt injections in read-tool results; stubbed results
   still go through the agent's sanitising and framing, so the real defences are tested.
7. **Repeats and consistency.** Each case runs N times (default 3) sequentially, and the report
   shows both the run pass rate and the share of cases passing every run.
8. **Reports are self-describing.** Provider, model, temperature, dataset fingerprint and system
   prompt fingerprint, plus every run with its trace id. Results are git-ignored; a report kept as
   a baseline is committed under `evals/baselines/`.
9. **Penalise guessing.** A failed tool call (an invented id, a customer id passed as a ticket
   id) counts against the run (`max_tool_errors`, default 0), because a model can guess its way
   to a correct final answer. The first real Gemini run did exactly that, and passed on answer
   checks alone. Failed guesses are only half of it: without thinking, qwen3 inferred T-100N
   from C-00N, guessed right, and passed. So reports record the prompt and every tool result,
   and `grounded_ids` requires every identifier argument (`id_pattern`) to have appeared before
   the call. It also catches writes made with an invented id.
10. **Respect provider quotas by waiting.** `--max-rpm` wraps each model in a sliding-window
   `RateLimitedChatModel`; a rate-limit error usually carries a Retry-After longer than the
   retry policy accepts, so waiting before the request is the only way not to lose the run.
11. **Comparable by construction.** Reports record provider, model, settings, system prompt and
   dataset fingerprints and the git commit. Old reports can be re-graded with a new dataset
   version (the trajectories are saved), and a comparison warns when the dataset differs.
   Dataset history is git plus a changelog in the file, not copies of old versions. The
   fingerprint hashes the graded content, so a comment does not make two runs incomparable.
12. **Fail fast on a broken environment.** An unreachable MCP server, bad credentials, an unknown
   provider or a model without tool support aborts the eval; transient provider errors are
   recorded per run.

## Consequences

- `make eval` needs the MCP server and a model provider running; it is not part of `make check`
  or CI (cost, and non-determinism would make CI flaky). `--fail-under` exists for a later CI gate.
- The dataset is tied to the mcp-server mock data; changing that data means updating the cases
  and bumping the dataset version.
- Answer checks match text, so they check facts, not wording, and can miss a correct answer
  phrased unexpectedly. Failures are read, not only counted.
