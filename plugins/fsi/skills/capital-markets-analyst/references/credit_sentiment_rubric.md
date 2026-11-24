# Fixed-Income Credit Sentiment & Fundamental Trajectory Rubric

## 4-Dimension Scoring Framework (`1` to `5` Integer Scale)
An analyst scores each issuer on four credit dimensions, anchored in its `10-K` / `10-Q` disclosures. The scores are analyst judgments, not filing facts. `scripts/filing_compare.py` does not produce them: it reports the four scores and computes their composite only when the dataset supplies them (`yoy_delta.credit_rubric`). The bundled book is built from filings and carries none, so the watcher reports every issuer as not scored.

| Dimension | Score `1–2` (Deteriorating / Elevated Risk) | Score `3` (Neutral / Balanced) | Score `4–5` (Improving / Fortress) |
| :--- | :--- | :--- | :--- |
| **1. Leverage Trajectory** | Net debt rising faster than operating cash flow; debt-funded Working Capital deficits | Flat net leverage; refinancing maturities par-for-par | Active gross debt paydown; net leverage `< 2.0x` |
| **2. Liquidity Headroom** | Cash + undrawn revolver `< 1.2x` near-term maturities & capex commitments | Adequate revolver cushion (`1.2x–2.5x` 24-month obligations) | Substantial unrestricted cash + multi-year undrawn syndicate revolver |
| **3. Margin Resilience** | Unhedged input cost spikes with weak contractual pass-through | Partial surcharge / pricing offset with 1–2 quarter lag | High contractual backlog (`RPO`), software/services mix, or disciplined hedging |
| **4. Covenant & Maturity Risk** | Near-term maturity wall (`< 18m`) or tight springing coverage ratios | Staggered maturities; comfortable covenant headroom | Investment-grade unsecured ladder with zero financial maintenance covenants |

## Composite Credit Stance Mapping (Non-Advisory)
- Composite Average `>= 4.25`: `Strong Investment Grade`
- Composite Average `3.50 – 4.24`: `Improving Credit Profile` / `Stable`
- Composite Average `2.75 – 3.49`: `Stable / Selective Watch`
- Composite Average `< 2.75`: `Watch / Negative Bias`

> **Compliance Note:** Credit stance labels summarize historical and disclosed balance-sheet trajectory only. They are **not** credit ratings or securities `buy / sell / hold` recommendations.
