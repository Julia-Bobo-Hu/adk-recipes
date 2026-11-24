# Deal-Desk Pitchbook & Investment Committee Memo Tie-Out

## Source-Posture Hierarchy
Institutional teams frequently review third-party management presentations, sell-side decks, or internal draft memos alongside statutory SEC filings. `fsi-capital-markets-analyst` labels every figure with a source posture. The posture of the bundled book is read from the dataset; an uploaded document is labelled `customer_submitted_unverified` by `fsi_engine.inspect_pdf_posture`.

| Source Posture | Examples | Permissible Role |
| :--- | :--- | :--- |
| `authoritative_filing` | SEC EDGAR `10-K`, `10-Q`, `8-K`, `us-gaap` XBRL `companyfacts` | Primary source of truth for historical revenue, GAAP EPS, tax rates, diluted shares, debt balances, and `Item 7A` sensitivities. |
| `illustrative_sample` | Authored or sample text that was not fetched from a filing | Demonstration only; never attributed to an issuer as its statement. The bundled book does not use it. |
| `customer_submitted_unverified` | Uploaded pitchbooks, management "Adjusted EBITDA" bridges, draft IC memos | Supplemental comparison only; **never** overwrites `authoritative_filing` cells without explicit variance disclosure. |

## Tie-Out Checklist
This is a manual procedure. No script reconciles an uploaded memo against XBRL, and the agent cannot read an uploaded file; only step 3 is implemented (`inspect_pdf_posture`).

1. Reconcile headline revenue, diluted EPS, and total debt in any uploaded memo against `us-gaap` XBRL within `$0.1M` / `$0.01/sh`.
2. Flag non-GAAP adjustments (stock-based compensation, restructuring add-backs) when presented without the corresponding GAAP base figure.
3. If an uploaded PDF is a rasterized scan with zero text layer in offline Quick Start mode, return `unassessable_offline` rather than OCR-guessing digits.
