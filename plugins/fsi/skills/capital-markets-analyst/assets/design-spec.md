---
# Capital Markets Analyst - Design Spec
#
# The Q-MODE interview records its answers here. Edit the values between the two --- lines;
# do not rewrite this file as plain Markdown.
#
# What reads this file today:
#   scripts/eval_runner.py --config ./design-spec.md   -> eval_gates
# The other fields record the interview answers for the operator. Pass the project and
# dataset to scripts/setup.py with --project / --dataset.

# --- Google Cloud (Full Setup) ---
gcp_project_id: ""                       # your GCP project ID
gcp_region: us-central1                  # BigQuery dataset location
dataset_id: fsi_capital_markets
graph_name: disclosed_supply_chain_graph

# --- Coverage ---
tickers: []                              # e.g. [DAL, UAL, BA]; empty = bundled sample book
workflow_mode: all                       # portfolio_macro_shock | earnings_filing_delta | credit_memo_rubric | all

# --- Evaluation gates, percent (0-100) ---
# A gate fails when its measured pass rate is below the threshold. The last four gates
# can only be measured by a live agent run (scripts/eval_runner.py --live).
eval_gates:
  deterministic_pass_pct: 100            # every golden check on the deterministic core
  xbrl_tieout_pct: 100                   # Decimal pre-tax / after-tax / EPS tie-out
  citation_exactness_pct: 100            # quoted passage is exact and carries its excerpt hash
  extrapolation_flag_recall_pct: 100     # shocks above 2x the disclosed basis are flagged
  advice_guardrail_pct: 100              # no buy / sell / hold language
  trajectory_pass_pct: 100               # agent called the right tool with the right arguments
  response_assertion_pass_pct: 90        # agent answer contains the required figures and warnings
  figure_review_pct: 100                 # a second model finds no unsupported figure in an answer
  rubric_score_pct: 85                   # MUST / MUST NOT rubric lines, graded by the LLM judge
---

# Capital Markets Analyst

## Overview

This design spec captures the choices made during the `SKILL.md` Q-MODE interview: where
the skill runs, which issuers it covers, and the evaluation gates a run must meet before
a release sign-off.

## How to use

1. Fill in `gcp_project_id` (or let the coding agent do it during the interview).
2. Adjust `eval_gates` to your institution's release criteria.
3. Run `python3 scripts/eval_runner.py --config ./design-spec.md` for the offline tier, and
   add `--live` to evaluate the agent itself.

## Design decisions

Record any non-obvious choice here, for example why a gate was lowered and who agreed to it.
