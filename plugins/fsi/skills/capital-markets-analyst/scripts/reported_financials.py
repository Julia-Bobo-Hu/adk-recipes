"""Reported annual figures from SEC XBRL company facts, with growth and margins in exact decimals.

``build_record`` reads one company's SEC "companyfacts" JSON and keeps, for each line item
in ``LINE_ITEMS`` and each recent fiscal year, the value that the most recent Form 10-K
reports for that year, with the XBRL tag and the accession number it was taken from.
``company_view`` turns a record into what the agent tool returns and adds year-over-year
changes, growth rates, margins and free cash flow. ``comparison_rows`` lines up the latest
fiscal year of several companies.

Nothing in this module uses the network. Run it as a script to rebuild the bundled records
from an EDGAR cache::

    python scripts/reported_financials.py --cache-dir ./.edgar_cache --offline --check

``scripts/sec_lookup.py`` builds the same record for a company outside the bundle.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import edgar_source, fsi_engine  # noqa: E402

BUNDLE_PATH = SKILL_ROOT / "assets" / "sample_10k_book" / "reported_financials.json"
DATASET_ID = "reported_financials_edgar_20261120"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

DURATION = "duration"
INSTANT = "instant"
USD_MILLIONS = "USD millions"
USD_PER_SHARE = "USD per share"
SHARES_MILLIONS = "millions of shares"
FRACTION = "fraction"
PERCENT = "percent"

# A fiscal year is 52 or 53 weeks, or a calendar year.
ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
# A record keeps one fiscal year more than a view can show, so the oldest shown year has a prior year.
KEEP_FISCAL_YEARS = 4
MAX_VIEW_YEARS = 3
MILLION = Decimal("1000000")
HUNDRED = Decimal("100")

BASIS_NOTE = (
    "Annual figures as reported on Form 10-K under US GAAP, read from SEC XBRL company facts. Each value "
    "is the one the most recent Form 10-K reports for that fiscal year, so a prior year can differ from "
    "what was first reported; restated_from then gives the first-reported value. Amounts are in USD "
    "millions unless line_items says otherwise. Growth, margins and free cash flow are computed in Python "
    "from these values and rounded half-up to two decimals; one is left out, with the reason under "
    "not_computed, when an input is missing or when its inputs come from different filings and one was "
    "restated. A line item under not_tagged is one the company does not report under any of the standard "
    "tags this skill reads; it is not zero."
)
DATA_NOTICE = (
    "Values are XBRL facts from each issuer's Form 10-K filings as returned by the SEC EDGAR company "
    "facts API (or, where noted, read from the filing's XBRL instance). The XBRL tag and accession number "
    "of every value are in the record. Line-item names are this skill's labels; the tag is the precise "
    "definition. Not investment advice."
)


@dataclass(frozen=True)
class LineItem:
    """One reported line item and the US GAAP tags that can carry it, most preferred first."""

    key: str
    label: str
    period: str
    xbrl_unit: str
    unit: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Metric:
    """One figure computed from the reported values."""

    key: str
    label: str
    unit: str
    formula: str


def _usd(key: str, label: str, period: str, *tags: str) -> LineItem:
    return LineItem(key, label, period, "USD", USD_MILLIONS, tags)


# The revenue, EPS and long-term debt tag orders are the ones scripts/build_sample_book.py uses,
# so both read the same fact for a bundled issuer.
LINE_ITEMS: tuple[LineItem, ...] = (
    _usd(
        "revenue",
        "Revenue",
        DURATION,
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ),
    _usd("cost_of_revenue", "Cost of revenue", DURATION, "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    _usd("gross_profit", "Gross profit", DURATION, "GrossProfit"),
    _usd("research_and_development", "Research and development expense", DURATION, "ResearchAndDevelopmentExpense"),
    _usd("operating_income", "Operating income", DURATION, "OperatingIncomeLoss"),
    _usd(
        "interest_expense",
        "Interest expense",
        DURATION,
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
    ),
    _usd(
        "pretax_income",
        "Income before income taxes",
        DURATION,
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    _usd("income_tax", "Income tax expense", DURATION, "IncomeTaxExpenseBenefit"),
    _usd(
        "net_income",
        "Net income",
        DURATION,
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ),
    LineItem(
        "diluted_eps",
        "Diluted earnings per share",
        DURATION,
        "USD/shares",
        USD_PER_SHARE,
        ("EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare"),
    ),
    LineItem(
        "diluted_shares",
        "Weighted-average diluted shares",
        DURATION,
        "shares",
        SHARES_MILLIONS,
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    ),
    LineItem(
        "effective_tax_rate",
        "Effective income tax rate",
        DURATION,
        "pure",
        FRACTION,
        ("EffectiveIncomeTaxRateContinuingOperations",),
    ),
    _usd(
        "operating_cash_flow",
        "Net cash from operating activities",
        DURATION,
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    _usd(
        "capital_expenditure",
        "Purchases of property, plant and equipment",
        DURATION,
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    _usd(
        "dividends_paid",
        "Dividends paid",
        DURATION,
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfOrdinaryDividends",
    ),
    _usd("share_repurchases", "Repurchases of common stock", DURATION, "PaymentsForRepurchaseOfCommonStock"),
    _usd(
        "cash_and_equivalents",
        "Cash and cash equivalents",
        INSTANT,
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    _usd("total_assets", "Total assets", INSTANT, "Assets"),
    _usd("total_liabilities", "Total liabilities", INSTANT, "Liabilities"),
    _usd(
        "stockholders_equity",
        "Shareholders' equity",
        INSTANT,
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    _usd(
        "long_term_debt",
        "Long-term debt",
        INSTANT,
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        "LongTermDebt",
        "ConvertibleLongTermNotesPayable",
    ),
)
ITEMS_BY_KEY: dict[str, LineItem] = {item.key: item for item in LINE_ITEMS}
# A Form 10-K's own fiscal year is the latest annual period it reports for any of these.
_ANCHOR_KEYS = ("net_income", "revenue", "diluted_eps", "operating_cash_flow")

_GROWTH = "({0} - prior-year {0}) / prior-year {0} x 100"
COMPUTED_METRICS: tuple[Metric, ...] = (
    Metric("revenue_change", "Change in revenue", USD_MILLIONS, "revenue - prior-year revenue"),
    Metric("revenue_growth_pct", "Revenue growth", PERCENT, _GROWTH.format("revenue")),
    Metric("operating_income_growth_pct", "Operating income growth", PERCENT, _GROWTH.format("operating_income")),
    Metric("net_income_change", "Change in net income", USD_MILLIONS, "net_income - prior-year net_income"),
    Metric("net_income_growth_pct", "Net income growth", PERCENT, _GROWTH.format("net_income")),
    Metric("diluted_eps_growth_pct", "Diluted EPS growth", PERCENT, _GROWTH.format("diluted_eps")),
    Metric("gross_margin_pct", "Gross margin", PERCENT, "gross_profit / revenue x 100"),
    Metric("operating_margin_pct", "Operating margin", PERCENT, "operating_income / revenue x 100"),
    Metric("pretax_margin_pct", "Pre-tax margin", PERCENT, "pretax_income / revenue x 100"),
    Metric("net_margin_pct", "Net margin", PERCENT, "net_income / revenue x 100"),
    Metric("rd_pct_of_revenue", "R&D as a share of revenue", PERCENT, "research_and_development / revenue x 100"),
    Metric("effective_tax_rate_pct", "Effective tax rate", PERCENT, "effective_tax_rate x 100"),
    Metric("free_cash_flow", "Free cash flow", USD_MILLIONS, "operating_cash_flow - capital_expenditure"),
    Metric("free_cash_flow_margin_pct", "Free cash flow margin", PERCENT, "free_cash_flow / revenue x 100"),
)
# What a comparison row shows for each company's latest fiscal year: reported items, then computed ones.
COMPARISON_ITEMS = ("revenue", "net_income", "diluted_eps", "long_term_debt")
COMPARISON_METRICS = (
    "revenue_growth_pct",
    "gross_margin_pct",
    "operating_margin_pct",
    "net_margin_pct",
    "free_cash_flow",
)


class NoAnnualFacts(ValueError):
    """The company facts hold no Form 10-K figures under US GAAP."""


class RecordIntegrityError(ValueError):
    """A stored record does not match its own hash."""


# --------------------------------------------------------------------------- reading company facts


def _annual_rows(company_facts: dict[str, Any], tag: str, item: LineItem) -> list[dict[str, Any]]:
    """Rows of one tag that a Form 10-K reports for a full fiscal year or a balance-sheet date."""
    node = company_facts.get("facts", {}).get("us-gaap", {}).get(tag, {})
    rows: list[dict[str, Any]] = []
    for row in node.get("units", {}).get(item.xbrl_unit, []):
        if not row.get("accn") or not row.get("end") or not str(row.get("form", "")).upper().startswith("10-K"):
            continue
        if item.period == INSTANT:
            if "start" in row:
                continue
        elif "start" not in row or not ANNUAL_MIN_DAYS <= _days(row["start"], row["end"]) <= ANNUAL_MAX_DAYS:
            continue
        rows.append(row)
    return rows


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _filing_order(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("filed", "")), str(row["accn"])


def _fiscal_year_label(row: dict[str, Any]) -> int:
    """The filing's own fiscal-year number when it is plausible, else the year the period ends in."""
    end_year = int(row["end"][:4])
    declared = row.get("fy")
    return declared if isinstance(declared, int) and abs(declared - end_year) <= 1 else end_year


def fiscal_years(company_facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Fiscal years the company has reported on Form 10-K, latest first.

    The fiscal year of a Form 10-K is the latest annual period it reports, and the filing's
    ``fy`` is the company's own number for that year. Earlier years that appear only as
    comparatives (the years before a newly listed company's first Form 10-K) are added as
    long as each ends one fiscal year before the next.
    """
    latest_in_filing: dict[str, dict[str, Any]] = {}
    first_report: dict[str, dict[str, Any]] = {}
    for key in _ANCHOR_KEYS:
        item = ITEMS_BY_KEY[key]
        for tag in item.tags:
            for row in _annual_rows(company_facts, tag, item):
                current = latest_in_filing.get(row["accn"])
                if current is None or row["end"] > current["end"]:
                    latest_in_filing[row["accn"]] = row
                known = first_report.get(row["end"])
                if known is None or _filing_order(row) < _filing_order(known):
                    first_report[row["end"]] = row
    years: dict[str, dict[str, Any]] = {}
    for row in latest_in_filing.values():
        end = row["end"]
        if end not in years or _filing_order(row) < _filing_order(years[end]["row"]):
            years[end] = {"row": row, "fiscal_year": _fiscal_year_label(row)}
    while years:
        earliest = min(years)
        before = [
            end
            for end in first_report
            if end < earliest and ANNUAL_MIN_DAYS <= _days(end, earliest) <= ANNUAL_MAX_DAYS
        ]
        if not before:
            break
        years[max(before)] = {"row": first_report[max(before)], "fiscal_year": years[earliest]["fiscal_year"] - 1}
    return [
        {
            "fiscal_year": years[end]["fiscal_year"],
            "period_start": years[end]["row"]["start"],
            "period_end": end,
            "first_reported_in": years[end]["row"]["accn"],
        }
        for end in sorted(years, reverse=True)
    ]


def _scaled(item: LineItem, raw: Any) -> str:
    """Exact value in the item's display unit; a per-share amount keeps at least two decimals."""
    value = Decimal(str(raw))
    if item.unit in (USD_MILLIONS, SHARES_MILLIONS):
        value = value / MILLION
    text = edgar_source.decimal_text(value)
    if item.unit == USD_PER_SHARE and len(text.partition(".")[2]) < 2:
        text = format(value.quantize(Decimal("0.01")), "f")
    return text


def _pick(company_facts: dict[str, Any], item: LineItem, tags: list[str], end: str) -> dict[str, Any] | None:
    """The value of the first tag that reports ``end``, as the most recent filing gives it.

    Returns None when no tag reports the period, or when the most recent filing gives two
    different values for it (the fact is then left out rather than guessed).
    """
    for tag in tags:
        rows = sorted((row for row in _annual_rows(company_facts, tag, item) if row["end"] == end), key=_filing_order)
        if not rows:
            continue
        last = rows[-1]
        if len({Decimal(str(row["val"])) for row in rows if row["accn"] == last["accn"]}) > 1:
            return None
        picked = {
            "tag": f"us-gaap:{tag}",
            "value": _scaled(item, last["val"]),
            "accession_number": last["accn"],
            "form": str(last.get("form", "")),
            "filed": str(last.get("filed", "")),
        }
        first = rows[0]
        if Decimal(str(first["val"])) != Decimal(str(last["val"])):
            picked["restated_from"] = _scaled(item, first["val"])
        return picked
    return None


def record_sha256(record: dict[str, Any]) -> str:
    """SHA-256 of a record's fiscal years and values, recomputable from the record itself."""
    payload = {"cik": record["cik"], "fiscal_years": record["fiscal_years"], "items": record["items"]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_record(
    company_facts: dict[str, Any],
    *,
    ticker: str,
    name: str,
    cik: str,
    source: str,
    retrieved_at_utc: str,
    keep_years: int = KEEP_FISCAL_YEARS,
) -> dict[str, Any]:
    """Reduce one company's SEC company facts to the record this skill stores.

    Raises:
        NoAnnualFacts: the facts hold no Form 10-K figures under US GAAP (for example a
            foreign issuer that files Form 20-F).
    """
    years = fiscal_years(company_facts)[:keep_years]
    if not years:
        raise NoAnnualFacts(f"{ticker}: SEC company facts hold no Form 10-K figures under US GAAP.")
    latest_end = years[0]["period_end"]
    items: dict[str, dict[str, Any]] = {}
    filings: dict[str, dict[str, str]] = {}
    for item in LINE_ITEMS:
        # Prefer, for every year, the tag the company uses in its latest year.
        anchor = next(
            (
                tag
                for tag in item.tags
                if any(row["end"] == latest_end for row in _annual_rows(company_facts, tag, item))
            ),
            None,
        )
        order = ([anchor] if anchor else []) + [tag for tag in item.tags if tag != anchor]
        values: dict[str, dict[str, str]] = {}
        item_tag = ""
        for year in years:
            picked = _pick(company_facts, item, order, year["period_end"])
            if picked is None:
                continue
            item_tag = item_tag or picked["tag"]
            filings[picked["accession_number"]] = {"form": picked["form"], "filed": picked["filed"]}
            value = {"value": picked["value"], "accession_number": picked["accession_number"]}
            if picked["tag"] != item_tag:
                value["tag"] = picked["tag"]
            if "restated_from" in picked:
                value["restated_from"] = picked["restated_from"]
            values[year["period_end"]] = value
        if values:
            items[item.key] = {"tag": item_tag, "values": values}
    record: dict[str, Any] = {
        "ticker": ticker,
        "name": name,
        "cik": cik,
        "source": source,
        "source_url": COMPANYFACTS_URL.format(cik=cik),
        "source_posture": fsi_engine.POSTURE_AUTHORITATIVE,
        "retrieved_at_utc": retrieved_at_utc,
        "fiscal_years": years,
        "filings": {accession: filings[accession] for accession in sorted(filings)},
        "items": items,
    }
    record["facts_sha256"] = record_sha256(record)
    return record


def merge_filing_facts(
    company_facts: dict[str, Any], filing_facts: dict[str, Any], *, form: str, filed: str
) -> dict[str, Any]:
    """Add one filing's facts to company facts that do not carry that filing yet.

    ``filing_facts`` is what ``edgar_source.instance_to_company_facts`` returns; its rows
    get the filing's form and date, which an XBRL instance does not state per fact.
    """
    merged = json.loads(json.dumps(company_facts, default=str))
    us_gaap = merged.setdefault("facts", {}).setdefault("us-gaap", {})
    for tag, node in filing_facts.get("facts", {}).get("us-gaap", {}).items():
        for unit, rows in node.get("units", {}).items():
            target = us_gaap.setdefault(tag, {"units": {}}).setdefault("units", {}).setdefault(unit, [])
            target.extend({**row, "val": str(row["val"]), "form": form, "filed": filed} for row in rows)
    return merged


# --------------------------------------------------------------------------- views


def _cell(record: dict[str, Any], key: str, end: str | None) -> dict[str, str] | None:
    return (record["items"].get(key) or {}).get("values", {}).get(end) if end else None


def _value_tag(record: dict[str, Any], key: str, end: str | None) -> str | None:
    value = _cell(record, key, end)
    return (value.get("tag") or record["items"][key]["tag"]) if value else None


def _number(record: dict[str, Any], key: str, end: str | None) -> Decimal | None:
    value = _cell(record, key, end)
    return Decimal(value["value"]) if value else None


def _one_basis(first: dict[str, str], second: dict[str, str]) -> bool:
    """Two values can be combined when one filing reports both, or neither was ever restated."""
    return first["accession_number"] == second["accession_number"] or (
        "restated_from" not in first and "restated_from" not in second
    )


MIXED_BASIS_REASON = "inputs come from different filings and one was restated, so they may be on different bases"


# A growth rate describes a change of scale. It is not reported when the prior year is zero or negative,
# when the figure turns negative, or when the change is more than ten times the prior-year figure: the
# arithmetic is defined, but a base that small makes the percentage say nothing (Intel, fiscal 2024:
# operating income went from 93 to -11,678 USD million, which is -12,656.99% as a percentage). The
# reported figures themselves are still shown, and the limit is a convention of this tool.
GROWTH_RATE_LIMIT_PCT = Decimal("1000")


def computed_metrics(record: dict[str, Any], end: str, prior_end: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(values, reasons)``: each metric in ``COMPUTED_METRICS`` is in exactly one of them.

    A metric is computed only from values that are on one basis (see ``_one_basis``) and,
    for a year-over-year change, reported under the same XBRL tag in both years.
    """
    values: dict[str, str] = {}
    reasons: dict[str, str] = {}

    def inputs(metric: str, *needs: tuple[str, str | None]) -> list[Decimal] | None:
        cells: list[dict[str, str]] = []
        for key, at in needs:
            found = _cell(record, key, at)
            if found is None:
                if at is None:
                    reasons[metric] = "the record holds no prior fiscal year"
                else:
                    reasons[metric] = f"{'' if at == end else 'prior-year '}{key} is not tagged"
                return None
            cells.append(found)
        if any(not _one_basis(a, b) for index, a in enumerate(cells) for b in cells[index + 1 :]):
            reasons[metric] = MIXED_BASIS_REASON
            return None
        return [Decimal(found["value"]) for found in cells]

    def change(metric: str, key: str, *, as_growth: bool) -> None:
        numbers = inputs(metric, (key, end), (key, prior_end))
        if numbers is None:
            return
        current, prior = numbers
        if _value_tag(record, key, end) != _value_tag(record, key, prior_end):
            reasons[metric] = f"{key} is reported under different XBRL tags in the two years"
        elif not as_growth:
            values[metric] = edgar_source.decimal_text(current - prior)
        elif prior <= 0:
            reasons[metric] = f"prior-year {key} is zero or negative, so a growth rate is not meaningful"
        elif current < 0:
            reasons[metric] = f"{key} is negative after a positive prior year, so a growth rate is not meaningful"
        elif abs((current - prior) / prior * HUNDRED) > GROWTH_RATE_LIMIT_PCT:
            reasons[metric] = (
                f"the change is more than ten times prior-year {key}, a base too small for a growth rate "
                "to be meaningful"
            )
        else:
            values[metric] = fsi_engine.q2((current - prior) / prior * HUNDRED)

    def share_of_revenue(metric: str, *keys: str) -> None:
        """``keys[0]`` minus any further keys, as a percentage of revenue."""
        numbers = inputs(metric, *((key, end) for key in keys), ("revenue", end))
        if numbers is None:
            return
        revenue = numbers[-1]
        if revenue <= 0:
            reasons[metric] = "revenue is zero or negative"
        else:
            values[metric] = fsi_engine.q2((numbers[0] - sum(numbers[1:-1], Decimal("0"))) / revenue * HUNDRED)

    change("revenue_change", "revenue", as_growth=False)
    change("revenue_growth_pct", "revenue", as_growth=True)
    change("operating_income_growth_pct", "operating_income", as_growth=True)
    change("net_income_change", "net_income", as_growth=False)
    change("net_income_growth_pct", "net_income", as_growth=True)
    change("diluted_eps_growth_pct", "diluted_eps", as_growth=True)
    share_of_revenue("gross_margin_pct", "gross_profit")
    share_of_revenue("operating_margin_pct", "operating_income")
    share_of_revenue("pretax_margin_pct", "pretax_income")
    share_of_revenue("net_margin_pct", "net_income")
    share_of_revenue("rd_pct_of_revenue", "research_and_development")

    tax_rate = inputs("effective_tax_rate_pct", ("effective_tax_rate", end))
    if tax_rate is not None:
        values["effective_tax_rate_pct"] = fsi_engine.q2(tax_rate[0] * HUNDRED)

    cash_flow = inputs("free_cash_flow", ("operating_cash_flow", end), ("capital_expenditure", end))
    if cash_flow is not None:
        values["free_cash_flow"] = edgar_source.decimal_text(cash_flow[0] - cash_flow[1])
    share_of_revenue("free_cash_flow_margin_pct", "operating_cash_flow", "capital_expenditure")
    return values, reasons


def company_view(record: dict[str, Any], *, years: int = MAX_VIEW_YEARS, origin: str) -> dict[str, Any]:
    """What the agent tool returns for one company: reported values, computed metrics and sources."""
    all_years = record["fiscal_years"]
    shown: list[dict[str, Any]] = []
    cited: set[str] = set()
    for index, year in enumerate(all_years[:years]):
        end = year["period_end"]
        prior_end = all_years[index + 1]["period_end"] if index + 1 < len(all_years) else None
        reported: dict[str, dict[str, str]] = {}
        for item in LINE_ITEMS:
            value = (record["items"].get(item.key) or {}).get("values", {}).get(end)
            if value is not None:
                reported[item.key] = dict(value)
                cited.add(value["accession_number"])
        computed, not_computed = computed_metrics(record, end, prior_end)
        shown.append(
            {
                "fiscal_year": year["fiscal_year"],
                "period_start": year["period_start"],
                "period_end": end,
                "reported": reported,
                "computed": computed,
                "not_computed": not_computed,
                "not_tagged": [item.key for item in LINE_ITEMS if item.key not in reported],
            }
        )
    return {
        "ticker": record["ticker"],
        "name": record["name"],
        "cik": record["cik"],
        "data_origin": origin,
        "source": record["source"],
        "source_url": record["source_url"],
        "source_posture": record["source_posture"],
        "retrieved_at_utc": record["retrieved_at_utc"],
        "facts_sha256": record["facts_sha256"],
        "tags": {key: entry["tag"] for key, entry in record["items"].items()},
        "filings": {accession: record["filings"][accession] for accession in sorted(cited)},
        "fiscal_years": shown,
    }


def comparison_rows(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per company for its latest fiscal year; a missing figure is None, never zero."""
    rows: list[dict[str, Any]] = []
    for view in views:
        latest = view["fiscal_years"][0]
        row: dict[str, Any] = {
            "ticker": view["ticker"],
            "name": view["name"],
            "fiscal_year": latest["fiscal_year"],
            "period_end": latest["period_end"],
        }
        for key in COMPARISON_ITEMS:
            row[key] = (latest["reported"].get(key) or {}).get("value")
        for key in COMPARISON_METRICS:
            row[key] = latest["computed"].get(key)
        rows.append(row)
    return rows


def legends() -> dict[str, dict[str, dict[str, str]]]:
    """Labels and units of the line items, and labels, units and formulas of the computed metrics."""
    return {
        "line_items": {item.key: {"label": item.label, "unit": item.unit} for item in LINE_ITEMS},
        "computed_metrics": {
            metric.key: {"label": metric.label, "unit": metric.unit, "formula": metric.formula}
            for metric in COMPUTED_METRICS
        },
    }


# --------------------------------------------------------------------------- bundled records


def checked_record(record: dict[str, Any], where: str) -> dict[str, Any]:
    """Return ``record`` if its stored hash matches its content.

    Raises:
        RecordIntegrityError: the stored ``facts_sha256`` is not the hash of the values.
    """
    if record.get("facts_sha256") != record_sha256(record):
        raise RecordIntegrityError(f"{where}: the record does not match its facts_sha256.")
    return record


def load_bundle(path: Path | None = None) -> dict[str, Any]:
    """Load the bundled reported-financials records (see the file's metadata for provenance)."""
    with (path or BUNDLE_PATH).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def bundled_record(ticker: str, path: Path | None = None) -> dict[str, Any] | None:
    """The bundled record of one ticker, hash-checked, or None when the ticker is not bundled."""
    record = load_bundle(path)["companies"].get(ticker)
    return checked_record(record, f"bundled record {ticker}") if record else None


def bundle_summary(path: Path | None = None) -> dict[str, Any]:
    """What an evaluation run records about the bundled records: identity, hash and failed hash checks."""
    bundle = load_bundle(path)
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "dataset_id": bundle["metadata"]["dataset_id"],
        "dataset_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "as_of_utc": bundle["metadata"]["as_of_utc"],
        "source_posture": bundle["metadata"]["source_posture"],
        "record_hash_mismatches": [
            ticker
            for ticker, record in bundle["companies"].items()
            if record.get("facts_sha256") != record_sha256(record)
        ],
    }


def dumps_bundle(bundle: dict[str, Any]) -> str:
    """Serialise the bundle with one value per line, so a change to one figure is a one-line diff."""
    text = json.dumps(bundle, indent=1, ensure_ascii=False)
    # Objects that hold only scalars (a value, a filing, a fiscal year) go on one line.
    return re.sub(r"\{\n\s+([^{}\[\]]+?)\n\s+\}", lambda match: "{" + re.sub(r"\n\s+", " ", match.group(1)) + "}", text) + "\n"


# --------------------------------------------------------------------------- building the bundle


class BuildError(RuntimeError):
    """The bundle cannot be built from the given cache."""


def _same_as_sample_book(ticker: str, record: dict[str, Any], company: dict[str, Any]) -> None:
    """The figures the sample book already holds must be the ones this record reports."""
    end = company["filing"]["report_date"]
    if record["fiscal_years"][0]["period_end"] != end:
        raise BuildError(
            f"{ticker}: latest fiscal year ends {record['fiscal_years'][0]['period_end']}, "
            f"but the sample book's filing reports {end}."
        )
    for book_key, key in (
        ("revenue_m", "revenue"),
        ("diluted_eps", "diluted_eps"),
        ("diluted_shares_m", "diluted_shares"),
        ("effective_tax_rate", "effective_tax_rate"),
        ("long_term_debt_m", "long_term_debt"),
    ):
        mine, tag = _number(record, key, end), _value_tag(record, key, end)
        raw_book = company["xbrl"].get(book_key)
        book_value = Decimal(raw_book) if raw_book is not None else None
        book_src = company["xbrl_sources"].get(book_key)
        book_tag = book_src["tag"] if isinstance(book_src, dict) else None
        if mine != book_value or tag != book_tag:
            raise BuildError(f"{ticker}.{key}: {mine} ({tag}) here, {book_value} ({book_tag}) in the sample book.")


def build_bundle(cache: edgar_source.EdgarCache, sample_book: dict[str, Any]) -> dict[str, Any]:
    """Build a record for every issuer of the sample book from cached (or fetched) SEC company facts."""
    companies: dict[str, dict[str, Any]] = {}
    retrieved: list[str] = []
    for ticker, company in sample_book["companies"].items():
        filing = company["filing"]
        facts, facts_retrieved = cache.company_facts(ticker, company["cik"])
        source = "SEC EDGAR company facts API"
        if not edgar_source.has_accession_facts(facts, filing["accession_number"]):
            instance, instance_url, instance_retrieved = cache.xbrl_instance(
                ticker, company["cik"], filing["accession_number"], filing["primary_document"]
            )
            facts = merge_filing_facts(
                facts,
                edgar_source.instance_to_company_facts(instance, filing["accession_number"]),
                form=filing["form"],
                filed=filing["filing_date"],
            )
            source += (
                f"; accession {filing['accession_number']} was not in it yet and is read from the "
                f"filing's XBRL instance ({instance_url})"
            )
            facts_retrieved = max(facts_retrieved, instance_retrieved)
        record = build_record(
            facts,
            ticker=ticker,
            name=company["name"],
            cik=company["cik"],
            source=source,
            retrieved_at_utc=facts_retrieved,
        )
        _same_as_sample_book(ticker, record, company)
        companies[ticker] = record
        retrieved.append(facts_retrieved)
    return {
        "metadata": {
            "dataset_id": DATASET_ID,
            "as_of_utc": max(retrieved),
            "source_posture": fsi_engine.POSTURE_AUTHORITATIVE,
            "data_notice": DATA_NOTICE,
            "description": (
                f"{len(companies)} issuers, up to {KEEP_FISCAL_YEARS} fiscal years and {len(LINE_ITEMS)} line "
                "items each, built by scripts/reported_financials.py. facts_sha256 is the SHA-256 of a record's "
                "fiscal years and values (reported_financials.record_sha256)."
            ),
            "basis": BASIS_NOTE,
        },
        "companies": companies,
    }


def without_retrieval_times(bundle: dict[str, Any]) -> dict[str, Any]:
    """Copy of a bundle with retrieval timestamps removed, for comparing two builds."""
    clone = json.loads(json.dumps(bundle))
    clone["metadata"].pop("as_of_utc", None)
    for record in clone["companies"].values():
        record.pop("retrieved_at_utc", None)
    return clone


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the bundled reported-financials records from SEC EDGAR.")
    parser.add_argument("--cache-dir", default=str(SKILL_ROOT / ".edgar_cache"), help="Directory for downloaded files")
    parser.add_argument("--offline", action="store_true", help="Fail instead of fetching anything that is not cached")
    parser.add_argument("--check", action="store_true", help="Compare the rebuilt records with the bundled file")
    parser.add_argument("--output", default=str(BUNDLE_PATH), help="Where to write the records")
    args = parser.parse_args()

    cache = edgar_source.EdgarCache(Path(args.cache_dir), offline=args.offline)
    try:
        bundle = build_bundle(cache, fsi_engine.load_sample_book())
    except (BuildError, NoAnnualFacts, FileNotFoundError, RuntimeError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    values = sum(len(entry["values"]) for record in bundle["companies"].values() for entry in record["items"].values())
    summary = f"{len(bundle['companies'])} issuers, {values} values, {cache.network_requests} request(s) to sec.gov"
    if args.check:
        bundled = json.loads(Path(args.output).read_text(encoding="utf-8"))
        if without_retrieval_times(bundled) != without_retrieval_times(bundle):
            print(f"check failed: {args.output} differs from a fresh build ({summary}).", file=sys.stderr)
            return 1
        print(f"check passed: {args.output} matches a fresh build ({summary}).")
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps_bundle(bundle), encoding="utf-8")
    print(f"wrote {output} ({summary}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
