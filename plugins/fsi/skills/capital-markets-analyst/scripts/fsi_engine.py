"""Deterministic Form 10-K scenario engine, XBRL EPS tie-out, and BigQuery Graph generator.

All financial scaling, tax adjustments, and EPS conversions execute strictly in
Python ``decimal.Decimal`` with explicit provenance spans and compliance guardrails.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
SAMPLE_BOOK_PATH = SKILL_ROOT / "assets" / "sample_10k_book" / "sp500_sample_book.json"
EVAL_YAML_PATH = SKILL_ROOT / "EVAL.yaml"

TWOPLACES = Decimal("0.01")
FOURPLACES = Decimal("0.0001")
EXTRAPOLATION_LIMIT = Decimal("2")

# The tax effect of a scaled impact uses the effective tax rate the filing reports in XBRL. That rate is
# the year's average, and it stands in for the marginal rate only in an ordinary year. A loss or
# near-breakeven year, a valuation allowance or a one-off item can push it below zero or towards 100%,
# and such a rate says nothing about the tax on one more dollar of income. A reported rate outside this
# range is not used: the US federal statutory rate is applied instead, and the holding says so.
USABLE_TAX_RATE_MIN = Decimal("0")
USABLE_TAX_RATE_MAX = Decimal("0.50")
US_FEDERAL_STATUTORY_TAX_RATE = Decimal("0.21")
TAX_BASIS_XBRL_EFFECTIVE = "xbrl_effective_rate"
TAX_BASIS_STATUTORY = "us_federal_statutory_rate"

# Source-posture labels. The posture is read from the dataset and never defaulted in code.
POSTURE_AUTHORITATIVE = "authoritative_filing"
POSTURE_ILLUSTRATIVE = "illustrative_sample"
POSTURE_UNVERIFIED = "customer_submitted_unverified"

# How a filing discloses a sensitivity. Only ``income_statement`` converts to earnings.
STATUS_INCOME_STATEMENT = "income_statement"
STATUS_FAIR_VALUE_ONLY = "fair_value_only"
STATUS_NOT_CONVERTIBLE = "not_convertible"
STATUS_NOT_QUANTIFIED = "not_quantified"
STATUS_INCORPORATED_BY_REFERENCE = "incorporated_by_reference"
STATUS_NONE_FOUND = "none_found"
SENSITIVITY_STATUSES = (
    STATUS_INCOME_STATEMENT,
    STATUS_FAIR_VALUE_ONLY,
    STATUS_NOT_CONVERTIBLE,
    STATUS_NOT_QUANTIFIED,
    STATUS_INCORPORATED_BY_REFERENCE,
    STATUS_NONE_FOUND,
)

# Unit constant used to convert a per-barrel fuel sensitivity to a per-gallon price.
GALLONS_PER_BARREL = Decimal("42")
TENPLACES = Decimal("0.0000000001")

ADVISORY_PROHIBITED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:strong\s+)?(?:buy|sell)\b", re.IGNORECASE),
    re.compile(r"\brecommend(?:s|ed|ing)?\s+(?:buying|selling|holding)\b", re.IGNORECASE),
    re.compile(r"\b(?:overweight|underweight|outperform|underperform)\s+rating\b", re.IGNORECASE),
    re.compile(r"\bprice\s+target\s*(?:of|:|\$)\s*\$?\d+", re.IGNORECASE),
)


def _quantized_text(val: Decimal, exponent: Decimal) -> str:
    quantized = val.quantize(exponent, rounding=ROUND_HALF_UP)
    # A zero result keeps no sign: "-0.00" would read as a small loss.
    return str(abs(quantized) if quantized == 0 else quantized)


def q2(val: Decimal) -> str:
    """Quantize a Decimal to 2 fractional digits using ROUND_HALF_UP."""
    return _quantized_text(val, TWOPLACES)


def q4(val: Decimal) -> str:
    """Quantize a Decimal to 4 fractional digits using ROUND_HALF_UP."""
    return _quantized_text(val, FOURPLACES)


def percent_text(fraction: Decimal) -> str:
    """A fraction as a percentage without trailing zeros: 0.983 -> '98.3%', 0.21 -> '21%'."""
    return f"{format((fraction * Decimal(100)).normalize(), 'f')}%"


def conversion_tax_rate(reported_rate: Decimal) -> tuple[Decimal, str]:
    """Return the rate used to tax-effect a scaled impact, and which rate that is.

    The filing's effective tax rate is used when it lies in the usable range; otherwise the
    US federal statutory rate is. The second value is ``TAX_BASIS_XBRL_EFFECTIVE`` or
    ``TAX_BASIS_STATUTORY``.
    """
    if USABLE_TAX_RATE_MIN <= reported_rate <= USABLE_TAX_RATE_MAX:
        return reported_rate, TAX_BASIS_XBRL_EFFECTIVE
    return US_FEDERAL_STATUTORY_TAX_RATE, TAX_BASIS_STATUTORY


def load_sample_book(path: Path | None = None) -> dict[str, Any]:
    """Load the bundled sample book (10-K passages and XBRL facts; see its metadata for provenance)."""
    target = path or SAMPLE_BOOK_PATH
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sensitivity_passages(info: dict[str, Any]) -> list[dict[str, str]]:
    """Passages bundled for one sensitivity: the quoted passage first, then supporting ones."""
    passages = info.get("passages")
    if passages:
        return [
            {
                "kind": passage.get("kind", "sentence"),
                "section": passage.get("section", info["section"]),
                "text": passage["text"],
            }
            for passage in passages
        ]
    if info.get("verbatim_quote"):
        return [{"kind": "sentence", "section": info["section"], "text": info["verbatim_quote"]}]
    return []


def canonical_excerpt_passages(dataset: dict[str, Any], ticker: str) -> list[dict[str, str]]:
    """Return the passages bundled for one issuer document, in canonical order.

    Order: rate passages, oil passages, the disclosed-link passages cited to this
    document, then any period-over-period evidence passage. A table row is one passage
    (its cell texts joined with " | "). A passage that is cited twice is listed once.
    """
    company = dataset["companies"][ticker]
    passages: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(section: str, text: str) -> None:
        if (section, text) not in seen:
            seen.add((section, text))
            passages.append({"section": section, "text": text})

    for factor in ("rate", "oil"):
        for passage in sensitivity_passages(company["sensitivities"][factor]):
            add(passage["section"], passage["text"])
    for edge in sorted(dataset["supply_chain_edges"], key=lambda item: item["edge_id"]):
        if edge["doc_id"] == company["filing_doc_id"]:
            add(edge["section"], edge["verbatim_quote"])
    watch = company.get("yoy_delta") or {}
    if watch.get("evidence_quote"):
        add(watch.get("evidence_section", "Period-over-period evidence"), watch["evidence_quote"])
    rubric = watch.get("credit_rubric") or {}
    if rubric.get("evidence_quote"):
        add("Credit rubric evidence", rubric["evidence_quote"])
    return passages


def compute_excerpt_sha256(dataset: dict[str, Any], ticker: str) -> str:
    """SHA-256 of the passages bundled for one issuer document.

    This hashes the excerpt text shipped in the dataset, so anyone can recompute
    it. It is not a hash of the issuer's SEC filing; a filing-sourced dataset records
    that separately as ``filing.document_sha256``.
    """
    payload = {
        "doc_id": dataset["companies"][ticker]["filing_doc_id"],
        "passages": canonical_excerpt_passages(dataset, ticker),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_fingerprint(dataset: dict[str, Any]) -> str:
    """SHA-256 of the whole dataset, used to tie an evaluation run to the data it ran on."""
    canonical = json.dumps(dataset, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_excerpt_hashes(dataset: dict[str, Any]) -> list[str]:
    """Return tickers whose stored ``excerpt_sha256`` does not match their bundled passages."""
    return [
        ticker
        for ticker, company in dataset["companies"].items()
        if company.get("excerpt_sha256") != compute_excerpt_sha256(dataset, ticker)
    ]


def enforce_non_advisory_guardrail(text: str) -> dict[str, Any]:
    """Deterministically block buy/sell/hold recommendations and price targets."""
    violations: list[str] = []
    for pattern in ADVISORY_PROHIBITED_PATTERNS:
        for match in pattern.finditer(text):
            violations.append(match.group(0))
    return {
        "allowed": len(violations) == 0,
        "violations": violations,
        "policy": "non_investment_advice",
    }


def inspect_pdf_posture(
    extracted_text: str,
    *,
    filename: str = "uploaded_document.pdf",
    offline_mode: bool = True,
    source_posture: str = "customer_submitted_unverified",
) -> dict[str, Any]:
    """Detect rasterized/scanned PDFs in offline Quick Start without guessing digits."""
    stripped = extracted_text.strip()
    if offline_mode and len(stripped) < 20:
        return {
            "filename": filename,
            "status": "unassessable_offline",
            "source_posture": source_posture,
            "message": (
                "unassessable_offline: the PDF has no extractable text layer. OCR is not "
                "implemented in this skill; supply a PDF with a text layer, or paste the "
                "passage as text."
            ),
        }
    return {
        "filename": filename,
        "status": "assessable",
        "source_posture": source_posture,
        "char_count": len(stripped),
    }


def _rate_basis(info: dict[str, Any]) -> Decimal | None:
    """Disclosed rate basis in basis points, or None when no income-statement amount is disclosed."""
    if info.get("status", STATUS_INCOME_STATEMENT) != STATUS_INCOME_STATEMENT:
        return None
    return Decimal(str(info["disclosed_basis_bps"]))


def _oil_price_per_basis_unit(info: dict[str, Any]) -> Decimal:
    """Reference price of the unit a fuel sensitivity is stated in (USD per gallon or per barrel)."""
    unit = info.get("disclosed_basis_unit")
    reference = Decimal(str(info["reference_price_usd_per_gallon"]))
    if unit == "usd_per_gallon":
        return reference
    if unit == "usd_per_barrel":
        return reference * GALLONS_PER_BARREL
    raise ValueError(f"Unsupported disclosed_basis_unit '{unit}'.")


def _oil_basis_pct(info: dict[str, Any]) -> Decimal | None:
    """Percent price move equal to the disclosed fuel basis, or None when nothing comparable is disclosed.

    A filing may state the basis in percent, per gallon or per barrel. A per-unit basis is
    converted with the reference price bundled next to it (basis / reference price).
    """
    if info.get("status", STATUS_INCOME_STATEMENT) != STATUS_INCOME_STATEMENT:
        return None
    if info.get("disclosed_basis_pct") is not None:
        return Decimal(str(info["disclosed_basis_pct"]))
    return Decimal(str(info["disclosed_basis_value"])) * Decimal("100") / _oil_price_per_basis_unit(info)


def _oil_multiplier(info: dict[str, Any], oil_pct: Decimal) -> Decimal:
    """Scenario move divided by the disclosed basis, computed without rounding the percent basis."""
    if info.get("disclosed_basis_pct") is not None:
        basis = Decimal(str(info["disclosed_basis_pct"]))
        return oil_pct / basis if basis != 0 else Decimal("0")
    basis_value = Decimal(str(info["disclosed_basis_value"]))
    if basis_value == 0:
        return Decimal("0")
    return oil_pct * _oil_price_per_basis_unit(info) / (basis_value * Decimal("100"))


def _oil_basis_label(info: dict[str, Any], basis_pct: Decimal) -> str:
    """Describe the disclosed fuel basis for a warning."""
    unit = info.get("disclosed_basis_unit")
    if info.get("disclosed_basis_pct") is not None or unit is None:
        return f"{basis_pct}% linear basis"
    unit_text = {"usd_per_gallon": "USD per gallon", "usd_per_barrel": "USD per barrel"}[unit]
    return (
        f"{info['disclosed_basis_value']} {unit_text} basis "
        f"({q4(basis_pct)}% of the {info['reference_price_usd_per_gallon']} USD per gallon reference price)"
    )


def _provenance_entry(
    factor: str,
    info: dict[str, Any],
    *,
    doc_id: str,
    sha256: str,
    source_posture: str,
) -> dict[str, Any]:
    """Citation for one sensitivity. No ``verbatim_quote`` key means the filing gave no passage."""
    entry: dict[str, Any] = {
        "factor": factor,
        "doc_id": doc_id,
        "sha256": sha256,
        "filing_section": info["section"],
        "disclosure_status": info.get("status", STATUS_INCOME_STATEMENT),
        "source_posture": source_posture,
    }
    passages = sensitivity_passages(info)
    if passages:
        entry["filing_section"] = passages[0]["section"]
        entry["verbatim_quote"] = passages[0]["text"]
        entry["supporting_passages"] = passages[1:]
    return entry


def compute_company_shock(
    company: dict[str, Any],
    rate_bps: Decimal,
    oil_pct: Decimal,
    *,
    source_posture: str,
) -> dict[str, Any]:
    """Scale disclosed sensitivities and convert them to diluted EPS with the issuer's XBRL facts.

    A sensitivity whose status is not ``income_statement`` (fair-value only, not
    convertible, not quantified, incorporated by reference, or none found) contributes
    nothing to earnings. Its disclosed basis and disclosed amount are reported as ``None``:
    the filing states no such figure, which is not the same as stating zero.
    """
    xbrl = company["xbrl"]
    sens = company["sensitivities"]

    rate_info = sens["rate"]
    oil_info = sens["oil"]

    rate_basis = _rate_basis(rate_info)
    rate_disclosed_m = Decimal(str(rate_info["pre_tax_impact_m"])) if rate_basis is not None else Decimal("0")
    rate_mult = rate_bps / rate_basis if rate_basis else Decimal("0")
    rate_scaled_m = rate_disclosed_m * rate_mult

    oil_basis = _oil_basis_pct(oil_info)
    oil_disclosed_m = Decimal(str(oil_info["pre_tax_impact_m"])) if oil_basis is not None else Decimal("0")
    oil_mult = _oil_multiplier(oil_info, oil_pct) if oil_basis else Decimal("0")
    oil_scaled_m = oil_disclosed_m * oil_mult

    total_pre_tax_m = rate_scaled_m + oil_scaled_m
    reported_tax_rate = Decimal(str(xbrl["effective_tax_rate"]))
    tax_rate, tax_basis = conversion_tax_rate(reported_tax_rate)
    after_tax_m = total_pre_tax_m * (Decimal("1") - tax_rate)

    diluted_shares_m = Decimal(str(xbrl["diluted_shares_m"]))
    baseline_eps = Decimal(str(xbrl["diluted_eps"]))
    eps_delta = after_tax_m / diluted_shares_m
    # A percentage of a zero or negative base is not meaningful, so none is reported for a loss year.
    eps_pct_of_fy = (eps_delta / baseline_eps) * Decimal("100") if baseline_eps > 0 else None

    # Notes on the conversion to after-tax earnings and EPS. They are data facts of the issuer, so they
    # do not depend on the scenario, and they are given only where the filing has a figure to convert.
    convertible = STATUS_INCOME_STATEMENT in (
        rate_info.get("status", STATUS_INCOME_STATEMENT),
        oil_info.get("status", STATUS_INCOME_STATEMENT),
    )
    conversion_notes: list[str] = []
    if convertible and tax_basis == TAX_BASIS_STATUTORY:
        conversion_notes.append(
            f"Tax: after-tax impact and EPS use the US federal statutory rate of "
            f"{percent_text(US_FEDERAL_STATUTORY_TAX_RATE)}, not the effective tax rate of "
            f"{percent_text(reported_tax_rate)} that the filing reports in XBRL. An effective rate outside "
            f"{percent_text(USABLE_TAX_RATE_MIN)} to {percent_text(USABLE_TAX_RATE_MAX)} reflects an unusual year "
            "(a loss or near-breakeven year, a valuation allowance or a one-off item) and is not a marginal "
            "rate. The statutory rate is an assumption of this tool, not a figure from the filing."
        )
    if convertible and baseline_eps <= 0:
        conversion_notes.append(
            f"EPS: the change is not shown as a percentage of fiscal-year EPS. Reported diluted EPS is "
            f"{xbrl['diluted_eps']} USD per share, and a percentage of a zero or negative base is not meaningful."
        )

    rate_extrapolated = abs(rate_mult) > EXTRAPOLATION_LIMIT and rate_disclosed_m != 0
    oil_extrapolated = abs(oil_mult) > EXTRAPOLATION_LIMIT and oil_disclosed_m != 0
    extrapolation_flags: list[str] = []
    if rate_extrapolated:
        extrapolation_flags.append(
            f"Rate shock ({rate_bps:+} bp) exceeds 2x disclosed {rate_basis} bp linear basis ({q2(abs(rate_mult))}x)."
        )
    if oil_extrapolated and oil_basis is not None:
        extrapolation_flags.append(
            f"Oil shock ({oil_pct:+}%) exceeds 2x disclosed {_oil_basis_label(oil_info, oil_basis)} "
            f"({q2(abs(oil_mult))}x)."
        )

    non_comparable_flags: list[str] = []
    coverage_notes: list[str] = []
    basis_notes: list[str] = []
    for label, info in (("Rate", rate_info), ("Oil", oil_info)):
        if info.get("non_comparable_flag"):
            non_comparable_flags.append(f"{label}: {info['non_comparable_flag']}")
        if info.get("coverage_note"):
            coverage_notes.append(f"{label}: {info['coverage_note']}")
        for key in ("derivation", "basis_note"):
            if info.get(key):
                basis_notes.append(f"{label}: {info[key]}")

    doc_id = company["filing_doc_id"]
    sha256 = company["excerpt_sha256"]
    if oil_basis is None:
        oil_basis_text = None
    elif oil_info.get("disclosed_basis_pct") is not None:
        oil_basis_text = str(oil_basis)
    else:
        oil_basis_text = str(oil_basis.quantize(TENPLACES, rounding=ROUND_HALF_UP))

    return {
        "ticker": company["ticker"],
        "name": company["name"],
        "cik": company["cik"],
        "sector": company["sector"],
        "xbrl": {
            "revenue_m": xbrl["revenue_m"],
            "effective_tax_rate": xbrl["effective_tax_rate"],
            "diluted_shares_m": xbrl["diluted_shares_m"],
            "diluted_eps": xbrl["diluted_eps"],
            "long_term_debt_m": xbrl.get("long_term_debt_m"),
        },
        "disclosed_basis": {
            "rate_status": rate_info.get("status", STATUS_INCOME_STATEMENT),
            "rate_basis_bps": str(rate_basis) if rate_basis is not None else None,
            "rate_pre_tax_m": q2(rate_disclosed_m) if rate_basis is not None else None,
            "oil_status": oil_info.get("status", STATUS_INCOME_STATEMENT),
            "oil_basis_pct": oil_basis_text,
            "oil_pre_tax_m": q2(oil_disclosed_m) if oil_basis is not None else None,
            "oil_basis_unit": oil_info.get("disclosed_basis_unit"),
            "oil_basis_value": oil_info.get("disclosed_basis_value"),
            "oil_reference_price_usd_per_gallon": oil_info.get("reference_price_usd_per_gallon"),
        },
        "scaled_impact": {
            "rate_multiplier": q2(rate_mult),
            "oil_multiplier": q2(oil_mult),
            "rate_pre_tax_m": q2(rate_scaled_m),
            "oil_pre_tax_m": q2(oil_scaled_m),
            "total_pre_tax_m": q2(total_pre_tax_m),
            "after_tax_m": q2(after_tax_m),
            "eps_delta": q4(eps_delta),
            "eps_pct_of_fy": q2(eps_pct_of_fy) if eps_pct_of_fy is not None else None,
            # The rate that entered the after-tax figure: the XBRL effective rate as filed, or the statutory rate.
            "tax_rate_applied": (
                xbrl["effective_tax_rate"] if tax_basis == TAX_BASIS_XBRL_EFFECTIVE else str(tax_rate)
            ),
            "tax_rate_basis": tax_basis,
        },
        "conversion_notes": conversion_notes,
        "extrapolation_warning": len(extrapolation_flags) > 0,
        "extrapolation_flags": extrapolation_flags,
        "non_comparable_warning": len(non_comparable_flags) > 0,
        "non_comparable_flags": non_comparable_flags,
        "coverage_notes": coverage_notes,
        "basis_notes": basis_notes,
        "provenance": [
            _provenance_entry("interest_rate", rate_info, doc_id=doc_id, sha256=sha256, source_posture=source_posture),
            _provenance_entry("commodity_oil", oil_info, doc_id=doc_id, sha256=sha256, source_posture=source_posture),
        ],
    }


def traverse_supply_chain_contagion(
    dataset: dict[str, Any],
    company_results: dict[str, dict[str, Any]],
    selected_tickers: list[str],
) -> dict[str, Any]:
    """Traverse 1st -> 2nd -> 3rd-order customer/supplier exposure across disclosed 10-K links."""
    companies = dataset["companies"]
    all_edges = dataset["supply_chain_edges"]
    selected_set = set(selected_tickers)
    posture = dataset["metadata"]["source_posture"]

    # 1st-order shocked issuers: holdings in the selected book with >= $20M direct P&L shock.
    hop1_tickers: set[str] = set()
    for ticker in selected_tickers:
        res = company_results[ticker]
        pre_tax_abs = abs(Decimal(res["scaled_impact"]["total_pre_tax_m"]))
        if pre_tax_abs >= Decimal("20.0"):
            hop1_tickers.add(ticker)
    if not hop1_tickers and selected_tickers:
        hop1_tickers.add(selected_tickers[0])

    # Directional contagion matching BigQuery GQL:
    # (hop3)-[e2]->(hop2)-[e1]->(shocked_hop1)
    node_hop: dict[str, int] = {t: 1 for t in hop1_tickers}
    active_edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()

    # Hop 2 discovery: suppliers/partners directly exposed to Hop 1 shocked customers
    hop2_tickers: set[str] = set()
    for edge in all_edges:
        src, dst = edge["src"], edge["dst"]
        if dst in hop1_tickers and (src in selected_set or dst in selected_set):
            seen_edge_ids.add(edge["edge_id"])
            if src not in node_hop:
                node_hop[src] = 2
                hop2_tickers.add(src)
            active_edges.append(_format_graph_edge(edge, companies, hop_order=2, source_posture=posture))

    # Hop 3 discovery: upstream suppliers/lenders feeding into Hop 2 ripple nodes (e.g. HXL/GE/RTX/JPM -> BA)
    for edge in all_edges:
        if edge["edge_id"] in seen_edge_ids:
            continue
        src, dst = edge["src"], edge["dst"]
        if dst in hop2_tickers:
            seen_edge_ids.add(edge["edge_id"])
            if src not in node_hop:
                node_hop[src] = 3
            active_edges.append(_format_graph_edge(edge, companies, hop_order=3, source_posture=posture))

    # Include any remaining book holdings as baseline portfolio nodes (hop 0 = unshocked direct holding)
    for ticker in selected_tickers:
        node_hop.setdefault(ticker, 1 if Decimal(company_results[ticker]["scaled_impact"]["total_pre_tax_m"]) != 0 else 0)

    nodes: list[dict[str, Any]] = []
    for ticker, hop in sorted(node_hop.items(), key=lambda kv: (kv[1], kv[0])):
        comp = companies[ticker]
        shock = company_results.get(ticker)
        nodes.append(
            {
                "ticker": ticker,
                "name": comp["name"],
                "sector": comp["sector"],
                "in_selected_book": ticker in selected_set,
                "hop_order": hop,
                "hop_label": {
                    0: "Direct Holding (No Converted Shock)",
                    1: "1st-Order Direct Shock",
                    2: "2nd-Order Customer/Supplier Ripple",
                    3: "3rd-Order Upstream Contagion",
                }[hop],
                "total_pre_tax_m": shock["scaled_impact"]["total_pre_tax_m"] if shock else "0.00",
                "eps_delta": shock["scaled_impact"]["eps_delta"] if shock else "0.0000",
            }
        )

    return {
        "nodes": nodes,
        "edges": active_edges,
        "summary": {
            "hop1_count": sum(1 for n in nodes if n["hop_order"] == 1),
            "hop2_count": sum(1 for n in nodes if n["hop_order"] == 2),
            "hop3_count": sum(1 for n in nodes if n["hop_order"] == 3),
            "total_edges": len(active_edges),
        },
        "bigquery_gql": generate_bigquery_graph_sql(),
    }


def _format_graph_edge(
    edge: dict[str, Any],
    companies: dict[str, Any],
    *,
    hop_order: int,
    source_posture: str,
) -> dict[str, Any]:
    doc_ticker = edge["doc_id"].split("-")[0]
    sha256 = companies.get(doc_ticker, {}).get("excerpt_sha256", "")
    return {
        "edge_id": edge["edge_id"],
        "src": edge["src"],
        "src_name": companies[edge["src"]]["name"],
        "dst": edge["dst"],
        "dst_name": companies[edge["dst"]]["name"],
        "relationship": edge["relationship"],
        "revenue_share_pct": edge.get("revenue_share_pct"),
        "share_note": edge.get("share_note", ""),
        "hop_order": hop_order,
        "provenance": {
            "doc_id": edge["doc_id"],
            "sha256": sha256,
            "filing_section": edge["section"],
            "verbatim_quote": edge["verbatim_quote"],
            "source_posture": source_posture,
        },
    }


def generate_bigquery_graph_sql(
    project_id: str = "your-gcp-project",
    dataset_id: str = "fsi_capital_markets",
    graph_name: str = "disclosed_supply_chain_graph",
) -> str:
    """Return BigQuery Property Graph DDL + ISO GQL 1st-to-3rd-order contagion query."""
    fq_ds = f"`{project_id}.{dataset_id}`"
    fq_graph = f"`{project_id}.{dataset_id}.{graph_name}`"
    return f"""-- 1. Zero-ETL BigQuery Property Graph over SEC 10-K XBRL & Footnote Tables
CREATE OR REPLACE PROPERTY GRAPH {fq_graph}
  NODE TABLES (
    {fq_ds}.sp500_companies
      KEY (ticker)
      LABEL Company PROPERTIES (ticker, name, sector, cik, filing_doc_id, excerpt_sha256, diluted_eps)
  )
  EDGE TABLES (
    {fq_ds}.supply_chain_edges
      KEY (edge_id)
      SOURCE KEY (src_ticker) REFERENCES sp500_companies (ticker)
      DESTINATION KEY (dst_ticker) REFERENCES sp500_companies (ticker)
      LABEL DISCLOSED_LINK PROPERTIES (edge_id, relationship, revenue_share_pct, share_note, doc_id, section, verbatim_quote)
  );

-- 2. ISO GQL Multi-Hop Contagion Query (1st -> 2nd -> 3rd Order Portfolio Ripple)
SELECT
  shocked_ticker,
  hop2_ticker,
  hop2_rel,
  hop2_rev_pct,
  hop3_ticker,
  hop3_rel,
  hop3_rev_pct,
  hop3_citation
FROM GRAPH_TABLE (
  {fq_graph}
  MATCH (hop3:Company)-[e2:DISCLOSED_LINK]->(hop2:Company)-[e1:DISCLOSED_LINK]->(shocked:Company)
  WHERE shocked.ticker IN UNNEST(@portfolio_shocked_tickers)
  COLUMNS (
    shocked.ticker AS shocked_ticker,
    hop2.ticker AS hop2_ticker,
    e1.relationship AS hop2_rel,
    e1.revenue_share_pct AS hop2_rev_pct,
    hop3.ticker AS hop3_ticker,
    e2.relationship AS hop3_rel,
    e2.revenue_share_pct AS hop3_rev_pct,
    e2.verbatim_quote AS hop3_citation
  )
)
ORDER BY shocked_ticker, hop2_ticker, hop3_ticker;"""


def scale_disclosed_sensitivity(
    *,
    disclosed_basis: str,
    disclosed_pre_tax_impact_m: str,
    scenario_shock: str,
    effective_tax_rate: str,
    diluted_shares_m: str,
    income_statement_comparable: bool = True,
) -> dict[str, Any]:
    """Scale one disclosed sensitivity to a scenario shock and convert it to diluted EPS.

    ``disclosed_basis`` and ``scenario_shock`` must share a unit (basis points or
    percent). ``disclosed_pre_tax_impact_m`` is the signed annual pre-tax income
    impact in USD millions. A fair-value or balance-sheet-only disclosure is not an
    income-statement impact: pass ``income_statement_comparable=False`` and no EPS
    figure is produced.

    Raises:
        ValueError: if an input is not a decimal number, the basis is zero, the tax
            rate is outside [0, 1), or the share count is not positive.
    """
    try:
        basis = Decimal(str(disclosed_basis))
        disclosed_m = Decimal(str(disclosed_pre_tax_impact_m))
        shock = Decimal(str(scenario_shock))
        tax_rate = Decimal(str(effective_tax_rate))
        shares_m = Decimal(str(diluted_shares_m))
    except InvalidOperation as exc:
        raise ValueError("All numeric inputs must be plain decimal numbers (e.g. '-11', '0.215').") from exc
    if basis == 0:
        raise ValueError("disclosed_basis must be non-zero.")
    if not Decimal("0") <= tax_rate < Decimal("1"):
        raise ValueError("effective_tax_rate must be a fraction in [0, 1), e.g. 0.215 for 21.5%.")
    if shares_m <= 0:
        raise ValueError("diluted_shares_m must be positive.")

    multiplier = shock / basis
    if not income_statement_comparable:
        return {
            "multiplier": q2(multiplier),
            "scaled_pre_tax_m": "0.00",
            "after_tax_m": "0.00",
            "eps_delta": "0.0000",
            "extrapolation_warning": False,
            "non_comparable_warning": True,
            "notes": [
                "The disclosure is a fair-value or balance-sheet sensitivity, not an annual "
                "income-statement impact, so it was not converted to earnings or EPS."
            ],
        }

    scaled_m = disclosed_m * multiplier
    after_tax_m = scaled_m * (Decimal("1") - tax_rate)
    eps_delta = after_tax_m / shares_m
    extrapolated = abs(multiplier) > EXTRAPOLATION_LIMIT and disclosed_m != 0
    notes: list[str] = []
    if tax_rate > USABLE_TAX_RATE_MAX:
        # The caller chose this rate, so it is used as given; the note says why it deserves a second look.
        notes.append(
            f"The tax rate supplied ({percent_text(tax_rate)}) is above {percent_text(USABLE_TAX_RATE_MAX)}. An "
            "effective rate that high usually reflects a loss or near-breakeven year and is not a marginal "
            "rate, so read the after-tax and EPS figures with care."
        )
    if extrapolated:
        notes.append(
            f"The shock is {q2(abs(multiplier))}x the disclosed basis; linear scaling beyond 2x is an extrapolation."
        )
    return {
        "multiplier": q2(multiplier),
        "scaled_pre_tax_m": q2(scaled_m),
        "after_tax_m": q2(after_tax_m),
        "eps_delta": q4(eps_delta),
        "extrapolation_warning": extrapolated,
        "non_comparable_warning": False,
        "notes": notes,
    }


def compute_packet_hash(payload: dict[str, Any]) -> tuple[str, str]:
    """Compute canonical SHA-256 and 6-char confirmation code over deterministic analysis output."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return digest, digest[:6].upper()


def analyze_book(
    book_id: str = "next27_flagship",
    *,
    rate_bps: str | int | Decimal = "25",
    oil_pct: str | int | Decimal = "0",
    scenario_id: str | None = None,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run full deterministic portfolio shock, XBRL EPS conversion, and 3-hop graph analysis."""
    data = dataset or load_sample_book()
    if book_id not in data["books"]:
        raise ValueError(f"Unknown book_id '{book_id}'. Available: {sorted(data['books'])}")
    posture = data["metadata"]["source_posture"]

    scenario_meta: dict[str, Any] | None = None
    if scenario_id:
        for sc in data["scenarios"]:
            if sc["id"] == scenario_id:
                scenario_meta = sc
                rate_bps = sc["rate_bps"]
                oil_pct = sc["oil_pct"]
                break

    rate_dec = Decimal(str(rate_bps))
    oil_dec = Decimal(str(oil_pct))

    book = data["books"][book_id]
    tickers: list[str] = book["tickers"]

    # Compute shocks across all 16 companies so upstream 2nd/3rd-order suppliers outside
    # the immediate portfolio subset also carry exact Decimal metrics.
    all_company_results: dict[str, dict[str, Any]] = {
        ticker: compute_company_shock(comp, rate_dec, oil_dec, source_posture=posture)
        for ticker, comp in data["companies"].items()
    }

    holdings_results = [all_company_results[t] for t in tickers]
    total_pre_tax_m = sum(
        (Decimal(r["scaled_impact"]["total_pre_tax_m"]) for r in holdings_results),
        Decimal("0"),
    )
    total_after_tax_m = sum(
        (Decimal(r["scaled_impact"]["after_tax_m"]) for r in holdings_results),
        Decimal("0"),
    )
    extrapolation_count = sum(1 for r in holdings_results if r["extrapolation_warning"])
    non_comparable_count = sum(1 for r in holdings_results if r["non_comparable_warning"])

    contagion = traverse_supply_chain_contagion(data, all_company_results, tickers)

    core_summary = {
        "book_id": book_id,
        "book_label": book["label"],
        "scenario": {
            "id": scenario_meta["id"] if scenario_meta else "custom_shock",
            "label": (
                scenario_meta["label"]
                if scenario_meta
                else f"Custom Shock: Rates {rate_dec:+} bp · Oil {oil_dec:+}%"
            ),
            "market_odds_pct": scenario_meta.get("market_odds_pct") if scenario_meta else None,
            "rate_bps": str(rate_dec),
            "oil_pct": str(oil_dec),
        },
        "portfolio_totals": {
            "holdings_count": len(tickers),
            "aggregate_pre_tax_impact_m": q2(total_pre_tax_m),
            "aggregate_after_tax_impact_m": q2(total_after_tax_m),
            "extrapolation_warning_count": extrapolation_count,
            "non_comparable_warning_count": non_comparable_count,
        },
        "holdings": holdings_results,
        "contagion_graph": contagion,
    }
    packet_sha256, confirm_code6 = compute_packet_hash(core_summary)
    core_summary["governance"] = {
        "packet_sha256": packet_sha256,
        "confirm_code_6": confirm_code6,
        "approval_phrase": f"APPROVE {confirm_code6}",
        "source_posture": posture,
        "data_notice": data["metadata"].get("data_notice", ""),
        "as_of_utc": data["metadata"]["as_of_utc"],
    }
    return core_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="FSI Capital Markets deterministic engine CLI")
    parser.add_argument("--book", default="next27_flagship", help="Sample book ID")
    parser.add_argument("--scenario", default="fed_up_50_oil_spike", help="Scenario ID")
    args = parser.parse_args()

    result = analyze_book(args.book, scenario_id=args.scenario)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
