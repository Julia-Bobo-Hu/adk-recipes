# Period-Over-Period Filing Delta Audit (`the-watcher`)

## What `scripts/filing_compare.py` does today
For each issuer in a book it reports **one** disclosed figure at two periods and renders the result as `the-watcher.md`. Both values come from the issuer's current Form 10-K. The `basis` field says how:

| `basis` | Where the two values come from | In the bundled book |
| :--- | :--- | :--- |
| `filing_passage` | One quoted passage of the current filing states both periods. The passage is returned with its `doc_id`, section and excerpt hash. | 11 issuers |
| `xbrl` | Long-term debt at the two balance-sheet dates reported in the current filing. The XBRL tag is named; there is no quotation. | 9 issuers |

It does not fetch or compare a prior filing, and it does not read `10-Q` or `8-K` filings.

Credit rubric scores are reported only when the dataset supplies them (see `credit_sentiment_rubric.md`). The bundled book is built from filings and carries none, so every row reads "not scored".

## Method for a fuller audit (not implemented)
The sections below are the order in which an analyst would compare two consecutive filings by hand. No script performs them.

1. **`Item 7A` hedge and floating-rate mix:** shifts from floating-rate payer to net cash yield beneficiary, or termination of fuel or FX hedges.
2. **`Item 1A` risk factor additions and escalations:** newly inserted paragraphs, or language that moves from hypothetical ("may adversely affect") to realised ("has constrained").
3. **Major customer and supplier footnotes (`ASC 280`, `>= 10%` of revenue):** year-over-year changes in single-customer revenue share, which would also update `supply_chain_edges`.
4. **Commitments, revolver covenants and off-balance-sheet obligations:** purchase commitment schedules, undrawn revolver availability and maturity walls.

## Provenance Standard
A row with `basis = filing_passage` cites `current_doc_id`, the section and a verbatim passage that states both periods. A row with `basis = xbrl` names the tag and the two period ends. A comparison across two filings would have to cite both `prior_doc_id` and `current_doc_id`; until that exists, `prior_doc_id` is empty.
