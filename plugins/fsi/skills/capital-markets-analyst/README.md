# Capital Markets Form 10-K Analyst (`fsi-capital-markets-analyst`)

A dedicated **SEC Form 10-K & XBRL** skill in the `fsi` Agent Plugin for equity research, credit surveillance and portfolio risk teams. An ADK agent answers questions about a company's reported annual figures from SEC XBRL and about the Item 7A market-risk and supply-chain fine print it discloses in Form 10-K. Deterministic Python does everything that has to be exact: reading SEC-tagged figures, growth and margins, scenario scaling, EPS conversion, disclosed customer/supplier traversal, workbook and Google Sheet formulas, evaluation and sign-off. A second model then checks every figure and unsourced company claim in each answer against what the tools returned.

> **Sample data notice.** The bundled sample book holds excerpts and XBRL facts from one Form 10-K for each of 20 issuers, retrieved from SEC EDGAR on 2026-11-23 and labelled `authoritative_filing`. It is a snapshot of selected excerpts, not the full filings, and it is not kept current. A second file holds the same issuers' reported annual figures from SEC XBRL company facts. The book groupings, scenario presets, labels, disclosure statuses and notes are annotations by the skill authors. Not investment advice.

## What it does

1. **Scenario scaling.** Scales disclosed interest-rate (`bps`) and commodity (`%`) sensitivities with `decimal.Decimal`, converts them to diluted EPS with the effective tax rate and diluted share count, flags shocks above 2x the disclosed basis, and refuses to turn a fair-value-only disclosure into an EPS figure.
2. **Ripple through disclosed links.** Traverses 1st to 3rd-order customer and supplier links in Python and generates the matching BigQuery `CREATE PROPERTY GRAPH` and `GRAPH_TABLE` (GQL) statements.
3. **Formula-linked workbooks.** Builds a four-sheet `.xlsx` of the scenario model in which every input cites its passage and every output is a live formula (`=D5*(Scenario_Control!$B$2/C5)`), and a four-sheet `.xlsx` of reported figures for up to six companies: the line items, growth and margins as live formulas over them, the peer comparison, and the XBRL tag and filing of every value.
4. **Filing watcher.** For each issuer, compares one disclosed figure across the two periods reported in the current filing. A four-part credit rubric is reported only for a dataset that carries analyst scores; the bundled book does not.
5. **Reported figures.** Returns revenue, profit, cash flow, balance-sheet and per-share figures for the latest fiscal years of one company or a peer group, each with its XBRL tag and the accession number of the Form 10-K it comes from, and computes growth, margins and free cash flow with `decimal.Decimal`. The 20 bundled issuers are answered offline. Any other company that files Form 10-K under US GAAP is read from SEC EDGAR when live lookups are switched on. See [Reported figures](#reported-figures).
6. **Second-model review.** After each answer that used a tool, a second model is given the question, the tool outputs and the answer, and lists every number it cannot support and every statement about a company that has no source in them. In the chat its note appears under the answer; in the live evaluation the figures are a gate. See [Second-model review](#second-model-review).
7. **Two-tier evaluation and sign-off.** `EVAL.yaml` holds 27 cases. The offline tier replays golden tool calls against the deterministic core. The live tier runs the agent on Vertex AI and grades its tool trajectory, its answer, the figures in it (second-model review) and the rubric. Sign-off is refused unless an evaluation run is attached, and the signer must differ from the maker.
8. **Chat surfaces (MCP App & A2UI).** The same analysis as an interactive SEP-1865 MCP App (`stdio` for IDEs and Streamable HTTP `/mcp` on Cloud Run for Gemini Enterprise, with in-widget Google Sheet sync) and as A2UI v0.9 cards. See [Chat surfaces](#chat-surfaces-mcp-app-and-a2ui-cards).
9. **Google Sheet link (optional).** Writes the same live-formula scenario model, or the reported figures of up to six companies, into one configured Google Sheet, reads back the scenario an analyst sets there, and checks what the Sheet computed against Python. The workstation shows the Sheet in a fifth tab. See [Google Sheet](#google-sheet-optional).

## Sample data

`assets/sample_10k_book/sp500_sample_book.json` holds 20 issuers, 4 books, 4 scenario presets and 12 disclosed customer or supplier links. For each issuer it records the accession number, the document URL and its SHA-256, and the XBRL tag and period behind each number.

What the filings give, and what they do not:

| | Interest rates | Fuel prices |
| :--- | ---: | ---: |
| Annual income-statement sensitivity, converted to EPS | 6 issuers | 2 issuers |
| Disclosure quoted but not converted (fair value only, not quantified, not convertible) | 13 issuers | 2 issuers |
| Market-risk section incorporated by reference to an exhibit that is not fetched | 1 issuer | 1 issuer |
| No sentence found by the build-time keyword screen | 0 issuers | 15 issuers |

A holding without a converted figure shows a zero impact and a note saying why. The zero means the filing gives no figure that can be converted, not that the issuer has no exposure. 5 of the 12 links state a revenue share; the other 7 are loaded with no share and a note.

Three figures are computed from quoted ones and are marked as derived: United's net rate figure (interest income less interest expense), and the conversion of each airline's fuel sensitivity (per gallon, per barrel) to a percent price move using the average fuel price in the same filing.

Rebuild the book, or check that the bundled file matches a fresh build:

```bash
SEC_EDGAR_USER_AGENT="Your Organisation contact@your-domain" \
  python3 scripts/build_sample_book.py --cache-dir ./.edgar_cache                # fetches from sec.gov, then writes the file
python3 scripts/build_sample_book.py --cache-dir ./.edgar_cache --offline --check  # no network; fails if the file differs
```

The builder refuses to write the file unless every sentence is an exact substring of the filing text, every table row matches one parsed row, each passage sits under the heading it is labelled with, and the XBRL facts belong to the pinned accession. SEC asks for a User-Agent that names you and a contact address, and for no more than 10 requests per second. Put your own organisation and address in `SEC_EDGAR_USER_AGENT`: the builder refuses an empty value or a placeholder such as the one above, and it stays under the request limit.

## Reported figures

`assets/sample_10k_book/reported_financials.json` holds, for the same 20 issuers, up to 4 fiscal years of 21 line items (1,495 values) read from SEC XBRL company facts. Each value carries its XBRL tag and the accession number of the Form 10-K it was taken from. The agent tool `get_reported_financials` returns them for one company or a peer group.

- **Which value.** For each fiscal year, the value in the most recent Form 10-K that reports it. If the first filing gave a different value, the record keeps that one as `restated_from`.
- **Computed, not typed.** Year-over-year changes, growth rates, margins and free cash flow are computed in Python and rounded half-up to two decimals. One is left out, with the reason, when an input is not tagged, or when its inputs come from different filings and one of them was restated. A growth rate is also left out when the prior year is zero or negative, when the figure turns negative, or when the change is more than ten times the prior-year figure: the percentage is defined but says nothing (Intel's fiscal 2024 operating income, 93 to -11,678 USD million, would read -12,656.99%).
- **Not tagged is not zero.** A line item the company does not report under one of the standard tags the skill reads is listed as not tagged. Gross margin is given only when the company tags gross profit.
- **Cross-checks.** The build fails unless the five figures the sample book already holds per issuer (revenue, diluted EPS, diluted shares, tax rate, long-term debt) equal the ones read here, value and tag. In a one-off check on 2026-11-22 that is not part of the build, 274 of the 294 latest-year values were found in a table cell of the issuer's primary filing document; the other 20 belong to one issuer whose statements are in an exhibit that was not fetched.
- **Not available:** share prices, valuation multiples, consensus estimates, guidance, quarterly figures, and companies that file Form 20-F or report under IFRS. The tool says so and returns no figure.

A company outside the bundle:

```bash
export FSI_SEC_LIVE_LOOKUP=true SEC_EDGAR_USER_AGENT="Your Organisation contact@your-domain"   # the agent may now read SEC EDGAR
python3 scripts/sec_lookup.py TSLA NFLX                                                         # or fetch companies ahead of time
```

Two SEC URLs are read: the list of company tickers and one company's XBRL company facts. Only the reduced record is kept, under `_output/company_store/` (`FSI_COMPANY_STORE_DIR`), and it is reused for a day before SEC is asked again. A company fetched ahead of time is answered with lookups off. The same contact rule applies as for the sample book: an empty or placeholder `SEC_EDGAR_USER_AGENT` is refused.

Rebuild the bundled figures, or check that the file matches a fresh build:

```bash
python3 scripts/reported_financials.py --cache-dir ./.edgar_cache                     # fetches SEC company facts, then writes the file
python3 scripts/reported_financials.py --cache-dir ./.edgar_cache --offline --check   # no network; fails if the file differs
```

## Second-model review

After the agent answers a question for which it called a tool, `scripts/answer_review.py` gives a second model the question, the tool calls, the full tool outputs and the answer, and asks it for two lists: every number that is unsupported or attached to the wrong company, metric or period, and every statement of fact about a named company (its business, an event, the reason for a change, a plan, a risk) that neither the tool outputs nor the question support.

- **In the chat** the review is a note under the answer: no finding, the figures it could not match, the statements it found no source for, or a statement that the review did not run. It adds one model call per answer; `FSI_ANSWER_REVIEW=false` turns it off.
- **In the live evaluation** the figures are the gate `figure_review_pct` (default 100), so one flagged figure blocks the run. Statements without a source are listed in the detail of the same check and do not gate.
- **Model.** `FSI_REVIEWER_MODEL`, default `gemini-3.7-flash`. Use a model other than the agent's.
- A review that could not run is reported as not run, never as a pass.

How well it catches wrong figures was first measured on 2026-11-22, when the reviewer was asked about figures only, with a script that is not part of this skill. It took 60 real answers from 8 stored live runs (16 question types) and reviewed each one as written, with one digit of one figure changed, and with one figure replaced by a different figure from the same answer:

| Reviewer | Flags on unchanged answers | Changed figure caught | Swapped figure caught |
| :--- | ---: | ---: | ---: |
| `gemini-3.7-flash` | 0 of 60 | 60 of 60 | 59 of 60 |
| `gemini-3.7-flash`, second run | 0 of 60 | 60 of 60 | 58 of 60 |
| `gemini-3.8-flash` | 4 of 60 | 60 of 60 | 59 of 60 |

A review took about 6 seconds (the slowest 96) and about 12,200 input tokens. The four flags on unchanged answers were about wording, not a wrong number. The planted errors are simple ones and real mistakes can be subtler; one swapped figure got past all three runs, and two identical runs disagreed on 1 of 180 reviews. A pass is a second model's opinion, not proof.

On 2026-11-23 the reviewer was also asked for statements without a source, and the measurement was repeated once with `gemini-3.7-flash`: the same 180 reviews, the 21 answers of one live run of this build as written, and 68 answers to which one invented sentence about a company named in the answer had been added.

| What was reviewed | Result |
| :--- | ---: |
| Changed figure caught | 60 of 60 |
| Swapped figure caught | 59 of 60 |
| Unchanged answers flagged for a figure | 1 of 81 |
| Invented statement listed | 68 of 68 |
| Unchanged answers with a statement listed | 1 of 81 |

The one figure flag was on a stored answer of an earlier build that gives 0 where the tool output now gives no figure. The one statement listed on an unchanged answer was one the agent had added itself: a restatement described as due to a stock split, which the tool output does not say. A review took about 10 seconds (the slowest 112) with 12 reviews running at once; the first measurement ran 10 at once, so the two times are not directly comparable. The invented sentences are plain and unrelated to the answer. A real unsupported statement can be subtler, and the list has been measured on these 68 only.

## Setup

```bash
uv sync                      # from this directory; installs google-adk and the other dependencies
uv sync --extra dev          # also pytest, jsonschema and the MCP SDK, which the test suite needs
cp .env.example .env         # only needed for Full Setup and the live evaluation
```

## Run: Quick Start (local, no cloud)

```bash
make test                    # offline test suite
make eval                    # offline evaluation tier
make demo                    # workstation at http://127.0.0.1:8080
```

The offline tier checks the deterministic core only. Its best verdict is `deterministic_only`, which permits a practice sign-off.

## Run: evaluate the agent (Vertex AI)

```bash
gcloud auth application-default login
export GOOGLE_GENAI_USE_VERTEXAI=True GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global
python3 scripts/eval_runner.py --live
```

Gates and their defaults are listed in `SKILL.md`; override them in `design-spec.md` (`eval_gates`) and pass `--config ./design-spec.md`. The live tier calls three models: the agent (`GEMINI_MODEL`), the second-model reviewer (`FSI_REVIEWER_MODEL`) and the rubric judge (`FSI_JUDGE_MODEL`). It always runs on the bundled data: SEC lookups are switched off for the length of a run. Models are called, so two runs can differ; `citation_exactness_pct` and `trajectory_pass_pct` are held at 100, and a single misquoted passage blocks the run. Run it again after any change to the agent instruction, the tools or the data.

## Run: chat with the agent (Vertex AI)

```bash
export GOOGLE_GENAI_USE_VERTEXAI=True GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global
make chat                    # ADK web page at http://127.0.0.1:8081/dev-ui/?app=scripts
```

This is the agent in `scripts/agent.py`, the one the live evaluation runs. Things to know:

- **The review note.** After an answer for which the agent called a tool, a second message gives the second-model review of its figures and statements. It arrives some seconds after the answer.
- **Companies outside the bundle.** Export `FSI_SEC_LIVE_LOOKUP=true` and `SEC_EDGAR_USER_AGENT` as well, and the agent can return the reported figures of any company that files Form 10-K. Without them it says the company is not available.
- **Name the book in the question.** Asked about one issuer without a book, the agent picked a different book in three of three tries. The issuer's figures were the same; anything said about the book would not be.
- **Only single questions are evaluated.** The evaluation asks each question alone in a new session, so follow-up questions in one chat are outside it.

The page is ADK's developer UI. It has no sign-in, so it listens on `127.0.0.1` only. Sessions are kept in memory, so ADK stores no session files in this folder.

## Google Sheet (optional)

The agent can put the scenario model into a Google Sheet and read back the scenario an analyst sets there. Two tools do it, `write_scenario_to_sheet` and `read_scenario_from_sheet`; the logic is in `scripts/sheets_sync.py`. A third tool, `write_reported_figures_to_sheet` (`scripts/sheets_reported.py`), puts the reported figures of up to six companies into the same Sheet.

1. Enable the Google Sheets API in the project.
2. Create a Sheet and share it, as Editor, with the service account the skill acts as. A service account has no Drive storage and cannot own a Sheet, so a person creates it.
3. Set `FSI_SHEET_ID` to the Sheet's id or link. When the skill does not already run as that service account, set `FSI_SHEETS_SERVICE_ACCOUNT` and give your own identity `roles/iam.serviceAccountTokenCreator` on it. No key file is used.

What it does and does not do:

- It writes two tabs, `Scenario_Control` and `Portfolio_Shock_Model`, and replaces them on every write. Other tabs are left alone. The analyst changes `Scenario_Control!B2` (rate, basis points) and `B3` (oil, percent), and the Sheet's formulas recalculate.
- After a write, and on every read, Python recomputes the book for the scenario in the Sheet and compares. An input cell that no longer equals the filing data is listed as edited, and a computed cell that differs from the engine by more than its rounding is listed as a mismatch.
- The reported-figures tool writes four more tabs, `Reported_Figures`, `Metrics`, `Comparison` and `Sources`, and replaces them on every write. `Metrics` holds growth and margins as live formulas over `Reported_Figures`; a metric Python did not compute shows its reason instead of a number. After the write, Python compares every cell of `Metrics` and `Comparison` with its own figure and lists the ones that differ.
- Figures are never read off the Sheet. The read tool returns the two scenario values and the comparison; the agent then calls `run_portfolio_shock_analysis` for the figures. The reported-figures tool returns no figures either; the agent calls `get_reported_financials` for them.
- The agent can reach one Sheet only, the configured one. It cannot create, share or delete a spreadsheet.
- The evaluation always runs without a Sheet, so it never writes to one. Its case `google_sheet_not_configured` checks that the agent then says so.

**In the workstation.** When `FSI_SHEET_ID` is set, the page has a fifth tab, Live Google Sheet, with the Sheet inside it and three actions. *Write this scenario to the Sheet* sends the scenario on screen one row at a time, so the rows are seen arriving, and reports the comparison. *Read the scenario from the Sheet* takes the two values from the Sheet; the page then shows the figures Python computes for them and lists any edited input. *Write reported figures to the Sheet* writes `Reported_Figures`, `Metrics`, `Comparison` and `Sources` for one to six tickers and reconciles every computed cell against Python. The workstation's Sheet is fixed when it starts, and nothing the browser sends can name another one. Whether the Sheet appears inside the page depends on the browser and on the Google account; *Open in a new window* does not.

**Checked against Google Sheets**, on 2026-11-23, with a Sheet in the `en_US` locale. Google Sheets computed the 72 formulas of the 12-holding book to the engine's figures on 12 of 12 rows at rates +50 bp with oil +25%, and again after oil was set to 40 in the Sheet; an input overwritten in the Sheet was listed as edited. For `DAL, UAL` (8 company-year rows, 99 formula cells across `Metrics` and `Comparison`), Google Sheets matched Python on 130 of 130 checked cells. Formulas are written with a comma between arguments, and a Sheet whose locale uses a semicolon was not tried. In headless Chrome (not signed in to Google), the embedded frame shows Google's sign-in page, so rendering inside the frame remains unverified until checked in a signed-in browser.

## Chat surfaces: MCP App and A2UI cards

Two ways to show the analysis inside a chat client. Both read the result of the same deterministic tool the agent calls. Neither changes the agent: it keeps its ten tools, and its evaluation is unaffected.

| | MCP App | A2UI card |
| :--- | :--- | :--- |
| What the client receives | One HTML document and a tool result | JSON messages that name catalog components |
| Who draws it | The document, inside the host's sandboxed frame | The client, with its own widgets |
| What it shows | The ripple graph, book and scenario controls, optional Google Sheet sync actions, and the filing passage behind each issuer and link | A holding card or a book summary card. Links are rows: the basic catalog has no graph component |
| Code | `scripts/mcp_server.py`, `scripts/ripple_view.py`, `assets/mcp-app/ripple.html` | `scripts/a2ui_cards.py` |

**MCP App (`stdio` and Streamable HTTP).** `scripts/mcp_server.py` defaults to `stdio` (`plugins/fsi/mcp.json`) with one read-only tool for the model (`show_ripple_graph`), one app-only tool (`recompute_ripple_graph`), and one `ui://` resource. Passing `--transport streamable-http --host 0.0.0.0 --port 8080 --full-suite` (packaged in `assets/export-template/deploy_mcp_cloudrun.sh`) serves `POST /mcp` on private Cloud Run for Gemini Enterprise Custom MCP Server connectors and adds Form 10-K lookup, filing-delta, and Google Sheet sync tools:

```bash
uv sync --extra mcp                                                                          # once
uv run --extra mcp python scripts/mcp_server.py                                              # stdio (plugins/fsi/mcp.json)
uv run --extra mcp python scripts/mcp_server.py --transport streamable-http --full-suite     # Streamable HTTP (/mcp)
```

In `stdio` mode the server reads the bundled book only. The model that calls the MCP server is the host's model, not this skill's ADK agent, so `EVAL.yaml` says nothing about what that model writes. For that reason `show_ripple_graph`'s text result carries the figures and no filing passage, and tells the model not to quote from memory; the passages are in the view.

**A2UI cards.** `scripts/a2ui_cards.py` builds A2UI v0.9 messages for the basic catalog in Python. No model writes the JSON.

```bash
python3 scripts/a2ui_cards.py --card holding --ticker UAL                                     # JSON Lines, one message per line
python3 scripts/a2ui_cards.py --card book --book aero_transport --rate-bps 250 --oil-pct 30
```

**Preview without a chat client.** Run `make demo` and open `http://127.0.0.1:8080/chat-surfaces.html`. The page plays the host side of the MCP Apps protocol for the view (a sandboxed frame under the default no-network content security policy) and draws the cards with a small renderer of its own. It is a stand-in for a client, not one.

The page is laid out as a chat: an example request, then the view; a second request, then the card. The frame is capped at 800 px tall, as a host caps a view it shows inline. When a control inside the view calls the tool, the request above it stays as it was, and a line under the view counts those calls and shows the last one. The card is built for the book and scenario the view shows. A section that starts closed, "Developer view: what goes over the wire", holds the text the model receives, the messages between host and view, and the card's JSON; `?dev=1` in the address opens it.

## Full Setup on Google Cloud

```bash
python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets                          # dry run, prints the DDL
python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets --execute                # dataset, tables, property graph
python3 scripts/setup.py --project "$GOOGLE_CLOUD_PROJECT" --dataset fsi_capital_markets --execute --load-sample  # also reloads the two reference tables from the bundled book
```

`--load-sample` deletes and reloads `sp500_companies` and `supply_chain_edges`. It never writes to `eval_runs`, `decision_ledger` or `signoff_refusals`.

## Deploy the workstation to Cloud Run (private)

```bash
python3 scripts/export_app.py --output-dir ./dist/capital-markets-app
cd ./dist/capital-markets-app
GOOGLE_CLOUD_PROJECT=<project> bash deploy_cloudrun.sh
gcloud run services proxy fsi-capital-markets-analyst --project <project> --region us-central1 --port 8080
```

The script deploys one service that only IAM-authorised callers can open. It runs as a dedicated service account that holds `roles/aiplatform.user` and nothing else. The settings are listed at the top of `deploy_cloudrun.sh`; the Gemini location (`GEMINI_LOCATION`, default `global`) is separate from the Cloud Run region (`FSI_RUN_REGION`).

- **The image installs the versions in `uv.lock`**, so "Run live agent eval" works in the service. One evaluation is a single request of about five and a half minutes; the script sets a 15-minute request timeout.
- **State lives in the instance.** A new instance holds only the offline tier, so run the live evaluation once after each deployment. The evaluation result and the sign-off log are lost when the instance is replaced. The script keeps one instance running (`FSI_MIN_INSTANCES=1`) for that reason, which costs money while the service exists.
- **Opening it.** A browser cannot send the identity token Cloud Run asks for, so open the service through a local proxy and browse to `http://localhost:8080`. `gcloud run services proxy` is the standard one. Where its `cloud-run-proxy` component cannot be installed (a package-managed gcloud, a locked-down laptop), `python3 scripts/cloudrun_proxy.py --project <project> --port 8080` does the same job with the standard library and the gcloud that is already signed in; it listens on the loopback interface only and keeps the token in memory. Project owners and editors may invoke the service; anyone else needs `roles/run.invoker` on it.
- **If the build fails with "does not have storage.objects.get access"**, the project's default build identity has no roles. Set `FSI_BUILD_SA=<name>`: the script then creates a build service account with `roles/run.builder`.
- **Not in the service:** the chat page (`make chat`) and the MCP server.

## Current limits

- Market-risk sensitivities, disclosed links and the filing watcher cover the 20 issuers in the bundled book, and no filing text is fetched at run time: `scripts/build_sample_book.py` reads SEC EDGAR when the book is built, and adding an issuer means adding its passages there. Reported figures cover those 20 offline, and any other Form 10-K filer under US GAAP when live lookups are on.
- Reported figures are annual and are what the company tagged in XBRL. A value comes from the most recent Form 10-K that SEC company facts holds; when SEC has not yet added a new filing, the latest year shown is the one before.
- The evaluation runs with SEC lookups off. The live lookup is covered by unit tests with a stand-in for SEC and by a manual check of six companies on 2026-11-22, not by the evaluation gate.
- The second-model review is a model's opinion, measured on planted errors and planted statements only (see [Second-model review](#second-model-review)). It adds a model call to every chat answer that used a tool. A statement it lists is not removed from the answer.
- The filing watcher compares two periods reported in one filing. It does not compare two filings.
- The Google Sheet link writes the scenario model and the reported-figures tabs, into one Sheet that a person has created and shared. Its unit tests use an in-memory spreadsheet, and it was run against Google Sheets once (see [Google Sheet](#google-sheet-optional)). The `.xlsx` exports write live formulas, which the tests recompute with a small formula evaluator and not with a spreadsheet application. A separate formula library recalculated the reported-figures workbook for all 16 bundled issuers once, on 2026-11-23, with a script that is not part of this skill: 813 of 813 formulas gave the figure computed in Python.
- An evaluation run is tied to the dataset it ran on, not to the agent instruction or the code.
- `scripts/server.py` serves the workstation and does not host the agent as a service: the agent runs in it only during a live evaluation. The Cloud Run template deploys this workstation. It has been deployed once, as a private service, and one live evaluation was run in it; it has not been load-tested.
- Scanned PDFs are detected and refused; OCR is not implemented.
- The MCP App (`scripts/mcp_server.py`) runs over `stdio` by default and over Streamable HTTP (`--transport streamable-http --full-suite`) on private Cloud Run (`deploy_mcp_cloudrun.sh`). It was verified live in Gemini Enterprise on a Google Cloud sandbox project on 2026-11-23 (`show_ripple_graph`, `recompute_ripple_graph`, and `push_scenario_from_widget` writing 110 reconciled cells to Google Sheets). A client that does not implement MCP Apps receives the tool's text summary only.
- The A2UI messages validate against the published v0.9.1 schemas (`tests/fixtures/a2ui_v0_9/`); no A2UI client has rendered them, and nothing sends them: the skill has no A2A server, and the agent does not call `scripts/a2ui_cards.py`.
- A2UI `Text` is rendered as Markdown, so the cards backslash-escape Markdown characters to keep a passage exact. A client that shows the raw string instead would show those backslashes.

## Safe teardown

```bash
python3 scripts/cleanup.py --dry-run
```

Audit tables (`decision_ledger`, `signoff_refusals`, `eval_runs`) and retention-protected buckets are kept unless `--include-records` and `--confirm-project <PROJECT_ID>` are both passed.
