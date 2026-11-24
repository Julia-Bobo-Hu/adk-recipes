"""MCP-conformant tool definition over the bundled SEC EDGAR 10-K excerpts and us-gaap XBRL facts.

``lookup_company_xbrl_and_item7a`` reads the bundled sample book and nothing else. This
module makes no network request; reported figures for a company outside the bundle are
looked up by ``scripts/sec_lookup.py``.
"""

from __future__ import annotations

from typing import Any

from scripts import fsi_engine

# MCP-conformant tool definitions with explicit inputSchema, outputSchema, and auditable annotations.
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "lookup_company_xbrl_and_item7a": {
        "name": "lookup_company_xbrl_and_item7a",
        "description": (
            "Returns the us-gaap XBRL metrics held for a covered ticker (revenue_m and long_term_debt_m in "
            "USD millions, effective_tax_rate as decimal fraction, diluted_shares_m in millions, diluted_eps "
            "in USD/share) with the tag and period behind each, the rate and oil market-risk disclosure "
            "records (status, quoted passages, notes) and the filing reference. Use this before scaling any "
            "macro shock; do not use for period-over-period filing changes (use "
            "evaluate_book_filing_deltas instead). Raises KeyError for a ticker outside the bundled book; "
            "no other issuer is looked up at run time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Uppercase stock ticker symbol (e.g., DAL, UAL, BA, JPM, NVDA).",
                }
            },
            "required": ["ticker"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "cik": {"type": "string"},
                "filing_doc_id": {"type": "string"},
                "excerpt_sha256": {"type": "string"},
                "source_posture": {"type": "string"},
                "xbrl": {"type": "object"},
                "xbrl_sources": {"type": "object"},
                "filing": {"type": "object"},
                "sensitivities": {"type": "object"},
            },
            "required": ["ticker", "cik", "filing_doc_id", "excerpt_sha256", "source_posture", "xbrl"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }
}


def lookup_company_xbrl_and_item7a(ticker: str) -> dict[str, Any]:
    """Return the XBRL facts and market-risk disclosure records held in the bundled sample book.

    Raises:
        KeyError: the ticker is not in the book. Disclosed sensitivities of other issuers
            are not looked up at run time; ``scripts/build_sample_book.py`` builds the book.
    """
    norm = ticker.strip().upper()
    dataset = fsi_engine.load_sample_book()
    if norm not in dataset["companies"]:
        raise KeyError(
            f"Ticker '{norm}' is not in the bundled sample book ({sorted(dataset['companies'])}). "
            "Disclosed sensitivities of other issuers are not looked up at run time; add the issuer to "
            "scripts/build_sample_book.py and rebuild the book."
        )
    comp = dataset["companies"][norm]
    return {
        "ticker": comp["ticker"],
        "name": comp["name"],
        "cik": comp["cik"],
        "filing_doc_id": comp["filing_doc_id"],
        "excerpt_sha256": comp["excerpt_sha256"],
        "source_posture": dataset["metadata"]["source_posture"],
        "xbrl": comp["xbrl"],
        "xbrl_sources": comp.get("xbrl_sources", {}),
        "filing": comp.get("filing", {}),
        "sensitivities": comp["sensitivities"],
    }
