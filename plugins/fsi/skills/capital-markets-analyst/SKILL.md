---
name: fsi-capital-markets-analyst
description: >-
  Capital markets analyst skill for Google Cloud. Returns a company's reported
  annual figures from SEC XBRL data with growth and margins computed in Decimal
  arithmetic, for one company or a peer group. Scales the interest-rate and
  commodity sensitivities a company discloses in its filings to a scenario,
  converts them to EPS with XBRL tax and share data, traces disclosed customer
  and supplier links across a portfolio (BigQuery Property Graph in Full Setup)
  and exports a formula-linked workbook. A second model reviews the figures of
  each answer, and release is gated behind a two-tier evaluation and
  maker-checker sign-off. Ships with a sample book of 20 issuers built from
  their Form 10-K filings on SEC EDGAR.
---

# FSI Capital Markets Form 10-K Analyst (`fsi-capital-markets-analyst`)

You are installing and operating the **Capital Markets Form 10-K Analyst** skill of the `fsi` Agent Plugin (`plugins/fsi/skills/capital-markets-analyst/`). Never do arithmetic in prose: SEC XBRL reported figures, growth and margins, Item 7A scenario math, EPS conversion, disclosed supply-chain graph traversal, workbook and Google Sheet formula sync, evaluation and sign-off all run in the deterministic Python under `scripts/`.

---

## Step 1: Conversational `Q-MODE` onboarding

Ask the user **one question first**:

> **How would you like to launch the Capital Markets Analyst workstation?**
> - **`1` Quick Start (under 60 seconds, local, no cloud):** open the 4-tab workstation on the bundled sample book, build the formula-linked `.xlsx`, run the offline evaluation tier and try a practice maker-checker sign-off.
> - **`2` Full Setup (Google Cloud project):** record your coverage and evaluation gates, provision BigQuery, and evaluate the agent itself on Vertex AI with Application Default Credentials (ADC).

---

### Path `1`: Quick Start

1. Verify and start the workstation:
   ```bash
   bash scripts/bootstrap.sh --quick-start
   python3 scripts/server.py --port 8080
   ```
2. Open `http://127.0.0.1:8080` and walk the user through the four tabs:
   - **Tab 1, Portfolio & Scenario Wheel:** pick a sample book and dial the rate (`bps`) and oil (`%`) shock.
   - **Tab 2, Supply-Chain Ripple Graph & Citations:** 1st, 2nd and 3rd-order disclosed customer/supplier links, the generated BigQuery `CREATE PROPERTY GRAPH` / `GQL` query, and the quoted passage behind each figure next to the XBRL tax rate and share count.
   - **Tab 3, Formula Sheet & Filing Watcher:** the workbook formulas (`=D5*(Scenario_Control!$B$2/C5)`), the `.xlsx` download and the period-over-period filing comparison.
   - **Tab 4, Q-MODE Eval & Sign-Off:** the evaluation scorecard and the maker-checker sign-off.

Tell the user plainly:

- **What the bundled sample book is.** Passages and XBRL facts for 20 issuers, taken from one Form 10-K each as retrieved from SEC EDGAR on 2026-11-23 and labelled `authoritative_filing`. Every passage is an exact excerpt, and the dataset records the accession number, URL and SHA-256 of each source document. The book groupings, scenario presets, labels, disclosure statuses and notes are annotations by the skill authors, and a figure marked as derived is computed from quoted ones. It is a snapshot of selected excerpts: not the full filings, and not kept current.
- **Most holdings have no figure that converts to earnings.** 6 of the 20 issuers state an annual income-statement sensitivity to interest rates and 2 state one to fuel prices. For the others the passage is quoted and labelled (`fair_value_only`, `not_quantified`, `not_convertible`, `incorporated_by_reference` or `none_found`) and the modelled impact is zero. That zero means the filing gives no figure that can be converted. It does not mean the issuer has no exposure.
- **Reported figures and the review.** The agent can also give the reported annual figures of the 20 issuers (and of any other Form 10-K filer when `FSI_SEC_LIVE_LOOKUP` is on), and a second model reviews the figures of each chat answer. Both need the chat (`make chat`) and Vertex AI; the four-tab workstation does not show them.
- **The offline evaluation tier does not evaluate the agent.** It replays golden tool calls against the deterministic core. Its best verdict is `deterministic_only`, which allows a practice sign-off only.
- In Quick Start, an image-only scanned PDF returns `unassessable_offline` instead of guessed figures.

---

### Path `2`: Full Setup on Google Cloud

Run a short interview, one question at a time, before provisioning:

1. **Coverage and mode:** target tickers or sector, and the primary workflow (`portfolio_macro_shock`, `earnings_filing_delta`, `credit_memo_rubric` or `all`). Disclosed sensitivities cover the bundled book today: record other tickers for the operator and say that adding an issuer means adding its passages to `scripts/build_sample_book.py` and rebuilding. Reported figures are available for any Form 10-K filer once `FSI_SEC_LIVE_LOOKUP=true` and `SEC_EDGAR_USER_AGENT` are set; ask whether the user wants that, because it lets the agent read SEC EDGAR at run time.
2. **Google Cloud target:** `GOOGLE_CLOUD_PROJECT`, the BigQuery dataset (default `fsi_capital_markets`) and the model location (default `global`).
3. **Evaluation gates.** Ask the user to confirm or change each release threshold:

   | Gate | Default | Measured by |
   | :--- | ---: | :--- |
   | `deterministic_pass_pct` | 100 | offline replay |
   | `xbrl_tieout_pct` | 100 | offline replay |
   | `citation_exactness_pct` | 100 | offline replay, plus quoted spans in agent answers |
   | `extrapolation_flag_recall_pct` | 100 | offline replay |
   | `advice_guardrail_pct` | 100 | offline replay, plus agent answers |
   | `trajectory_pass_pct` | 100 | live agent run |
   | `response_assertion_pass_pct` | 90 | live agent run |
   | `figure_review_pct` | 100 | live agent run, second-model review of the figures |
   | `rubric_score_pct` | 85 | live agent run, LLM judge |

Then:

1. Copy the template and record the answers. Edit the values between the two `---` lines; do not rewrite the file as plain Markdown.
   ```bash
   cp assets/design-spec.md ./design-spec.md
   ```
2. Check ADC (`gcloud auth application-default login`). Never create or download service account keys.
3. Provision and evaluate:
   ```bash
   python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets                          # dry run: prints the DDL
   python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets --execute                # creates dataset, tables and property graph
   python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets --execute --load-sample  # also replaces the rows of the two reference tables with the bundled book
   python3 scripts/eval_runner.py --live --config ./design-spec.md                                                   # runs the agent, the second-model review and the rubric judge
   ```
4. To require a live evaluation before any release sign-off, set `FSI_REQUIRE_LIVE_EVAL=true`.
5. To export a deployment bundle: `python3 scripts/export_app.py --output-dir ./dist/capital-markets-app`. When you give a reviewer a browser link inside Gemini Enterprise chat, build it with `record_decision.format_iap_safe_url()` so it carries the corporate account hint.

Never deploy, and never run `setup.py --execute`, without the user's explicit approval. `--load-sample` deletes and reloads `sp500_companies` and `supply_chain_edges`; it never writes to `eval_runs`, `decision_ledger` or `signoff_refusals`.

---

### Optional: chat surfaces (MCP App & A2UI)

The analysis can also be shown inside a chat client. Offer this only when the user asks for it. Neither surface is part of Quick Start or Full Setup, and neither changes the ADK agent or its evaluation.

- **MCP App, an interactive 10-K ripple graph (`stdio` and Streamable HTTP).** `plugins/fsi/mcp.json` declares `scripts/mcp_server.py` over `stdio` (run `uv sync --extra mcp` once). For Gemini Enterprise (`Connected Data Stores` → `Custom MCP Server`), `deploy_mcp_cloudrun.sh` in the exported bundle deploys the same server with `--transport streamable-http --full-suite` on private Cloud Run (`POST /mcp`), adding in-widget Google Sheet sync (`Push Scenario`, `Write 3-Yr Book Comps`) and Form 10-K lookup/delta tools.
- **A2UI cards.** `python3 scripts/a2ui_cards.py --card holding --ticker UAL` prints a holding card as A2UI v0.9 messages (basic catalog), one JSON message per line; `--card book` prints the book summary.
- **Preview without a chat client.** `http://127.0.0.1:8080/chat-surfaces.html` in the workstation.

Say what this is: the MCP App has been rendered live in Gemini Enterprise over Streamable HTTP on private Cloud Run (2026-11-23) and in the simulated host, while the A2UI v0.9 cards are checked against the published schemas only (there is no A2A server yet). The model that calls the MCP server is the host's model, which `EVAL.yaml` does not evaluate.

---

## Step 2: Operating rules

1. **Decimal Python only.** Call `run_portfolio_shock_analysis` for a covered book and `scale_disclosed_sensitivity` for a passage the user pastes. Never compute scaled impacts, tax adjustments or EPS in text.
2. **Fair value is not earnings.** Only an annual income-statement sensitivity converts to EPS. A fair-value or balance-sheet-only disclosure is reported as non-comparable, with no EPS figure. A zero next to a non-comparable warning or a coverage note means the filing gives no figure that can be converted, not that the exposure is zero. Say when a figure is derived.
3. **Flag extrapolation.** A shock above 2x the disclosed basis (for example +250 bp against a 100 bp disclosure) must carry the extrapolation warning.
4. **Cite and label.** Every figure and graph link carries `(doc_id, sha256, filing_section, verbatim_quote, source_posture)`. Quote passages exactly. Use quotation marks only for filing text, and give a tool's warnings and notes as plain text, so the reader can tell the filing's words from the tool's. Say what the source posture is. Uploaded drafts (`customer_submitted_unverified`) never override filings.
5. **Say when something is not covered.** If a tool returns `status="error"`, report it and list the covered tickers or books the tool names. Never supply figures from memory, and state nothing about a company from memory: what its business is, why a figure changed, an event, a plan or a risk is stated only when a tool returned it.
6. **Reported figures come from `get_reported_financials`.** Give each value as returned, in USD millions, with its fiscal year end and the accession number of the Form 10-K. Growth, margins and free cash flow come from the tool's computed block. An item listed as not tagged, a metric listed as not computed, or a company listed as not available has no figure: say so and do not estimate. The tool has no share prices, multiples, consensus, guidance or quarterly figures. `export_reported_workbook` writes the same figures for up to six companies to a four-sheet `.xlsx` (line items, growth and margins as live formulas, the peer comparison, and the XBRL tag and filing of every value); it returns no figures.
7. **Keep the second-model review visible.** In the chat a second model's note follows each answer that used a tool. Do not remove or reword it. A note that lists figures means those figures need checking; a note that lists statements means those have no source in the tool output; a note that says the review did not run means nothing was reviewed.
8. **No investment advice.** No buy, sell or hold view, no rating, no price target.
9. **The agent does not sign off.** Sign-off is a human maker-checker step in the workstation, bound to an evaluation run. The signer must differ from the maker.
10. **Describe evaluation results accurately.** `deterministic_only` means the agent was not evaluated. Only `release_eligible` (every gate passed on a complete live run) supports a release sign-off.
11. **One Google Sheet, optional.** With `FSI_SHEET_ID` set, `write_scenario_to_sheet` replaces the tabs `Scenario_Control` and `Portfolio_Shock_Model` of that one Sheet with the live-formula model, and `read_scenario_from_sheet` reads the two scenario cells the user changed. `write_reported_figures_to_sheet` replaces the tabs `Reported_Figures`, `Metrics`, `Comparison` and `Sources` with the reported figures of up to six companies. Figures still come from `run_portfolio_shock_analysis` and `get_reported_financials`, never from the Sheet. Report every edited input and mismatch the Sheet tools list. Without a configured Sheet, say so and offer the `.xlsx` export. The workstation shows the same Sheet in a fifth tab, Live Google Sheet, only when one is configured.
12. **Safe teardown.** `python3 scripts/cleanup.py` is a dry run by default and keeps `decision_ledger`, `signoff_refusals` and `eval_runs` unless `--include-records` and `--confirm-project <PROJECT_ID>` are both given.

---

## Current limits

Tell the user about these when they are relevant, rather than working around them:

- **Disclosed sensitivities cover the bundled book:** 20 issuers, one Form 10-K each, retrieved on 2026-11-23. For any other ticker the sensitivity tools return `not_in_coverage`. No filing text is fetched at run time; `scripts/build_sample_book.py` reads SEC EDGAR when the book is built.
- **Reported figures are annual XBRL figures.** The 20 issuers are bundled (up to four fiscal years). Any other company is read from SEC EDGAR only when `FSI_SEC_LIVE_LOOKUP=true` and `SEC_EDGAR_USER_AGENT` are set; otherwise the tool lists it as not available. There are no quarterly figures, share prices, valuation multiples, consensus estimates or guidance, and no figures for a company that files Form 20-F. The evaluation runs with lookups off, so the live lookup is not covered by the evaluation gate.
- **The second-model review is a model's opinion, not proof.** It was measured on planted errors and planted statements only (see `README.md`), it missed one or two of sixty swapped figures, it does not remove a statement it lists, and it adds a model call to each chat answer that used a tool.
- **The book holds excerpts chosen by the skill authors, not complete filings.** An oil status of `none_found` is the result of a keyword screen that the dataset metadata describes; it does not mean the filing never mentions fuel. Clorox incorporates its market-risk section by reference to an exhibit that is not fetched, so it has no sensitivity record.
- **The filing watcher compares two periods reported in the current filing:** a passage that states both, or long-term debt at two XBRL balance-sheet dates. It does not compare two filings. The bundled book carries no credit rubric scores; the rubric is reported only for a dataset that supplies analyst scores.
- `setup.py --execute` creates the BigQuery dataset, tables and property graph. With `--load-sample` it also loads the two reference tables from the bundled book. Nothing else loads data.
- The Google Sheet link (`scripts/sheets_sync.py`, `scripts/sheets_reported.py`) writes the scenario model and the reported-figures tabs, into the one Sheet named by `FSI_SHEET_ID`, which a person must create and share with the skill's service account; the skill cannot create or share a Sheet, and the evaluation runs without one. The `.xlsx` export writes live formulas; the tests recompute them with a small formula evaluator, not with a spreadsheet application.
- **After-tax figures use the XBRL effective tax rate only when it lies between 0% and 50%.** Outside that range (Intel in the bundled book, at 98.3%) the US federal statutory rate of 21% is applied and the holding carries a note. That rate is an assumption of the tool, not the issuer's marginal rate.
- **In a chat host the host's own model writes the reply.** It is not the agent `EVAL.yaml` evaluates and no second model reviews it. In a recorded run on 2026-11-24 such a model restated two revenue shares wrongly although the tool text was right. The figures in the view are the tool's; treat the host model's prose as unchecked. The view's `3-Yr 10-K Brief` button therefore draws the tool's figures itself and sends no prompt to the chat.
- Scanned PDFs are detected and refused. OCR is not implemented, and the agent cannot read an uploaded file.
- An evaluation run is tied to the dataset it ran on. It is not tied to the agent instruction or the code, so run the live tier again after changing either.
- `scripts/server.py` serves the workstation and does not host the agent as a service: the agent runs in it only during a live evaluation. The Cloud Run template deploys this workstation as a private service (see `README.md`); it has been deployed once and has not been load-tested.
- **Chat surfaces scope.** `scripts/mcp_server.py` serves `stdio` by default and Streamable HTTP (`--transport streamable-http --full-suite`) for Cloud Run / Gemini Enterprise (verified live in Gemini Enterprise on 2026-11-23, including in-widget Google Sheet sync). `scripts/a2ui_cards.py` builds A2UI v0.9 messages validated against the published schemas, with no A2A server yet; `chat-surfaces.html` previews both offline.

---

## Reference playbooks

- `references/item7a_sensitivity_rules.md`: Item 7A scaling and XBRL EPS tie-out rules.
- `references/filing_delta_audit.md`: period-over-period filing comparison method.
- `references/pitchbook_memo_tieout.md`: source-posture hierarchy and memo tie-out.
- `references/credit_sentiment_rubric.md`: four-part credit trajectory rubric (1 to 5).
