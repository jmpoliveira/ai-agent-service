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
| Is the model guessing less? | `check: tool_errors` |
| What does it cost? | avg steps, input/output tokens, latency |

## Log

Dataset `tool_use` v4 (fingerprint `4b658680ae30`) unless stated otherwise. Rates are runs that
passed every check; "gap" is the `customer-tickets` tag (needs `search_tickets`).

| # | Date | Change | Provider / model | Pass rate | tool_errors | Gap | Safety | Avg latency | Out tokens | Decision |
|---|---|---|---|---|---|---|---|---|---|---|
| E0 | 2026-11-14 | Baseline (v3 runs, regraded to v4) | ollama / qwen3:8b | 36/44 (81.8%) | 100% | 0% | 100% | 110 s | 500 | baseline |
| E0 | 2026-11-14 | Baseline (v3 runs, regraded to v4) | gemini / gemini-3.5-flash-lite | 34/44 (77.3%) | 77.3% | 0% | 50% | 11 s | 64 | baseline |

## Planned

| # | Change | Where | Affects | Expected effect |
|---|---|---|---|---|
| E1 | `OLLAMA_REASONING=false` | `.env` | Ollama | Much lower latency and output tokens; does accuracy hold? |
| E2 | System prompt: never guess ids; a failed lookup is not evidence of absence | `config/settings.py` | both | Gemini: `tool_errors` up, false "no tickets" answers become honest refusals; gap stays 0% |
| E3 | `ticket_id` description: "Customer ids (C-...) are not ticket ids" | mcp-server | both | Fewer `get_ticket_info("C-00x")` calls |
| E4 | New `search_tickets` tool + dataset v5 (gap cases expect it) | mcp-server, dataset | both | Gap from 0% to near 100%; fewer steps and tokens in those cases |
