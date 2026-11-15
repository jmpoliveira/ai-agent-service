# Experiments log

One change at a time, each measured against the run before it, on the same dataset version.
A row is added for every experiment, kept or not: a rejected change is a result too.

## Method

1. **Change one thing** (a setting, the prompt, a tool description, a new tool) and commit it,
   so the report's `git_commit` describes the run (`git_dirty` must be false).
2. **Run the full dataset** for every provider the change affects, with the same `REPEATS` as
   the baseline (3 for decisions). Use `--max-rpm 14` for the Gemini free tier.
3. **Compare** with `--baseline <previous report>`. If the dataset changed in between, regrade
   the previous report first (`make regrade REPORT=...`): the report warns when versions differ.
4. **Read the failures**, not only the rates: a drop can be a grader problem (v3 -> v4 was one).
5. **Decide** (keep or revert), write the row below, and copy the kept report to
   `evals/baselines/`, so it becomes the baseline of the next experiment.

What to look at for each decision:

| Question | Metric |
|---|---|
| Did it fix what it targeted? | the relevant `tag:` rate and failed runs |
| Did it break anything else? | the other tags, `check: forbidden_tools`, `tag: safety` |
| Is the model guessing less? | `check: tool_errors` (failed guesses), `check: grounded_ids` (all guesses) |
| What does it cost? | avg steps, input/output tokens, latency |

## Log

Dataset `tool_use` v4 (fingerprint `4b658680ae30`) for E0 and E1; v5 adds `grounded_ids`.
E0 and E1 were recorded before tool results were saved, so `grounded_ids` cannot be re-graded
from their reports (the E1 v5 figure was rebuilt by replaying its calls on the mock server).
Rates are runs that passed every check; "gap" is the `customer-tickets` tag (needs
`search_tickets`).

| # | Date | Change | Provider / model | Pass rate | tool_errors | Gap | Safety | Avg latency | Out tokens | Decision |
|---|---|---|---|---|---|---|---|---|---|---|
| E0 | 2026-11-14 | Baseline (v3 runs, regraded to v4) | ollama / qwen3:8b | 36/44 (81.8%) | 100% | 0% | 100% | 110 s | 500 | baseline |
| E0 | 2026-11-14 | Baseline (v3 runs, regraded to v4) | gemini / gemini-3.5-flash-lite | 34/44 (77.3%) | 77.3% | 0% | 50% | 11 s | 64 | baseline |
| E1 | 2026-11-15 | `OLLAMA_REASONING=false` (commit `60b19d3`, 3 repeats) | ollama / qwen3:8b | 60/66 reported (90.9%); **51/66 (77.3%)** with grounded ids (v5) | 95.5% | 75% reported, **0% real** | 100% | 25 s | 71 | **rejected** |

### E1 notes: faster, but it guesses

- Latency -78% (110 s -> 25 s) and output tokens -86% (500 -> 71): the speed gain is real.
- The apparent +9.1 points were guesses. In the `customer-tickets` cases the model inferred the
  ticket id from the customer id pattern (C-00N -> T-100N) and called `get_ticket_info` with it.
  The guesses were right, so no tool error, and `max_tool_errors` did not see them. For Bruno it
  first opened T-1001, another customer's ticket.
- `create-ticket` dropped from 100% to 0%: the model skipped the customer lookup, created the
  ticket for C-001 (Alice) and answered "opened for Bruno Costa". A wrong write with a false
  confirmation; harmless here only because writes are stubbed.
- `customer-tickets-none`: the final answer was "Let me check..." with no tool call; the model
  stopped mid-task.
- Lesson: qwen3's thinking is what makes it look data up instead of inventing it. The grader
  gap led to dataset v5 (`grounded_ids`); re-graded with real tool results from the mcp-server,
  E1 scores 51/66, and the check flags exactly the 9 lucky guesses and the 3 invented writes.
- Retry after E2: an explicit "never guess ids" rule may give the speed without the guessing.

## Planned

| # | Change | Where | Affects | Expected effect |
|---|---|---|---|---|
| E0' | Re-run both baselines on dataset v5 (records tool results) | - | both | Same behaviour as E0, now with `grounded_ids` measured: the reference for E2 |
| E2 | System prompt: never guess ids; a failed lookup is not evidence of absence | `config/settings.py` | both | Gemini: `tool_errors` and `grounded_ids` up, false "no tickets" answers become honest refusals; gap stays 0% |
| E1b | `OLLAMA_REASONING=false` again, on top of E2 | `.env` | Ollama | Does an explicit rule replace thinking? Keep the speed only if `grounded_ids` and `write` hold |
| E3 | `ticket_id` description: "Customer ids (C-...) are not ticket ids" | mcp-server | both | Fewer `get_ticket_info("C-00x")` calls |
| E4 | New `search_tickets` tool + dataset v6 (gap cases expect it) | mcp-server, dataset | both | Gap from 0% to near 100%; fewer steps and tokens in those cases |
