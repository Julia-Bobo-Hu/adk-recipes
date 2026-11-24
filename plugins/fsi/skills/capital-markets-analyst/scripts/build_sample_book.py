"""Build the bundled sample book from SEC EDGAR 10-K filings.

Every passage in ``assets/sample_10k_book/sp500_sample_book.json`` is taken from a filing
fetched from sec.gov and checked here: a sentence must be an exact substring of the filing
text, a table row must match one parsed row, and each must sit under the heading it is
labelled with. XBRL facts are read for the pinned accession and the period of report only.

Run from the skill directory::

    SEC_EDGAR_USER_AGENT="Your Organisation contact@your-domain" \\
        python scripts/build_sample_book.py --cache-dir ./.edgar_cache

    python scripts/build_sample_book.py --cache-dir ./.edgar_cache --offline --check

``--check`` rebuilds in memory and fails if the result differs from the bundled file
(retrieval timestamps aside). Nothing in this module runs at agent runtime.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import edgar_source, fsi_engine  # noqa: E402

DATASET_ID = "form10k_fine_print_edgar_20261120"

S7A = "Item 7A. Quantitative and Qualitative Disclosures About Market Risk"
H7A_UPPER = "ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK"
H7A_TITLE = "Item 7A. Quantitative and Qualitative Disclosures About Market Risk"

NONE_FOUND_SECTION = "No passage located"
OIL_NONE_FOUND_NOTE = (
    "The build-time screen found no sentence in this filing that states a dollar sensitivity "
    "to oil or fuel prices."
)
FUEL_SCREEN_NOTE = (
    "Oil status 'none_found' means no sentence of the filing text matched all of: a fuel term "
    "(fuel, crude, oil, petroleum, gasoline, diesel); a dollar amount; would, could or "
    "hypothetical; price, cost, per gallon or per barrel; and increase, decrease or change. It "
    "does not mean the filing never mentions fuel."
)
ANNOTATION_NOTE = (
    "Book groupings, scenario presets, sector labels, relationship labels, disclosure statuses "
    "and the notes attached to them are annotations by the skill authors, not filing content."
)
DATA_NOTICE = (
    "Passages and XBRL facts come from each issuer's Form 10-K as retrieved from SEC EDGAR; "
    "the accession number, URL and SHA-256 of every source document are in the dataset. "
    "Scenario presets, book groupings, labels, statuses and notes are annotations, and values "
    "marked as derived are computed from the quoted figures. Not investment advice."
)

_FUEL_TERM = re.compile(r"(?i)\b(?:fuel|crude|oil|petroleum|gasoline|diesel)\b")
_MONEY = re.compile(r"\$\s?[\d,.]+")
_CONDITIONAL = re.compile(r"(?i)\bwould\b|\bcould\b|hypothetical")
_PRICE = re.compile(r"(?i)price|cost|per gallon|per barrel")
_CHANGE = re.compile(r"(?i)increase|decrease|change")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u201c\"(])")


class BuildError(Exception):
    """A spec does not match the filing it cites."""


def fuel_sensitivity_sentences(text: str) -> list[str]:
    """Sentences that state a dollar sensitivity to oil or fuel prices (see FUEL_SCREEN_NOTE)."""
    return [
        sentence
        for sentence in _SENTENCE_SPLIT.split(text)
        if _FUEL_TERM.search(sentence)
        and _MONEY.search(sentence)
        and _CONDITIONAL.search(sentence)
        and _PRICE.search(sentence)
        and _CHANGE.search(sentence)
    ]


def sent(text: str, section: str, *anchors: tuple[str, int], not_between: tuple[str, ...] = ()) -> dict[str, Any]:
    """Spec for a sentence passage that must follow each ``(heading, max_gap)`` anchor."""
    return {"kind": "sentence", "text": text, "section": section, "anchors": anchors, "not_between": not_between}


def row(cells: list[str], section: str, *anchors: tuple[str, int], not_between: tuple[str, ...] = ()) -> dict[str, Any]:
    """Spec for a table-row passage, matched on the given cell texts in order."""
    return {"kind": "table_row", "cells": cells, "section": section, "anchors": anchors, "not_between": not_between}


ISSUERS: dict[str, dict[str, str]] = {
    "DAL": {"name": "Delta Air Lines, Inc.", "sector": "Airlines", "cik": "0000027904", "accession": "0000027904-26-000013"},
    "UAL": {"name": "United Airlines Holdings, Inc.", "sector": "Airlines", "cik": "0000100517", "accession": "0000100517-26-000023"},
    "BA": {"name": "The Boeing Company", "sector": "Aerospace & Defense", "cik": "0000012927", "accession": "0001628280-26-004357"},
    "GE": {"name": "GE Aerospace", "sector": "Aerospace & Defense", "cik": "0000040545", "accession": "0000040545-26-000008"},
    "HXL": {"name": "Hexcel Corporation", "sector": "Aerospace Materials", "cik": "0000717605", "accession": "0001193125-26-046377"},
    "FDX": {"name": "FedEx Corporation", "sector": "Air Freight & Logistics", "cik": "0001048911", "accession": "0001048911-26-000105"},
    "WMT": {"name": "Walmart Inc.", "sector": "Consumer Staples Retail", "cik": "0000104169", "accession": "0000104169-26-000055"},
    "CLX": {"name": "The Clorox Company", "sector": "Household Products", "cik": "0000021076", "accession": "0000021076-26-000034"},
    "PG": {"name": "The Procter & Gamble Company", "sector": "Household & Personal Products", "cik": "0000080424", "accession": "0000080424-26-000103"},
    "AAPL": {"name": "Apple Inc.", "sector": "Technology Hardware", "cik": "0000320193", "accession": "0000320193-26-000026"},
    "MSFT": {"name": "Microsoft Corporation", "sector": "Systems & Cloud Software", "cik": "0000789019", "accession": "0001193125-26-323660"},
    "JPM": {"name": "JPMorgan Chase & Co.", "sector": "Diversified Banks", "cik": "0000019617", "accession": "0001628280-26-008131"},
    "NVDA": {"name": "NVIDIA Corporation", "sector": "Semiconductors", "cik": "0001045810", "accession": "0001045810-26-000021"},
    "AMZN": {"name": "Amazon.com, Inc.", "sector": "Cloud & E-Commerce", "cik": "0001018724", "accession": "0001018724-26-000004"},
    "RTX": {"name": "RTX Corporation", "sector": "Aerospace & Defense", "cik": "0000101829", "accession": "0000101829-26-000006"},
    "CAT": {"name": "Caterpillar Inc.", "sector": "Machinery", "cik": "0000018230", "accession": "0000018230-26-000008"},
    "QRVO": {"name": "Qorvo, Inc.", "sector": "Semiconductors", "cik": "0001604778", "accession": "0001628280-26-032873"},
    "SWKS": {"name": "Skyworks Solutions, Inc.", "sector": "Semiconductors", "cik": "0000004127", "accession": "0000004127-26-000055"},
    "INTC": {"name": "Intel Corporation", "sector": "Semiconductors", "cik": "0000050863", "accession": "0000050863-26-000011"},
    "SMCI": {"name": "Super Micro Computer, Inc.", "sector": "AI Servers & Storage", "cik": "0001375365", "accession": "0001375365-26-000022"},
}

BOOKS: dict[str, dict[str, Any]] = {
    "next27_flagship": {
        "label": "Next '27 Sample Book (12 Holdings)",
        "tickers": ["DAL", "UAL", "BA", "GE", "HXL", "FDX", "WMT", "CLX", "PG", "AAPL", "MSFT", "JPM"],
    },
    "aero_transport": {
        "label": "Travel, Aerospace & Industrials",
        "tickers": ["DAL", "UAL", "BA", "GE", "HXL", "FDX", "RTX", "CAT"],
    },
    "megacap_tech": {"label": "Mega-Cap Tech & AI Supply Chain", "tickers": ["AAPL", "MSFT", "NVDA", "AMZN", "INTC"]},
    "defensive_consumer": {"label": "Defensive Consumer & Staples", "tickers": ["WMT", "PG", "CLX"]},
}

# Scenario presets are assumptions chosen by the skill authors. They carry no probability.
SCENARIOS: list[dict[str, str]] = [
    {"id": "fed_up_25_oil_flat", "label": "Rates +25 bp \u00b7 Oil unchanged", "rate_bps": "25", "oil_pct": "0"},
    {"id": "fed_up_50_oil_spike", "label": "Rates +50 bp \u00b7 Oil +25%", "rate_bps": "50", "oil_pct": "25"},
    {"id": "fed_cut_50_oil_drop", "label": "Rates -50 bp \u00b7 Oil -20%", "rate_bps": "-50", "oil_pct": "-20"},
    {
        "id": "extreme_rates_250_stress",
        "label": "Stress: Rates +250 bp \u00b7 Oil +30% (beyond 2x the disclosed basis)",
        "rate_bps": "250",
        "oil_pct": "30",
    },
]

_OIL_NONE: dict[str, Any] = {"status": "none_found", "coverage_note": OIL_NONE_FOUND_NOTE}

_DAL_7A = (H7A_UPPER, 4000)
_DAL_ITEM1 = ("ITEM 1. BUSINESS", 30000)
_UAL_7A = (H7A_UPPER, 5000)
_FDX_7A = (H7A_UPPER, 6000)
_GE_RISK = (("CAPITAL RESOURCES AND LIQUIDITY", 8000), ("FOREIGN EXCHANGE AND INTEREST RATE RISKS.", 1800))
_GE_RISK_LABEL = "Capital Resources and Liquidity (Foreign Exchange and Interest Rate Risks)"
_HXL_RISK = (("Market Risks As a result of our global operating and financing activities", 2500),)
_HXL_RISK_LABEL = "Management's Discussion and Analysis (Market Risks)"
_JPM_EAR = (
    ("Structural interest rate risk management", 9000),
    ("Earnings-at-risk One way that the Firm evaluates its structural interest rate risk", 6000),
)
_JPM_EAR_LABEL = "Structural interest rate risk management (Earnings-at-risk)"
_CAT_SENS = (("Interest Rate Sensitivity For our MP&E operations", 2500),)
_CAT_SENS_LABEL = "Other Matters (Interest Rate Sensitivity)"
_CLX_7A = ("ITEM 7.A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK", 400)
_CLX_BY_REFERENCE = sent(
    "This information appears under \u201cQuantitative and Qualitative Disclosures about Market Risk\u201d in "
    "\u201cManagement\u2019s Discussion and Analysis of Financial Condition and Results of Operations,\u201d in "
    "Exhibit 99.1, which is incorporated herein by reference.",
    S7A,
    _CLX_7A,
)
_CLX_BY_REFERENCE_FLAG = (
    "Item 7A is incorporated by reference to Exhibit 99.1, which is not part of the bundled "
    "primary document, so no sensitivity is read from it."
)

SENSITIVITIES: dict[str, dict[str, dict[str, Any]]] = {
    "DAL": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "-11",
            "passages": [
                sent(
                    "An increase of 100 basis points in average annual interest rates would have decreased the "
                    "estimated fair value of our fixed-rate debt by $470 million at December 31, 2025 and would "
                    "have increased the annual interest expense on our variable-rate debt and variable-rate "
                    "leases by $11 million.",
                    S7A,
                    _DAL_7A,
                )
            ],
            "expect": ["100 basis points", "$11 million"],
            "basis_note": (
                "The same sentence states a $470 million decrease in the fair value of fixed-rate debt. "
                "That is a fair-value effect and is not converted."
            ),
        },
        "oil": {
            "status": "income_statement",
            "disclosed_basis_unit": "usd_per_gallon",
            "disclosed_basis_value": "0.01",
            "reference_price_usd_per_gallon": "2.30",
            "pre_tax_impact_m": "-40",
            "passages": [
                sent(
                    "A one cent increase in the cost of jet fuel per gallon would result in approximately "
                    "$40 million of additional annual fuel expense based on annual consumption of approximately "
                    "four billion gallons of jet fuel.",
                    S7A,
                    _DAL_7A,
                ),
                sent(
                    "The following table shows our aircraft fuel consumption and costs:",
                    "Item 1. Business",
                    _DAL_ITEM1,
                    not_between=("ITEM 1A. RISK FACTORS",),
                ),
                row(
                    ["Year", "Average Price Per Gallon(1)(2)"],
                    "Item 1. Business",
                    _DAL_ITEM1,
                    not_between=("ITEM 1A. RISK FACTORS",),
                ),
                row(
                    ["2025", "4,269", "$9,819", "$2.30", "17%"],
                    "Item 1. Business",
                    _DAL_ITEM1,
                    not_between=("ITEM 1A. RISK FACTORS",),
                ),
                sent(
                    "(2)Includes the impact of fuel hedge activity and refinery segment results.",
                    "Item 1. Business",
                    _DAL_ITEM1,
                    not_between=("ITEM 1A. RISK FACTORS",),
                ),
            ],
            "expect": ["one cent", "$40 million", "$2.30"],
            "derivation": (
                "Derived basis. The filing states the sensitivity per one cent per gallon. A percent "
                "price move is converted with the 2025 average price per gallon in the same filing "
                "($2.30, which includes fuel hedge activity and refinery segment results), so a 1% move "
                "is 2.30 cents per gallon."
            ),
        },
    },
    "UAL": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "77",
            "components": [
                {"label": "Interest income on cash, cash equivalents and short-term investments", "amount_m": "140"},
                {"label": "Projected interest expense on variable rate debt", "amount_m": "-63"},
            ],
            "passages": [
                sent(
                    "Assuming our cash, cash equivalents and short-term investments remain at their average 2025 "
                    "levels, a 100 basis point increase in interest rates would result in a corresponding increase "
                    "in the Company's interest income of approximately $140 million during 2026.",
                    S7A,
                    _UAL_7A,
                ),
                sent(
                    "The following table summarizes information related to the Company's interest rate market "
                    "risk at December 31, 2025 (in millions):",
                    S7A,
                    _UAL_7A,
                ),
                row(
                    ["Impact of 100 basis point increase on projected interest expense for the following year", "63"],
                    S7A,
                    _UAL_7A,
                ),
            ],
            "expect": ["$140 million", "100 basis point", " | 63"],
            "derivation": (
                "Derived net figure. The filing gives two separate effects of a 100 basis point increase: "
                "about $140 million more interest income and $63 million more interest expense on variable "
                "rate debt. The net of the two, $77 million, is not stated in the filing."
            ),
        },
        "oil": {
            "status": "income_statement",
            "disclosed_basis_unit": "usd_per_barrel",
            "disclosed_basis_value": "1",
            "reference_price_usd_per_gallon": "2.44",
            "pre_tax_impact_m": "-116",
            "passages": [
                sent(
                    "A one-dollar change in the price of a barrel of aircraft fuel would change the Company's 2026 "
                    "projected fuel expense by approximately $116 million.",
                    S7A,
                    _UAL_7A,
                ),
                sent(
                    "The table below summarizes the fuel consumption and expense of UAL's aircraft (including the "
                    "operations of our regional carriers operating under CPAs) during the last three years.",
                    "Item 1. Business (Aircraft Fuel)",
                    ("Aircraft Fuel.", 100),
                ),
                row(
                    ["Year", "Average Price Per Gallon"],
                    "Item 1. Business (Aircraft Fuel)",
                    ("Aircraft Fuel.", 600),
                ),
                row(
                    ["2025", "4,663", "$11,396", "$2.44", "21%"],
                    "Item 1. Business (Aircraft Fuel)",
                    ("Aircraft Fuel.", 800),
                ),
            ],
            "expect": ["one-dollar", "$116 million", "$2.44"],
            "derivation": (
                "Derived basis. The filing states the sensitivity per one dollar per barrel and a symmetric "
                "change; a price increase is taken as an expense increase. A percent price move is converted "
                "with the 2025 average price per gallon in the same filing ($2.44) and 42 gallons per barrel, "
                "so a 1% move is about $1.02 per barrel."
            ),
        },
    },
    "BA": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "We have financial instruments that are subject to interest rate risk, principally fixed-rate "
                    "debt obligations. The investors in our fixed-rate debt obligations do not generally have the "
                    "right to demand we pay off these obligations prior to maturity. Therefore, exposure to "
                    "interest rate risk is not believed to be material for our fixed-rate debt.",
                    S7A,
                    (H7A_TITLE, 400),
                )
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that exposure to interest rate risk is "
                "not believed to be material for its fixed-rate debt."
            ),
        },
        "oil": _OIL_NONE,
    },
    "GE": {
        "rate": {
            "status": "not_convertible",
            "passages": [
                sent(
                    "The analyses indicated that our 2025 consolidated net income would be impacted by $0.1 billion "
                    "for interest rate risk and insignificantly for foreign exchange risk.",
                    _GE_RISK_LABEL,
                    *_GE_RISK,
                ),
                sent(
                    "To assess exposure to interest rate risk, we apply a +/- 100 basis points change in interest "
                    "rates and keep that in place for the next 12 months.",
                    _GE_RISK_LABEL,
                    *_GE_RISK,
                ),
            ],
            "expect": ["$0.1 billion", "+/- 100 basis points"],
            "non_comparable_flag": (
                "The filing states an unsigned $0.1 billion effect on consolidated net income for a +/- 100 "
                "basis point change. It gives no direction and no pre-tax amount, so it is not converted."
            ),
        },
        "oil": _OIL_NONE,
    },
    "HXL": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "Assuming a 10% favorable and a 10% unfavorable change in the underlying weighted average "
                    "interest rates of our variable rate debt and swap agreements, interest expense for 2025 of "
                    "$37.7 million would not be materially impacted.",
                    _HXL_RISK_LABEL,
                    *_HXL_RISK,
                )
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that a 10% change in its variable interest "
                "rates would not materially affect interest expense."
            ),
        },
        "oil": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "In addition, we have several contracts with both suppliers and customers that contain "
                    "pricing adjustments based on the price of oil outside of a specified band.",
                    _HXL_RISK_LABEL,
                    *_HXL_RISK,
                )
            ],
            "non_comparable_flag": (
                "The filing mentions contract pricing adjustments tied to the price of oil and gives no dollar sensitivity."
            ),
        },
    },
    "FDX": {
        "rate": {
            "status": "fair_value_only",
            "passages": [
                sent(
                    "Market risk for long-term debt is estimated as the potential decrease in fair value resulting "
                    "from a hypothetical 10% increase in interest rates and amounts to approximately $694 million "
                    "as of May 31, 2026 and approximately $600 million as of May 31, 2025.",
                    S7A,
                    _FDX_7A,
                ),
                sent(
                    "While we currently have market risk sensitive instruments related to interest rates, we do "
                    "not have significant exposure to changing interest rates on our long-term debt.",
                    S7A,
                    _FDX_7A,
                ),
            ],
            "expect": ["$694 million", "$600 million", "fair value"],
            "non_comparable_flag": (
                "The filing states a fair-value sensitivity of long-term debt (about $694 million for a "
                "hypothetical 10% increase in interest rates), not an annual income effect."
            ),
        },
        "oil": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "While we have market risk for changes in the price of jet and vehicle fuel, this risk is "
                    "largely mitigated by our indexed fuel surcharges.",
                    S7A,
                    _FDX_7A,
                )
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that fuel price risk is largely mitigated "
                "by indexed fuel surcharges."
            ),
        },
    },
    "WMT": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "-100",
            "passages": [
                sent(
                    "Based on January 31, 2026 debt levels, a 100 basis point change in prevailing market rates "
                    "would cause our annual interest costs to change by approximately $0.1 billion.",
                    S7A,
                    (H7A_UPPER, 4000),
                )
            ],
            "expect": ["100 basis point", "$0.1 billion"],
            "basis_note": (
                "The filing states a symmetric change rounded to $0.1 billion; higher rates are taken as "
                "higher interest costs."
            ),
        },
        "oil": _OIL_NONE,
    },
    "CLX": {
        "rate": {
            "status": "incorporated_by_reference",
            "passages": [_CLX_BY_REFERENCE],
            "non_comparable_flag": _CLX_BY_REFERENCE_FLAG,
        },
        "oil": {
            "status": "incorporated_by_reference",
            "passages": [_CLX_BY_REFERENCE],
            "non_comparable_flag": _CLX_BY_REFERENCE_FLAG,
        },
    },
    "PG": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "Based on our interest rate exposure as of and during the fiscal year ended June 30, 2026, "
                    "including derivative and other instruments sensitive to interest rates, we believe a "
                    "near-term change in interest rates, at a 95% confidence level based on historical interest "
                    "rate movements, would not materially affect our financial statements.",
                    "Other Information (Hedging and Derivative Financial Instruments)",
                    ("OTHER INFORMATION Hedging and Derivative Financial Instruments", 3500),
                )
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that a near-term change in interest rates "
                "would not materially affect its financial statements."
            ),
        },
        "oil": _OIL_NONE,
    },
    "AAPL": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "-95",
            "passages": [
                row(
                    ["Term debt", "100 basis points, all tenors", "Increase in annual interest expense", "$95", "$129"],
                    S7A,
                    (H7A_TITLE, 2000),
                ),
                sent(
                    "The following table sets forth potential impacts on the Company\u2019s investment portfolio and "
                    "term debt, including the effects of any associated derivatives, that would result from a "
                    "hypothetical increase in relevant interest rates as of September 26, 2026 and September 27, "
                    "2025 (dollars in millions):",
                    S7A,
                    (H7A_TITLE, 2000),
                ),
                row(
                    ["Interest Rate Sensitive Instrument", "Hypothetical Interest Rate Increase", "Potential Impact", "2026", "2025"],
                    S7A,
                    (H7A_TITLE, 2000),
                ),
                row(
                    ["Investment portfolio", "100 basis points, all tenors", "Decline in fair value", "$2,608", "$2,416"],
                    S7A,
                    (H7A_TITLE, 2000),
                ),
            ],
            "expect": ["Increase in annual interest expense", "$95", "100 basis points"],
            "basis_note": (
                "Term debt only. The same table states a $2,608 million decline in the fair value of the "
                "investment portfolio, which is a fair-value effect and is not converted; the filing does not "
                "quantify the change in interest income on that portfolio."
            ),
        },
        "oil": _OIL_NONE,
    },
    "MSFT": {
        "rate": {
            "status": "fair_value_only",
            "passages": [
                row(
                    ["Interest rate", "100 basis point increase in U.S. treasury interest rates", "(1,201)", "Fair Value"],
                    S7A,
                    (H7A_UPPER, 2500),
                ),
                sent(
                    "The following table sets forth the potential loss in future earnings or fair values, including "
                    "associated derivatives, resulting from hypothetical changes in relevant market rates or prices:",
                    S7A,
                    (H7A_UPPER, 2500),
                ),
                row(["Risk Categories", "Hypothetical Change", "June 30, 2026", "Impact"], S7A, (H7A_UPPER, 2500)),
            ],
            "expect": ["(1,201)", "Fair Value"],
            "non_comparable_flag": (
                "The filing's table marks the interest-rate row as a fair-value impact ($1,201 million for a "
                "100 basis point increase in U.S. treasury interest rates), not an earnings impact."
            ),
        },
        "oil": _OIL_NONE,
    },
    "JPM": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "2100",
            "passages": [
                row(["+100 bps shift in rates", "$2.1", "$2.3"], _JPM_EAR_LABEL, *_JPM_EAR),
                sent(
                    "Earnings-at-risk estimates the Firm\u2019s interest rate exposure for a given interest rate "
                    "scenario. It is presented as a sensitivity to a baseline, which includes net interest income "
                    "and certain interest rate sensitive fees.",
                    _JPM_EAR_LABEL,
                    *_JPM_EAR,
                ),
                sent(
                    "Earnings-at-risk scenarios estimate the potential change to a baseline over the following 12 "
                    "months utilizing multiple assumptions.",
                    _JPM_EAR_LABEL,
                    *_JPM_EAR,
                ),
                sent("The Firm\u2019s sensitivities are presented in the table below.", _JPM_EAR_LABEL, *_JPM_EAR),
                row(["December 31, (in billions)", "2025(a)", "2024(a)"], _JPM_EAR_LABEL, *_JPM_EAR),
                row(["-100 bps shift in rates", "(2.4)", "(2.5)"], _JPM_EAR_LABEL, *_JPM_EAR),
                row(["+200 bps shift in rates", "3.7", "4.6"], _JPM_EAR_LABEL, *_JPM_EAR),
                row(["-200 bps shift in rates", "(6.0)", "(4.9)"], _JPM_EAR_LABEL, *_JPM_EAR),
            ],
            "expect": ["+100 bps shift in rates", "$2.1", "in billions"],
            "basis_note": (
                "Earnings-at-risk over the following 12 months for a +100 bps parallel shift ($2.1 billion). "
                "The same table states different amounts for other shifts (-100 bps: $(2.4) billion; "
                "+200 bps: $3.7 billion; -200 bps: $(6.0) billion), so linear scaling from the +100 bps row "
                "does not reproduce them."
            ),
        },
        "oil": _OIL_NONE,
    },
    "NVDA": {
        "rate": {
            "status": "fair_value_only",
            "passages": [
                sent(
                    "According to our sensitivity analysis on our investment portfolio, a decrease in the yield "
                    "curve of 0.5% as of the end of fiscal year 2026 and 2025 would decrease the fair value for "
                    "these investments by approximately $0.2 billion.",
                    S7A,
                    ("Item 7A. Quantitative and Qualitative Disclosures about Market Risk", 1500),
                ),
                sent(
                    "As the Notes bear interest at a fixed rate, we have no financial statement risk associated "
                    "with changes in interest rates.",
                    S7A,
                    ("Item 7A. Quantitative and Qualitative Disclosures about Market Risk", 1500),
                ),
            ],
            "expect": ["0.5%", "$0.2 billion", "fair value"],
            "non_comparable_flag": (
                "The filing states a fair-value sensitivity of its investment portfolio (about $0.2 billion "
                "for a 0.5% yield-curve move), not an annual income effect."
            ),
        },
        "oil": _OIL_NONE,
    },
    "AMZN": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "Our long-term debt is carried at amortized cost and fluctuations in interest rates do not "
                    "impact our consolidated financial statements.",
                    S7A,
                    (H7A_TITLE, 1500),
                ),
                sent(
                    "However, the fair value of our long-term debt, which pays interest at a fixed rate, will "
                    "generally fluctuate with movements of interest rates, increasing in periods of declining "
                    "rates of interest and declining in periods of increasing rates of interest.",
                    S7A,
                    (H7A_TITLE, 1500),
                ),
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that interest-rate fluctuations do not "
                "impact its consolidated financial statements for long-term debt carried at amortized cost."
            ),
        },
        "oil": _OIL_NONE,
    },
    "RTX": {
        "rate": {
            "status": "fair_value_only",
            "passages": [
                sent(
                    "A 100 basis point unfavorable interest rate movement would have had an approximate $3 billion "
                    "impact on the fair value of our fixed-rate debt at both December 31, 2025 and 2024.",
                    S7A,
                    (H7A_UPPER, 7000),
                ),
                sent(
                    "A 100 basis point unfavorable interest rate movement on variable debt would not be expected "
                    "to have a material effect on our operations or cash flows.",
                    S7A,
                    (H7A_UPPER, 7000),
                ),
            ],
            "expect": ["$3 billion", "fair value"],
            "non_comparable_flag": (
                "The filing states a fair-value sensitivity of fixed-rate debt (about $3 billion per 100 basis "
                "points) and says the effect on variable debt would not be material; neither is an annual "
                "income amount."
            ),
        },
        "oil": _OIL_NONE,
    },
    "CAT": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "A hypothetical 100 basis point adverse move in interest rates along the entire interest rate "
                    "yield curve would have a minimal impact to the 2026 pre-tax earnings of MP&E.",
                    _CAT_SENS_LABEL,
                    *_CAT_SENS,
                ),
                sent(
                    "An analysis of the December 31, 2025 balance sheet, using these assumptions, estimates the "
                    "impact of a 100 basis point immediate and sustained adverse change in interest rates to have "
                    "a minimal impact on 2026 pre-tax earnings.",
                    _CAT_SENS_LABEL,
                    *_CAT_SENS,
                ),
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that a 100 basis point adverse move would "
                "have a minimal impact on 2026 pre-tax earnings."
            ),
        },
        "oil": _OIL_NONE,
    },
    "QRVO": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "As of March 28, 2026, we did not have any outstanding borrowings under the Revolving Facility.",
                    S7A,
                    ("ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK.", 1500),
                ),
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that it had no outstanding variable-rate "
                "borrowings under its Revolving Facility as of March 28, 2026."
            ),
        },
        "oil": _OIL_NONE,
    },
    "SWKS": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "Based on our results of operations for the fiscal year ended October 2, 2026, a hypothetical "
                    "reduction in the interest rates on our cash, cash equivalents, and other investments of 100 "
                    "basis points would result in an immaterial reduction of interest income with a de minimis "
                    "impact on income before taxes.",
                    S7A,
                    ("ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK.", 1500),
                ),
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that a 100 basis point reduction in interest "
                "rates on cash and investments would have a de minimis impact on income before taxes."
            ),
        },
        "oil": _OIL_NONE,
    },
    "INTC": {
        "rate": {
            "status": "income_statement",
            "disclosed_basis_bps": "100",
            "pre_tax_impact_m": "-97",
            "passages": [
                sent(
                    "Taking into account fixed-rate debt that is swapped to floating-rate debt, a hypothetical "
                    "increase in interest rates of 1% would result in an increase in annual interest expense of "
                    "approximately $97 million from debt outstanding as of December 27, 2025 ($120 million increase "
                    "in annual interest expense from debt outstanding as of December 28, 2024).",
                    S7A,
                    ("Quantitative and Qualitative Disclosures About Market Risk", 4000),
                    ("Interest Rates", 1500),
                ),
                sent(
                    "A hypothetical change in benchmark interest rates of 1%, after taking into account investment "
                    "hedges, would have resulted in a change in the fair value of our investment portfolio of less "
                    "than $100 million as of December 27, 2025 and as of December 28, 2024.",
                    S7A,
                    ("Quantitative and Qualitative Disclosures About Market Risk", 4000),
                    ("Interest Rates", 1500),
                ),
            ],
            "expect": ["1%", "$97 million", "annual interest expense"],
            "basis_note": (
                "Debt swapped to floating-rate debt only ($97 million increase in annual interest expense for a 1% "
                "increase in rates). The same section notes a fair-value change of less than $100 million on the "
                "investment portfolio, which is not an income-statement impact."
            ),
        },
        "oil": _OIL_NONE,
    },
    "SMCI": {
        "rate": {
            "status": "not_quantified",
            "passages": [
                sent(
                    "Based on the outstanding principal indebtedness of $4,056.1 million under our credit "
                    "facilities as of June 30, 2026, we believe that a 10% change in interest rates would not have "
                    "a significant impact on the results of operations.",
                    S7A,
                    ("Item 7A. Quantitative and Qualitative Disclosure About Market Risk", 1800),
                ),
            ],
            "non_comparable_flag": (
                "The filing gives no dollar sensitivity; it states that a 10% change in interest rates on its "
                "$4,056.1 million credit-facility principal would not have a significant impact on results of "
                "operations."
            ),
        },
        "oil": _OIL_NONE,
    },
}

EDGES: list[dict[str, Any]] = [
    {
        "edge_id": "E01",
        "src": "HXL",
        "dst": "BA",
        "relationship": "SUPPLIES_COMPOSITE_MATERIALS_TO",
        "revenue_share_pct": "13",
        "share_note": "Share of Hexcel's 2025 net sales that went to Boeing and its subcontractors.",
        "cited_in": "HXL",
        "passage": sent(
            "Approximately 13%, 15% and 15% of our 2025, 2024 and 2023 net sales, respectively, were to Boeing "
            "and its subcontractors.",
            "Item 1. Business",
            ("ITEM 1. Business", 14000),
        ),
        "expect": ["13%"],
    },
    {
        "edge_id": "E02",
        "src": "RTX",
        "dst": "BA",
        "relationship": "SUPPLIES_AEROSPACE_SYSTEMS_TO",
        "revenue_share_pct": None,
        "share_note": (
            "No Boeing-only share is disclosed. Boeing and Airbus together were 16% of Collins segment "
            "sales in 2025, before discounts and incentives."
        ),
        "cited_in": "RTX",
        "passage": sent(
            "Collins\u2019 largest commercial customers are Boeing and Airbus with combined sales, prior to discounts "
            "and incentives, of 16%, 16%, and 19% of total Collins segment sales in 2025, 2024, and 2023, "
            "respectively.",
            "Item 1. Business",
            ("ITEM 1. BUSINESS", 4500),
        ),
    },
    {
        "edge_id": "E03",
        "src": "GE",
        "dst": "BA",
        "relationship": "SUPPLIES_ENGINES_TO",
        "revenue_share_pct": None,
        "share_note": "The filing names Boeing as a customer and discloses no revenue share.",
        "cited_in": "GE",
        "passage": sent(
            "Our CES customers for equipment and services consist primarily of airframers and airlines, including "
            "both Boeing and Airbus, and third-party MRO shops, to whom we sell spare parts and license MRO "
            "technology.",
            "About GE Aerospace (Commercial Engines & Services)",
            ("ABOUT GE AEROSPACE.", 3000),
            ("COMMERCIAL ENGINES & SERVICES.", 1200),
        ),
    },
    {
        "edge_id": "E04",
        "src": "BA",
        "dst": "DAL",
        "relationship": "SELLS_AIRCRAFT_TO",
        "revenue_share_pct": None,
        "share_note": "The filing discloses an aircraft purchase agreement and no revenue share.",
        "cited_in": "DAL",
        "passage": sent(
            "In addition to the aircraft purchase commitments above, on January 12, 2026, we entered into a "
            "definitive agreement with The Boeing Company to acquire 30 Boeing 787-10 aircraft, with an option "
            "to purchase up to an additional 30 of the same aircraft.",
            "Item 2. Properties (Aircraft Purchase Commitments)",
            ("ITEM 2. PROPERTIES", 4000),
            ("Aircraft Purchase Commitments", 1500),
        ),
    },
    {
        "edge_id": "E05",
        "src": "BA",
        "dst": "UAL",
        "relationship": "SELLS_AIRCRAFT_TO",
        "revenue_share_pct": None,
        "share_note": "The filing names Boeing as an aircraft supplier and discloses no revenue share.",
        "cited_in": "UAL",
        "passage": sent(
            'The Company currently sources substantially all of its aircraft and many related aircraft parts from '
            'The Boeing Company ("Boeing") or Airbus S.A.S. ("Airbus").',
            "Item 1A. Risk Factors",
            ("ITEM 1A. RISK FACTORS", 21000),
            not_between=("ITEM 1B.",),
        ),
    },
    {
        "edge_id": "E06",
        "src": "GE",
        "dst": "DAL",
        "relationship": "SUPPLIES_ENGINES_TO",
        "revenue_share_pct": None,
        "share_note": "The filing names the engine manufacturer and discloses no revenue share.",
        "cited_in": "DAL",
        "passage": sent(
            "The B-787-10 aircraft will include GEnx engines manufactured by General Electric.",
            "Item 2. Properties (Aircraft Purchase Commitments)",
            ("ITEM 2. PROPERTIES", 4000),
            ("Aircraft Purchase Commitments", 1500),
        ),
    },
    {
        "edge_id": "E07",
        "src": "CLX",
        "dst": "WMT",
        "relationship": "SELLS_THROUGH_RETAILER",
        "revenue_share_pct": "26",
        "share_note": "Share of Clorox's fiscal 2026 consolidated net sales that went to Walmart and its affiliates.",
        "cited_in": "CLX",
        "passage": sent(
            "Net sales to the Company\u2019s largest customer, Walmart Stores, Inc. and its affiliates, were 26%, 27% "
            "and 25% of consolidated net sales for each of the fiscal years ended June 30, 2026, 2025 and 2024, "
            "respectively, and occurred across all of the Company\u2019s reportable segments.",
            "Item 1. Business (Customers)",
            ("ITEM 1. BUSINESS", 16000),
            ("Customers", 20),
            not_between=("ITEM 1.A. RISK FACTORS",),
        ),
        "expect": ["26%"],
    },
    {
        "edge_id": "E08",
        "src": "PG",
        "dst": "WMT",
        "relationship": "SELLS_THROUGH_RETAILER",
        "revenue_share_pct": "16",
        "share_note": "Approximate share of P&G's fiscal 2026 total sales that went to Walmart and its affiliates.",
        "cited_in": "PG",
        "passage": sent(
            "Sales to Walmart Inc. and its affiliates represent approximately 16% of our total sales in 2026, "
            "2025 and 2024.",
            "Item 1. Business",
            ("Item 1. Business", 4500),
        ),
        "expect": ["16%"],
    },
    {
        "edge_id": "E09",
        "src": "QRVO",
        "dst": "AAPL",
        "relationship": "SUPPLIES_RF_SOLUTIONS_TO",
        "revenue_share_pct": "50",
        "share_note": "Approximate aggregate share of Qorvo's fiscal 2026 total revenue from end customer Apple Inc. through contract manufacturers.",
        "cited_in": "QRVO",
        "passage": sent(
            "The Company provides products to its largest end customer, Apple Inc., through sales to multiple "
            "contract manufacturers, which in the aggregate accounted for approximately 50%, 47% and 46% of "
            "total revenue in fiscal years 2026, 2025 and 2024, respectively.",
            "Note 3. Concentrations of Credit Risk",
            ("3. CONCENTRATIONS OF CREDIT RISK", 1000),
        ),
        "expect": ["50%", "Apple Inc."],
    },
    {
        "edge_id": "E10",
        "src": "SWKS",
        "dst": "AAPL",
        "relationship": "SUPPLIES_RF_SEMICONDUCTORS_TO",
        "revenue_share_pct": "61",
        "share_note": "Aggregate share of Skyworks' fiscal 2026 net revenue from Apple through distributors and contract manufacturers.",
        "cited_in": "SWKS",
        "passage": sent(
            "During fiscal 2026, fiscal 2025, and fiscal 2024, Apple, through sales to multiple distributors "
            "and contract manufacturers for multiple applications including smartphones, tablets, desktop and "
            "notebook computers, watches, and other devices, in the aggregate accounted for 61%, 67%, and 69% "
            "of the Company\u2019s net revenue, respectively.",
            "Note 14. Segment Information and Concentrations",
            ("14. SEGMENT INFORMATION AND CONCENTRATIONS", 4500),
        ),
        "expect": ["61%", "Apple"],
    },
    {
        "edge_id": "E11",
        "src": "NVDA",
        "dst": "INTC",
        "relationship": "CO_DEVELOPS_AI_PRODUCTS_WITH",
        "revenue_share_pct": None,
        "share_note": "The filing discloses a strategic partnership to co-develop custom client and data center products and states no revenue share.",
        "cited_in": "INTC",
        "passage": sent(
            "We also announced a strategic partnership with NVIDIA to co-develop custom client and data center "
            "products combining our x86 CPU technologies with NVIDIA's AI and accelerated computing capabilities.",
            "Item 1. Business (Overview)",
            ("Overview", 3000),
        ),
        "expect": ["NVIDIA"],
    },
    {
        "edge_id": "E12",
        "src": "SMCI",
        "dst": "NVDA",
        "relationship": "ALIGNS_AI_SERVER_PLATFORMS_WITH",
        "revenue_share_pct": None,
        "share_note": "The filing names NVIDIA among the processor vendors Supermicro collaborates with on product cycles and discloses no revenue share.",
        "cited_in": "SMCI",
        "passage": sent(
            "We work closely with NVIDIA, Intel and AMD, among others, to develop products that are compatible "
            "with the latest generation of industry-standard technologies under development.",
            "Item 1. Business (Research and Development)",
            ("Research and Development", 1500),
        ),
        "expect": ["NVIDIA"],
    },
]

# Period-over-period records. ``evidence`` names where the filing states both periods:
# ("rate", index) or ("oil", index) is a sensitivity passage, ("edge", id) an edge passage.
# Issuers without such a passage fall back to long-term debt from XBRL.
WATCH: dict[str, dict[str, Any]] = {
    "AAPL": {
        "metric": "Increase in annual interest expense on term debt per 100 basis points",
        "evidence": ("rate", 0),
        "prior": ("$129 million", "129", "September 27, 2025"),
        "current": ("$95 million", "95", "September 26, 2026"),
        "unit": "usd_m",
        "expect": ["$129", "$95"],
    },
    "JPM": {
        "metric": "Earnings-at-risk for a +100 bps parallel shift",
        "evidence": ("rate", 0),
        "prior": ("$2.3 billion", "2300", "December 31, 2024"),
        "current": ("$2.1 billion", "2100", "December 31, 2025"),
        "unit": "usd_m",
        "expect": ["$2.3", "$2.1"],
    },
    "FDX": {
        "metric": "Fair-value decrease of long-term debt for a hypothetical 10% increase in interest rates",
        "evidence": ("rate", 0),
        "prior": ("$600 million", "600", "May 31, 2025"),
        "current": ("$694 million", "694", "May 31, 2026"),
        "unit": "usd_m",
        "expect": ["$600 million", "$694 million"],
    },
    "NVDA": {
        "metric": "Fair-value decrease of investments for a 0.5% yield-curve move",
        "evidence": ("rate", 0),
        "prior": ("about $0.2 billion", "200", "end of fiscal year 2025"),
        "current": ("about $0.2 billion", "200", "end of fiscal year 2026"),
        "unit": "usd_m",
        "expect": ["fiscal year 2026 and 2025", "$0.2 billion"],
    },
    "HXL": {
        "metric": "Share of net sales to Boeing and its subcontractors",
        "evidence": ("edge", "E01"),
        "prior": ("15%", "15", "2024"),
        "current": ("13%", "13", "2025"),
        "unit": "pct_share",
        "expect": ["13%, 15% and 15%"],
    },
    "RTX": {
        "metric": "Boeing and Airbus combined share of Collins segment sales",
        "evidence": ("edge", "E02"),
        "prior": ("16%", "16", "2024"),
        "current": ("16%", "16", "2025"),
        "unit": "pct_share",
        "expect": ["16%, 16%, and 19%"],
    },
    "CLX": {
        "metric": "Share of consolidated net sales to Walmart and its affiliates",
        "evidence": ("edge", "E07"),
        "prior": ("27%", "27", "fiscal year ended June 30, 2025"),
        "current": ("26%", "26", "fiscal year ended June 30, 2026"),
        "unit": "pct_share",
        "expect": ["26%, 27% and 25%"],
    },
    "PG": {
        "metric": "Approximate share of total sales to Walmart and its affiliates",
        "evidence": ("edge", "E08"),
        "prior": ("16%", "16", "2025"),
        "current": ("16%", "16", "2026"),
        "unit": "pct_share",
        "expect": ["16% of our total sales in 2026, 2025 and 2024"],
    },
    "QRVO": {
        "metric": "Approximate aggregate share of total revenue from end customer Apple Inc.",
        "evidence": ("edge", "E09"),
        "prior": ("47%", "47", "fiscal year 2025"),
        "current": ("50%", "50", "fiscal year 2026"),
        "unit": "pct_share",
        "expect": ["50%, 47% and 46%"],
    },
    "SWKS": {
        "metric": "Aggregate share of net revenue from Apple",
        "evidence": ("edge", "E10"),
        "prior": ("67%", "67", "fiscal 2025"),
        "current": ("61%", "61", "fiscal 2026"),
        "unit": "pct_share",
        "expect": ["61%, 67%, and 69%"],
    },
    "INTC": {
        "metric": "Increase in annual interest expense per 1% increase in interest rates",
        "evidence": ("rate", 0),
        "prior": ("$120 million", "120", "December 28, 2024"),
        "current": ("$97 million", "97", "December 27, 2025"),
        "unit": "usd_m",
        "expect": ["$97 million", "$120 million"],
    },
}

REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
EPS_TAGS = ["EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare"]
DEBT_TAGS = [
    "LongTermDebtNoncurrent",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    "LongTermDebt",
    "ConvertibleLongTermNotesPayable",
]
# Plain descriptions; the XBRL tag shown next to each one is the precise definition.
DEBT_LABELS = {
    "us-gaap:LongTermDebtNoncurrent": "Long-term debt (noncurrent)",
    "us-gaap:LongTermDebtAndCapitalLeaseObligations": "Long-term debt and lease obligations (noncurrent)",
    "us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": (
        "Long-term debt and lease obligations (including current maturities)"
    ),
    "us-gaap:LongTermDebt": "Long-term debt",
    "us-gaap:ConvertibleLongTermNotesPayable": "Convertible senior notes (noncurrent)",
}


def _usd_millions(value: Decimal) -> str:
    quantized = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"${quantized:,} million"


def _check_anchors(ticker: str, text: str, offset: int, spec: dict[str, Any], what: str) -> None:
    """Each anchor heading must precede the passage within its gap, with no blocker in between."""
    if not spec["anchors"]:
        raise BuildError(f"{ticker}: {what} has no section anchor.")
    for heading, max_gap in spec["anchors"]:
        position = text.rfind(heading, 0, offset)
        if position < 0:
            raise BuildError(f"{ticker}: heading {heading!r} does not precede {what}.")
        gap = offset - (position + len(heading))
        if gap > max_gap:
            raise BuildError(f"{ticker}: {what} is {gap} characters after {heading!r} (limit {max_gap}).")
        for blocker in spec["not_between"]:
            if text.find(blocker, position + len(heading), offset) >= 0:
                raise BuildError(f"{ticker}: {blocker!r} occurs between {heading!r} and {what}.")


def resolve_passage(ticker: str, filing: edgar_source.FilingText, spec: dict[str, Any]) -> dict[str, str]:
    """Check one passage spec against the filing and return the bundled passage."""
    if spec["kind"] == "sentence":
        text = spec["text"]
        offset = filing.text.find(text)
        if offset < 0:
            raise BuildError(f"{ticker}: sentence is not in the filing text: {text[:90]!r}")
    else:
        matches = edgar_source.find_rows(filing, spec["cells"])
        if len(matches) != 1:
            raise BuildError(f"{ticker}: {len(matches)} table rows match {spec['cells']!r}; expected 1.")
        text = edgar_source.row_text(matches[0]["cells"])
        offset = matches[0]["offset"]
    _check_anchors(ticker, filing.text, offset, spec, f"{spec['kind']} {text[:60]!r}")
    return {"kind": spec["kind"], "section": spec["section"], "text": text}


def build_sensitivity(ticker: str, factor: str, filing: edgar_source.FilingText) -> dict[str, Any]:
    """Resolve one sensitivity spec into the record stored in the dataset."""
    spec = SENSITIVITIES[ticker][factor]
    status = spec["status"]
    if status not in fsi_engine.SENSITIVITY_STATUSES:
        raise BuildError(f"{ticker}.{factor}: unknown status {status!r}.")
    passages = [resolve_passage(ticker, filing, item) for item in spec.get("passages", [])]
    joined = "\n".join(passage["text"] for passage in passages)
    for needle in spec.get("expect", []):
        if needle not in joined:
            raise BuildError(f"{ticker}.{factor}: {needle!r} is not in the bundled passages.")

    record: dict[str, Any] = {"status": status}
    if status == fsi_engine.STATUS_INCOME_STATEMENT:
        if not passages:
            raise BuildError(f"{ticker}.{factor}: an income-statement sensitivity needs a passage.")
        for key in (
            "disclosed_basis_bps",
            "disclosed_basis_unit",
            "disclosed_basis_value",
            "reference_price_usd_per_gallon",
            "pre_tax_impact_m",
        ):
            if key in spec:
                record[key] = spec[key]
        if "components" in spec:
            total = sum((Decimal(item["amount_m"]) for item in spec["components"]), Decimal("0"))
            if total != Decimal(spec["pre_tax_impact_m"]):
                raise BuildError(f"{ticker}.{factor}: components sum to {total}, not {spec['pre_tax_impact_m']}.")
            record["components"] = spec["components"]
    elif status == fsi_engine.STATUS_NONE_FOUND:
        if passages:
            raise BuildError(f"{ticker}.{factor}: none_found must not carry a passage.")
    elif not passages or not spec.get("non_comparable_flag"):
        raise BuildError(f"{ticker}.{factor}: status {status} needs a passage and a non_comparable_flag.")

    # The first passage is the one the figure is quoted from; the rest support it.
    record["section"] = passages[0]["section"] if passages else NONE_FOUND_SECTION
    if passages:
        record["passages"] = passages
    for key in ("derivation", "basis_note", "non_comparable_flag", "coverage_note"):
        if spec.get(key):
            record[key] = spec[key]
    return record


def check_fuel_screen(ticker: str, filing: edgar_source.FilingText, oil: dict[str, Any]) -> None:
    """The oil status must agree with what the fuel screen finds in the filing text.

    A quantified status needs exactly one hit and it must be the quoted sentence (a hit may
    carry a heading in front of it, because headings do not end with a full stop). Any other
    status needs no hit at all.
    """
    hits = fuel_sensitivity_sentences(filing.text)
    if oil["status"] == fsi_engine.STATUS_INCOME_STATEMENT:
        if len(hits) != 1 or not hits[0].endswith(oil["passages"][0]["text"]):
            raise BuildError(f"{ticker}.oil: the fuel screen found {hits!r}, not exactly the quoted sentence.")
    elif hits:
        raise BuildError(f"{ticker}.oil: status {oil['status']} but the fuel screen found {hits!r}.")


def build_xbrl(
    ticker: str,
    facts: dict[str, Any],
    *,
    accession: str,
    period_end: str,
    source: str,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """Return ``(xbrl values, their sources, long-term debt comparison)`` for one accession."""

    def need(label: str, tags: list[str], unit: str, annual: bool) -> dict[str, Any]:
        fact = edgar_source.select_fact(facts, tags, unit, accession=accession, period_end=period_end, annual_duration=annual)
        if fact is None:
            raise BuildError(f"{ticker}: accession {accession} reports none of {tags} for {period_end} ({label}).")
        return fact

    revenue = need("revenue", REVENUE_TAGS, "USD", True)
    shares = need("diluted shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares", True)
    eps = need("diluted EPS", EPS_TAGS, "USD/shares", True)
    tax_rate = need("effective tax rate", ["EffectiveIncomeTaxRateContinuingOperations"], "pure", True)
    debt = edgar_source.select_fact(facts, DEBT_TAGS, "USD", accession=accession, period_end=period_end, annual_duration=False)
    if debt is None and ticker not in WATCH:
        raise BuildError(f"{ticker}: accession {accession} reports none of {DEBT_TAGS} for {period_end} (long-term debt).")

    comparison: dict[str, Any] | None = None
    debt_value_m: str | None = None
    if debt is not None:
        prior_end = edgar_source.prior_instant_end(facts, debt["tag"], "USD", accession=accession, period_end=period_end)
        if prior_end is None:
            raise BuildError(f"{ticker}: no prior balance-sheet date for {debt['tag']} in accession {accession}.")
        prior_debt = edgar_source.select_fact(
            facts, [debt["tag"].removeprefix("us-gaap:")], "USD", accession=accession, period_end=prior_end, annual_duration=False
        )
        if prior_debt is None:
            raise BuildError(f"{ticker}: {debt['tag']} has no value at {prior_end}.")
        debt_value_m = edgar_source.to_millions(debt["value"])
        comparison = {
            "tag": debt["tag"],
            "prior_end": prior_end,
            "prior_m": Decimal(edgar_source.to_millions(prior_debt["value"])),
            "current_end": period_end,
            "current_m": Decimal(debt_value_m),
        }

    values: dict[str, Any] = {
        "revenue_m": edgar_source.to_millions(revenue["value"]),
        "effective_tax_rate": edgar_source.decimal_text(Decimal(tax_rate["value"])),
        "diluted_shares_m": edgar_source.to_millions(shares["value"]),
        "diluted_eps": edgar_source.decimal_text(Decimal(eps["value"])),
        "long_term_debt_m": debt_value_m,
    }
    if not Decimal("0") <= Decimal(values["effective_tax_rate"]) < Decimal("1"):
        raise BuildError(f"{ticker}: effective tax rate {values['effective_tax_rate']} is outside [0, 1).")

    def origin(fact: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": fact["tag"],
            "unit": fact["unit"],
            "period_start": fact["period_start"],
            "period_end": fact["period_end"],
        }

    sources = {
        "source": source,
        "accession_number": accession,
        "fiscal_year": eps["fiscal_year"],
        "revenue_m": origin(revenue),
        "effective_tax_rate": origin(tax_rate),
        "diluted_shares_m": origin(shares),
        "diluted_eps": origin(eps),
        "long_term_debt_m": origin(debt) if debt is not None else None,
    }
    return values, sources, comparison


def _change_text(prior: Decimal, current: Decimal, unit: str) -> str:
    delta = current - prior
    if delta == 0:
        return "Unchanged."
    direction = "Up" if delta > 0 else "Down"
    if unit == "pct_share":
        points = abs(delta)
        return f"{direction} {edgar_source.decimal_text(points)} percentage point{'s' if points != 1 else ''}."
    percent = (delta / abs(prior) * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{direction} {_usd_millions(abs(delta))} ({percent:+}%)."


def build_watch(
    ticker: str,
    sensitivities: dict[str, dict[str, Any]],
    edge_passages: dict[str, dict[str, str]],
    debt: dict[str, Any] | None,
    accession: str,
) -> dict[str, Any]:
    """Period-over-period record: a passage that states both periods, else long-term debt from XBRL."""
    spec = WATCH.get(ticker)
    if spec is None:
        assert debt is not None
        label = DEBT_LABELS[debt["tag"]]
        return {
            "metric": f"{label} ({debt['tag']})",
            "basis": "xbrl",
            "prior_period_end": debt["prior_end"],
            "current_period_end": debt["current_end"],
            "prior_value": _usd_millions(debt["prior_m"]),
            "current_value": _usd_millions(debt["current_m"]),
            "change_summary": _change_text(debt["prior_m"], debt["current_m"], "usd_m"),
            "value_source": f"XBRL {debt['tag']}; both balance-sheet dates as reported in accession {accession}.",
        }

    kind, key = spec["evidence"]
    passage = edge_passages[key] if kind == "edge" else sensitivities[kind]["passages"][key]
    for needle in spec["expect"]:
        if needle not in passage["text"]:
            raise BuildError(f"{ticker}.watch: {needle!r} is not in the evidence passage.")
    prior_text, prior_number, prior_period = spec["prior"]
    current_text, current_number, current_period = spec["current"]
    return {
        "metric": spec["metric"],
        "basis": "filing_passage",
        "prior_period_end": prior_period,
        "current_period_end": current_period,
        "prior_value": prior_text,
        "current_value": current_text,
        "change_summary": _change_text(Decimal(prior_number), Decimal(current_number), spec["unit"]),
        "value_source": "Both periods are stated in the quoted passage of the current filing.",
        "evidence_section": passage["section"],
        "evidence_quote": passage["text"],
    }


def build_dataset(cache: edgar_source.EdgarCache) -> dict[str, Any]:
    """Fetch (or read from cache) every source document and assemble the sample book."""
    filings: dict[str, edgar_source.FilingText] = {}
    companies: dict[str, dict[str, Any]] = {}
    debts: dict[str, dict[str, Any]] = {}
    retrieved: list[str] = []

    for ticker, issuer in ISSUERS.items():
        cik, accession = issuer["cik"], issuer["accession"]
        record = edgar_source.filing_record(cache.submissions(ticker, cik), accession)
        if record["form"] != "10-K":
            raise BuildError(f"{ticker}: accession {accession} is a {record['form']}, not a 10-K.")
        document, url, document_retrieved = cache.primary_document(ticker, cik, accession, record["primary_document"])
        filing = edgar_source.parse_filing_html(document.decode("utf-8", errors="replace"))
        filings[ticker] = filing
        retrieved.append(document_retrieved)

        facts, facts_retrieved = cache.company_facts(ticker, cik)
        source = "SEC companyfacts API"
        if not edgar_source.has_accession_facts(facts, accession):
            instance, instance_url, facts_retrieved = cache.xbrl_instance(ticker, cik, accession, record["primary_document"])
            facts = edgar_source.instance_to_company_facts(instance, accession)
            source = f"XBRL instance of the filing ({instance_url}); the companyfacts API had no facts for this accession"
        retrieved.append(facts_retrieved)
        xbrl, xbrl_sources, debts[ticker] = build_xbrl(
            ticker, facts, accession=accession, period_end=record["report_date"], source=source
        )

        sensitivities = {factor: build_sensitivity(ticker, factor, filing) for factor in ("rate", "oil")}
        check_fuel_screen(ticker, filing, sensitivities["oil"])
        companies[ticker] = {
            "ticker": ticker,
            "name": issuer["name"],
            "cik": cik,
            "sector": issuer["sector"],
            "filing_doc_id": f"{ticker}-10K-FY{xbrl_sources['fiscal_year']}",
            "filing": {
                **record,
                "url": url,
                "document_sha256": edgar_source.sha256_bytes(document),
                "retrieved_at_utc": document_retrieved,
            },
            "excerpt_sha256": "",
            "xbrl": xbrl,
            "xbrl_sources": xbrl_sources,
            "sensitivities": sensitivities,
        }

    edges: list[dict[str, Any]] = []
    edge_passages: dict[str, dict[str, str]] = {}
    for spec in EDGES:
        cited = spec["cited_in"]
        passage = resolve_passage(cited, filings[cited], spec["passage"])
        for needle in spec.get("expect", []):
            if needle not in passage["text"]:
                raise BuildError(f"{spec['edge_id']}: {needle!r} is not in the edge passage.")
        edge_passages[spec["edge_id"]] = passage
        edges.append(
            {
                "edge_id": spec["edge_id"],
                "src": spec["src"],
                "dst": spec["dst"],
                "relationship": spec["relationship"],
                "revenue_share_pct": spec["revenue_share_pct"],
                "share_note": spec["share_note"],
                "doc_id": companies[cited]["filing_doc_id"],
                "section": passage["section"],
                "verbatim_quote": passage["text"],
            }
        )

    for ticker, company in companies.items():
        company["yoy_delta"] = build_watch(
            ticker, company["sensitivities"], edge_passages, debts[ticker], company["filing"]["accession_number"]
        )

    dataset: dict[str, Any] = {
        "metadata": {
            "dataset_id": DATASET_ID,
            "as_of_utc": max(retrieved),
            "source_posture": fsi_engine.POSTURE_AUTHORITATIVE,
            "data_notice": DATA_NOTICE,
            "description": (
                f"{len(companies)} issuers, {len(BOOKS)} books, {len(SCENARIOS)} scenario presets and "
                f"{len(edges)} disclosed links, built by scripts/build_sample_book.py. excerpt_sha256 is the "
                "SHA-256 of an issuer's bundled passages (fsi_engine.compute_excerpt_sha256); "
                "filing.document_sha256 is the SHA-256 of the primary document as retrieved."
            ),
            "retrieval": {
                "source": "SEC EDGAR: sec.gov Archives, data.sec.gov submissions and XBRL companyfacts",
                "retrieved_from_utc": min(retrieved),
                "retrieved_to_utc": max(retrieved),
                "builder": "scripts/build_sample_book.py",
                "normalisation": edgar_source.NORMALISATION_NOTE,
                "fuel_screen": FUEL_SCREEN_NOTE,
                "annotations": ANNOTATION_NOTE,
            },
        },
        "books": BOOKS,
        "scenarios": SCENARIOS,
        "companies": companies,
        "supply_chain_edges": edges,
    }
    for ticker, company in companies.items():
        evidence = company["yoy_delta"].get("evidence_quote")
        bundled = {passage["text"] for passage in fsi_engine.canonical_excerpt_passages(dataset, ticker)}
        if evidence and evidence not in bundled:
            raise BuildError(f"{ticker}.watch: the evidence passage is not among the issuer's bundled passages.")
        company["excerpt_sha256"] = fsi_engine.compute_excerpt_sha256(dataset, ticker)
    return dataset


def without_retrieval_times(dataset: dict[str, Any]) -> dict[str, Any]:
    """Copy of a dataset with retrieval timestamps removed, for comparing two builds."""
    clone = json.loads(json.dumps(dataset))
    clone["metadata"].pop("as_of_utc", None)
    for key in ("retrieved_from_utc", "retrieved_to_utc"):
        clone["metadata"].get("retrieval", {}).pop(key, None)
    for company in clone["companies"].values():
        company.get("filing", {}).pop("retrieved_at_utc", None)
    return clone


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the sample book from SEC EDGAR 10-K filings.")
    parser.add_argument("--cache-dir", default=str(SKILL_ROOT / ".edgar_cache"), help="Directory for downloaded filings")
    parser.add_argument("--offline", action="store_true", help="Fail instead of fetching anything that is not cached")
    parser.add_argument("--check", action="store_true", help="Compare the rebuilt dataset with the bundled file")
    parser.add_argument("--output", default=str(fsi_engine.SAMPLE_BOOK_PATH), help="Where to write the dataset")
    args = parser.parse_args()

    cache = edgar_source.EdgarCache(Path(args.cache_dir), offline=args.offline)
    try:
        dataset = build_dataset(cache)
    except (BuildError, FileNotFoundError, RuntimeError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    summary = (
        f"{len(dataset['companies'])} issuers, {len(dataset['supply_chain_edges'])} links, "
        f"{cache.network_requests} request(s) to sec.gov"
    )
    if args.check:
        bundled = json.loads(Path(args.output).read_text(encoding="utf-8"))
        if without_retrieval_times(bundled) != without_retrieval_times(dataset):
            print(f"check failed: {args.output} differs from a fresh build ({summary}).", file=sys.stderr)
            return 1
        print(f"check passed: {args.output} matches a fresh build ({summary}).")
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dataset, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {output} ({summary}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
