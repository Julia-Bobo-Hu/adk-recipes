# Form 10-K `Item 7A` Market-Risk Sensitivity & XBRL EPS Rules

## 1. Extraction Rules
1. **Locate Quantitative Disclosures First:** Search SEC Form 10-K `Item 7A` (*Quantitative and Qualitative Disclosures About Market Risk*) and the debt and derivative footnotes. Some issuers put the disclosure elsewhere (in MD&A, or in an exhibit incorporated by reference); cite the section the passage actually sits under.
2. **Give every disclosure a status.** Only the first converts to earnings:

   | Status | Meaning | In the bundled book (rates) |
   | :--- | :--- | :--- |
   | `income_statement` | States an annual pre-tax income or expense effect for a stated move | `DAL`, `UAL`, `WMT`, `AAPL`, `JPM`, `INTC` |
   | `fair_value_only` | States only a change in the fair value of debt or investments | `FDX`, `MSFT`, `NVDA`, `RTX` |
   | `not_quantified` | Says the effect is not material, or gives no amount | `BA`, `HXL`, `PG`, `AMZN`, `CAT`, `QRVO`, `SWKS`, `SMCI` |
   | `not_convertible` | Gives an amount that cannot be signed or annualised as stated | `GE` |
   | `incorporated_by_reference` | Points to an exhibit that is not in the primary document | `CLX` |
   | `none_found` | No passage was located (for fuel: no sentence matched the build-time keyword screen) | none for rates; 15 issuers for fuel |

3. **Never** put a fair-value or balance-sheet sensitivity into an income-statement EPS column. A record whose status is not `income_statement` carries no `pre_tax_impact_m`; it carries the quoted passage and a `non_comparable_flag` (or, for `none_found`, a `coverage_note`) that says why. Its modelled impact is zero, and that zero means "no figure to convert", not "no exposure".
4. **Sign Convention:** a positive `pre_tax_impact_m` means pre-tax income rises when the risk factor rises (`+bps` or `+%`). In the bundled book `UAL` and `JPM` gain when rates rise, `DAL`, `WMT`, `AAPL` and `INTC` lose, and both airlines lose when fuel prices rise.
5. **Say when a figure is derived.** Three bundled figures are computed from quoted ones and carry a `derivation` note:
   - `UAL` rates: the filing states more interest income and more interest expense separately; the net of the two is not stated in the filing.
   - `DAL` fuel: stated per one cent per gallon; converted to a percent move with the average price per gallon in the same filing.
   - `UAL` fuel: stated per one dollar per barrel; converted with the average price per gallon in the same filing and 42 gallons per barrel.

## 2. Deterministic `Decimal` Scaling & XBRL EPS Conversion
All calculations execute in `scripts/fsi_engine.py` using `decimal.Decimal` (`ROUND_HALF_UP`):

$$\text{Scaled Pre-Tax Impact (\$M)} = \text{Disclosed Pre-Tax Impact} \times \left(\frac{\text{Scenario Shock}}{\text{Disclosed Basis}}\right)$$

$$\text{After-Tax Impact (\$M)} = \text{Scaled Pre-Tax Impact} \times (1 - \text{Effective Tax Rate}_{\text{XBRL}})$$

$$\Delta \text{Diluted EPS (\$/sh)} = \frac{\text{After-Tax Impact (\$M)}}{\text{Weighted-Average Diluted Shares (M)}_{\text{XBRL}}}$$

The effective tax rate and the diluted share count are the XBRL facts of the same filing (`us-gaap:EffectiveIncomeTaxRateContinuingOperations`, `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`). The scaling is linear. It ignores hedges that reset, pass-through to customers and any change in volumes.

**When the filed tax rate is not usable.** The effective tax rate is the year's average rate, and it stands in for the marginal rate only in an ordinary year. When the XBRL rate is below 0% or above 50% (a loss or near-breakeven year, a valuation allowance, a one-off item), `fsi_engine.conversion_tax_rate` does not apply it. The after-tax impact and EPS then use the US federal statutory rate of 21%. The holding says so in `conversion_notes` and in `scaled_impact.tax_rate_basis` (`us_federal_statutory_rate`), and the workbook says so in its citation column; `xbrl.effective_tax_rate` still reports the filed rate. The statutory rate is an assumption of this tool, not a figure from the filing: an issuer with a valuation allowance may get no tax benefit at all from a further loss. In the bundled book this applies to `INTC`, whose filed rate is 98.3% (income tax of USD 1,531 million on pre-tax income of USD 1,557 million).

**What a book total adds up.** A total is the sum of the holdings whose filing states a figure that converts to earnings. A holding without one adds nothing: that is the absence of a disclosed figure, not zero exposure. Every surface that shows a total therefore says how many holdings it covers ("5 of 12 holdings"), and the others are listed with the reason.

**EPS as a percentage of fiscal-year EPS** is reported only when reported diluted EPS is positive. A percentage of a zero or negative base is not meaningful, so `eps_pct_of_fy` is then null and a note says why.

## 3. Mandatory `> 2x` Linear-Extrapolation Warning
`Item 7A` disclosures describe a stated hypothetical move with everything else held constant. Whenever $|\text{Scenario Shock}| > 2 \times |\text{Disclosed Basis}|$ (for example `+250 bp` on a `100 bp` basis, or `+25%` oil against a one-cent-per-gallon basis), emit `extrapolation_warning = True` so analysts review convexity, cap strikes and fuel-surcharge pass-throughs. A fuel sensitivity stated per cent or per dollar is a small basis, so almost any percent scenario exceeds it and is flagged.
