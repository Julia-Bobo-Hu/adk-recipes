"""Agent-facing tools for the Capital Markets Analyst skill.

Every tool takes one Pydantic parameter object: on ADK 2.x that is the idiom that
carries per-parameter descriptions into the function declaration. All arithmetic is
delegated to ``scripts.fsi_engine``. Tools do not raise for expected domain errors;
they return ``{"status": "error", "error_code", "message"}`` so the model can tell the
user what went wrong instead of guessing.
"""

from __future__ import annotations

from collections.abc import Callable
import inspect
import re
from typing import Any

from pydantic import BaseModel, Field

from scripts import (
    edgar_xbrl_mcp,
    filing_compare,
    fsi_engine,
    model_exporter,
    reported_financials,
    reported_workbook,
    sec_lookup,
    sheets_reported,
    sheets_sync,
)

WORKBOOK_SHEETS = ["Scenario_Control", "Portfolio_Shock_Model", "Supply_Chain_Graph", "Provenance_Audit"]
MAX_REPORTED_TICKERS = 6


class BookShockParams(BaseModel):
    """Parameters for a rate and oil shock across a covered book."""

    book_id: str = Field(
        default="next27_flagship",
        description="Covered book identifier: next27_flagship, aero_transport, megacap_tech or defensive_consumer.",
    )
    rate_bps: str = Field(
        default="0",
        description="Parallel interest-rate shock in basis points as a decimal string, e.g. '25', '-50', '250'.",
    )
    oil_pct: str = Field(
        default="0",
        description="Oil / jet-fuel price shock in percent as a decimal string, e.g. '0', '25', '-20'.",
    )


class TickerLookupParams(BaseModel):
    """Parameters for a single-issuer disclosure lookup."""

    ticker: str = Field(description="Uppercase stock ticker symbol, e.g. DAL, UAL, BA, JPM, NVDA.")


class FilingDeltaParams(BaseModel):
    """Parameters for the period-over-period filing comparison."""

    book_id: str = Field(
        default="next27_flagship",
        description="Covered book identifier whose issuers should be compared period over period.",
    )


class SensitivityScalingParams(BaseModel):
    """Parameters extracted from one disclosure passage the user supplied."""

    disclosed_basis: str = Field(
        description="Size of the move the issuer disclosed, as a decimal string in the same unit as scenario_shock "
        "(basis points for rates, percent for prices), e.g. '100' or '10'."
    )
    disclosed_pre_tax_impact_m: str = Field(
        description="Signed annual PRE-TAX INCOME impact for that move in USD millions: negative when income falls "
        "(for example higher interest or fuel expense), positive when it rises. Convert billions to millions. "
        "Never put a fair-value or balance-sheet change here."
    )
    scenario_shock: str = Field(
        description="The scenario move to scale to, as a decimal string in the same unit as disclosed_basis, e.g. '25'."
    )
    effective_tax_rate: str = Field(description="Effective tax rate as a fraction, e.g. '0.215' for 21.5%.")
    diluted_shares_m: str = Field(description="Weighted-average diluted shares in millions, e.g. '645.0'.")
    income_statement_comparable: bool = Field(
        default=True,
        description="Set false when the passage only discloses a fair-value or balance-sheet sensitivity and no "
        "annual income-statement impact. No EPS figure is produced in that case.",
    )


class WorkbookExportParams(BaseModel):
    """Parameters for exporting the formula-linked workbook."""

    book_id: str = Field(
        default="next27_flagship",
        description="Covered book identifier: next27_flagship, aero_transport, megacap_tech or defensive_consumer.",
    )
    rate_bps: str = Field(default="0", description="Interest-rate shock in basis points as a decimal string, e.g. '25'.")
    oil_pct: str = Field(default="0", description="Oil / jet-fuel price shock in percent as a decimal string, e.g. '0'.")


class ReportedFinancialsParams(BaseModel):
    """Parameters for the reported annual figures of one or more companies."""

    tickers: list[str] = Field(
        description="One to six stock tickers, e.g. ['AAPL'] for one company or ['DAL', 'UAL'] to compare peers."
    )
    fiscal_years: int = Field(
        default=reported_financials.MAX_VIEW_YEARS,
        description="How many of each company's latest fiscal years to return: 1, 2 or 3.",
    )


class ReportedWorkbookParams(BaseModel):
    """Parameters for writing the reported figures of one or more companies to a workbook or to the Google Sheet."""

    tickers: list[str] = Field(
        description="One to six stock tickers of the companies whose reported figures to write, e.g. ['DAL', 'UAL']."
    )


class SheetReadParams(BaseModel):
    """Parameters for reading the scenario the user set in the configured Google Sheet."""

    book_id: str = Field(
        default="",
        description="Leave empty to use the book recorded in the Sheet. Give a covered book identifier only to "
        "override it: next27_flagship, aero_transport, megacap_tech or defensive_consumer.",
    )


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "message": message, **extra}


def run_portfolio_shock_analysis(params: BookShockParams) -> dict[str, Any]:
    """Scale every holding's disclosed rate and oil sensitivity to a scenario and convert it to diluted EPS.

    Returns, per holding, the scaled pre-tax and after-tax impact in USD millions, the
    diluted EPS change in USD per share, extrapolation (more than 2x the disclosed basis)
    and non-comparable-disclosure warnings, coverage and basis notes (a derived figure, or
    a factor for which the filing gives no convertible amount, in which case its disclosed
    basis and amount are null and its impact is 0), the quoted source passage behind every
    figure, and the 1st-to-3rd-order disclosed
    customer/supplier links. Use it for any rate or oil shock question about a covered
    book. Do not use it for a passage the user pastes (use scale_disclosed_sensitivity) or
    for period-over-period filing changes (use evaluate_book_filing_deltas). Returns
    status="error" for an unknown book or a non-numeric shock.
    """
    try:
        result = fsi_engine.analyze_book(params.book_id, rate_bps=params.rate_bps, oil_pct=params.oil_pct)
    except ValueError as exc:
        return _error("unknown_book", str(exc))
    except ArithmeticError:
        return _error("invalid_input", "rate_bps and oil_pct must be plain decimal numbers, e.g. '25' or '-20'.")
    return {"status": "ok", **result}


def lookup_statutory_filing_facts(params: TickerLookupParams) -> dict[str, Any]:
    """Return the XBRL metrics and market-risk disclosure passages held for one ticker.

    Returns revenue and long-term debt (USD millions), effective tax rate (fraction),
    diluted shares (millions), diluted EPS (USD per share) with the XBRL tag and period
    behind each, the rate and oil disclosure records (status, notes and quoted passages,
    the first passage being the one a figure comes from),
    the filing's accession number and URL when the data is filing-sourced, and the source
    posture of the data. Use it to inspect one issuer before answering. Do not use it to
    compute a scenario (use run_portfolio_shock_analysis). Returns status="error" with
    error_code="not_in_coverage" when the ticker is not covered; in that case say so and
    never supply figures from memory.
    """
    try:
        fact = edgar_xbrl_mcp.lookup_company_xbrl_and_item7a(params.ticker)
    except KeyError:
        covered = sorted(fsi_engine.load_sample_book()["companies"])
        return _error(
            "not_in_coverage",
            f"Ticker '{params.ticker.strip().upper()}' is not in the covered universe.",
            covered_tickers=covered,
        )
    return {"status": "ok", **fact}


def evaluate_book_filing_deltas(params: FilingDeltaParams) -> dict[str, Any]:
    """Report one period-over-period change per issuer from its latest filing (the-watcher).

    Returns, per issuer, a metric with its prior and current value and the change, and
    where the two values come from: basis="filing_passage" (one quoted passage of the
    current filing states both periods; the passage is returned) or basis="xbrl"
    (long-term debt in USD millions at two balance-sheet dates). Credit rubric scores (1-5
    for leverage, liquidity, margin resilience and covenant risk, plus a composite and a
    descriptive stance) are returned only when the dataset carries them; otherwise
    credit_scores is null and rubric_status is "not_scored", and scores must not be
    supplied from elsewhere. Also returns the rendered the-watcher markdown. A stance is a
    description of disclosed trends, not a rating or a recommendation. Use it for "what
    changed since the prior period" questions. Do not use it for scenario shocks (use
    run_portfolio_shock_analysis). Returns status="error" for an unknown book.
    """
    try:
        result = filing_compare.evaluate_book_deltas(params.book_id)
    except ValueError as exc:
        return _error("unknown_book", str(exc))
    return {"status": "ok", **result}


def scale_disclosed_sensitivity(params: SensitivityScalingParams) -> dict[str, Any]:
    """Scale one disclosed sensitivity from a passage the user supplied and convert it to diluted EPS.

    Extract the disclosed basis and the signed annual pre-tax income impact from the
    passage, then call this tool; never do the arithmetic yourself. Returns the scaling
    multiple, the scaled pre-tax and after-tax impact in USD millions, the diluted EPS
    change in USD per share, an extrapolation warning when the shock is more than 2x the
    disclosed basis, and a non-comparable warning when income_statement_comparable is
    false. Do not use it for covered books (use run_portfolio_shock_analysis). Returns
    status="error" with error_code="invalid_input" for non-numeric or out-of-range inputs.
    """
    try:
        result = fsi_engine.scale_disclosed_sensitivity(
            disclosed_basis=params.disclosed_basis,
            disclosed_pre_tax_impact_m=params.disclosed_pre_tax_impact_m,
            scenario_shock=params.scenario_shock,
            effective_tax_rate=params.effective_tax_rate,
            diluted_shares_m=params.diluted_shares_m,
            income_statement_comparable=params.income_statement_comparable,
        )
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    return {"status": "ok", **result}


def export_formula_workbook(params: WorkbookExportParams) -> dict[str, Any]:
    """Write the formula-linked .xlsx model for a covered book and scenario to the local _output folder.

    Returns the file path, its size in bytes, the number of holdings, the number of live
    formula cells and whether they reference the Scenario_Control sheet. Use it when the
    user asks for the spreadsheet model. It writes a local .xlsx file only and overwrites
    the previous export for the same book and scenario; it does not create a Google Sheet
    and does not upload anything. Returns status="error" for an unknown book or a
    non-numeric shock.
    """
    try:
        xlsx_bytes, grid = model_exporter.export_workbook_bytes(
            params.book_id, rate_bps=params.rate_bps, oil_pct=params.oil_pct
        )
    except ValueError as exc:
        return _error("unknown_book", str(exc))
    except ArithmeticError:
        return _error("invalid_input", "rate_bps and oil_pct must be plain decimal numbers, e.g. '25' or '-20'.")

    out_dir = fsi_engine.SKILL_ROOT / "_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    scenario_tag = re.sub(r"[^0-9A-Za-z.+-]", "_", f"rate{params.rate_bps}_oil{params.oil_pct}")
    path = out_dir / f"fsi_{params.book_id}_{scenario_tag}.xlsx"
    path.write_bytes(xlsx_bytes)
    stats = model_exporter.inspect_xlsx_formulas(xlsx_bytes)
    return {
        "status": "ok",
        "path": str(path),
        "bytes": len(xlsx_bytes),
        "holdings": len(grid["rows"]),
        "formula_cell_count": stats["formula_cell_count"],
        "has_scenario_sheet_refs": stats["has_scenario_sheet_refs"],
        "sheets": list(WORKBOOK_SHEETS),
    }


def _reported_views(requested: list[str], years: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Look each requested company up once. Returns the company views and the companies that could not be returned."""
    views: list[dict[str, Any]] = []
    not_available: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in requested:
        try:
            symbol = sec_lookup.normalise_ticker(raw)
            if symbol in seen:
                continue
            seen.add(symbol)
            record, origin = sec_lookup.lookup(symbol)
        except sec_lookup.LookupUnavailable as exc:
            not_available.append({"ticker": raw.upper()[:12], "reason": exc.reason, "message": exc.message})
            continue
        views.append(reported_financials.company_view(record, years=years, origin=origin))
    return views, not_available


def get_reported_financials(params: ReportedFinancialsParams) -> dict[str, Any]:
    """Return reported annual figures for one or more companies from SEC XBRL data, with growth and margins computed in Python.

    Returns, for each ticker and each of its latest fiscal years: revenue, cost of revenue,
    gross profit, R&D, operating income, interest expense, pre-tax income, income tax, net
    income, operating cash flow, capital expenditure, dividends, share repurchases, cash,
    total assets, total liabilities, shareholders' equity and long-term debt in USD
    millions, diluted EPS in USD per share, diluted shares in millions and the effective
    tax rate, each with the accession number of the Form 10-K it was taken from and, under
    tags, its XBRL tag; computed year-over-year changes, growth rates, margins and free
    cash flow; the line items the company does not tag (not_tagged) and the metrics that
    could not be computed, with the reason (not_computed); and, for two or more tickers, a
    comparison of each company's latest fiscal year. Use it for any question about
    reported results, growth, margins, cash flow or debt, for one company or a peer group.
    Do not use it for market-risk sensitivities (use lookup_statutory_filing_facts or
    run_portfolio_shock_analysis). It holds no share prices, valuation multiples,
    consensus estimates, guidance or quarterly figures. A company outside the bundled set
    is read from SEC EDGAR only when live lookups are switched on; otherwise it is listed
    under not_available with its reason and no figures. Returns status="error" with
    error_code="invalid_input" for an empty or oversized ticker list or a fiscal_years
    value outside 1 to 3, and error_code="not_available" when none of the requested
    companies could be returned.
    """
    requested = [str(ticker).strip() for ticker in params.tickers if str(ticker).strip()]
    if not requested or len(requested) > MAX_REPORTED_TICKERS:
        return _error("invalid_input", f"Give between 1 and {MAX_REPORTED_TICKERS} tickers.")
    if not 1 <= params.fiscal_years <= reported_financials.MAX_VIEW_YEARS:
        return _error("invalid_input", f"fiscal_years must be between 1 and {reported_financials.MAX_VIEW_YEARS}.")

    views, not_available = _reported_views(requested, params.fiscal_years)
    if not views:
        return _error(
            "not_available", "None of the requested companies could be returned.", not_available=not_available
        )
    result: dict[str, Any] = {
        "status": "ok",
        "basis": reported_financials.BASIS_NOTE,
        **reported_financials.legends(),
        "companies": views,
        "not_available": not_available,
    }
    if len(views) > 1:
        result["comparison"] = {
            "note": "Each row is that company's latest fiscal year; fiscal years can end on different dates.",
            "rows": reported_financials.comparison_rows(views),
        }
    return result


def write_scenario_to_sheet(params: BookShockParams) -> dict[str, Any]:
    """Write the live-formula rate and oil model for a covered book into the configured Google Sheet.

    Replaces the Sheet's two tabs Scenario_Control and Portfolio_Shock_Model with the filing
    inputs and live formulas for the given scenario, then reads the computed cells back and
    compares them with the Python engine. Returns the Sheet's link and title, the book and
    scenario written, the two cells the user may change (rate in basis points, oil in
    percent), the number of holdings, cells and formula cells written, and a reconciliation:
    rows checked, rows matching, and every input cell or computed cell that differs. It
    returns no impact figures; call run_portfolio_shock_analysis for those. Use it when the
    user asks to put, build or refresh the scenario model in the Google Sheet. Do not use it
    for a downloadable file (use export_formula_workbook). It writes only to the one Sheet
    the deployment is configured with, it cannot create or share a spreadsheet, and anything
    typed into those two tabs is replaced. Returns status="error" with
    error_code="sheet_not_configured" when no Sheet is configured,
    "sheet_permission_denied" or "sheet_not_found" when the Sheet is not shared with the
    skill's identity, "sheets_unreachable" or "sheets_api_error" for other failures,
    "unknown_book" for an unknown book and "invalid_input" for a non-numeric shock.
    """
    try:
        result = sheets_sync.write_scenario(
            sheets_sync.client_from_env(),
            params.book_id,
            params.rate_bps,
            params.oil_pct,
            pace_seconds=sheets_sync.row_pace_seconds(),
        )
    except sheets_sync.SheetsError as exc:
        return _error(exc.code, str(exc))
    except ValueError as exc:
        return _error("unknown_book", str(exc))
    except ArithmeticError:
        return _error("invalid_input", "rate_bps and oil_pct must be plain decimal numbers, e.g. '25' or '-20'.")
    return {"status": "ok", **result}


def read_scenario_from_sheet(params: SheetReadParams) -> dict[str, Any]:
    """Read the scenario the user set in the configured Google Sheet and check the Sheet against the filing data.

    Reads the rate (basis points) and oil (percent) cells of the Scenario_Control tab,
    recomputes the book in Python for those two values and compares the Sheet with that
    result. Returns the Sheet's link and title, the book, the two scenario values as decimal
    strings, and a reconciliation: rows checked, rows matching, edited_inputs (input cells
    whose value no longer equals the filing data) and mismatches (computed cells that differ
    from the engine by more than rounding). It returns no impact figures: after it, call
    run_portfolio_shock_analysis with the returned book_id, rate_bps and oil_pct and answer
    from that. Use it when the user says they changed the Sheet, or asks what the Sheet
    shows now. Do not use it to write (use write_scenario_to_sheet). Returns status="error"
    with error_code="sheet_not_configured" when no Sheet is configured, "sheet_has_no_model"
    when the model was never written to the Sheet, "invalid_sheet_scenario" when a scenario
    cell is not a number, "sheet_permission_denied", "sheet_not_found", "sheets_unreachable"
    or "sheets_api_error" when the Sheet cannot be read, and "unknown_book" for an unknown
    book.
    """
    try:
        result = sheets_sync.read_scenario(sheets_sync.client_from_env(), params.book_id or None)
    except sheets_sync.SheetsError as exc:
        return _error(exc.code, str(exc))
    except ValueError as exc:
        return _error("unknown_book", str(exc))
    return {"status": "ok", **result}


class ReportedRequestError(ValueError):
    """The requested companies cannot be written. ``code`` is the error code a tool returns for it."""

    def __init__(self, code: str, message: str, not_available: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.not_available = list(not_available or [])


def workbook_views(tickers: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Views holding every fiscal year on record for one to six companies, and the companies that could not be returned.

    Raises:
        ReportedRequestError: No ticker or more than six (``invalid_input``), or none of the
            requested companies could be returned (``not_available``).
    """
    requested = [str(ticker).strip() for ticker in tickers if str(ticker).strip()]
    if not requested or len(requested) > MAX_REPORTED_TICKERS:
        raise ReportedRequestError("invalid_input", f"Give between 1 and {MAX_REPORTED_TICKERS} tickers.")
    views, not_available = _reported_views(requested, reported_financials.KEEP_FISCAL_YEARS)
    if not views:
        raise ReportedRequestError("not_available", "None of the requested companies could be returned.", not_available)
    return views, not_available


def _reported_request_error(exc: ReportedRequestError) -> dict[str, Any]:
    extra = {"not_available": exc.not_available} if exc.not_available else {}
    return _error(exc.code, str(exc), **extra)


def export_reported_workbook(params: ReportedWorkbookParams) -> dict[str, Any]:
    """Write the reported annual figures of one to six companies to a formula-linked .xlsx workbook in the local _output folder.

    The workbook has four sheets: Reported_Figures (every reported line item for each fiscal
    year on record, up to four per company), Metrics (growth, margins and free cash flow as
    live formulas whose saved values are the figures computed in Python; a metric that could
    not be computed shows its reason), Comparison (each company's latest fiscal year) and
    Sources (the XBRL tag, accession number, form and filing date of every value). Returns
    the file path, its size in bytes, the tickers written, the number of company-year rows,
    the number of live formula cells, the sheet names and any company that could not be
    returned (not_available, with the reason). It returns no figures; call
    get_reported_financials for the numbers. Use it when the user asks for reported figures
    or a peer comparison as a spreadsheet, a workbook or an Excel file. Do not use it for the
    rate and oil scenario model (use export_formula_workbook) or for the Google Sheet (use
    write_reported_figures_to_sheet). It writes a local .xlsx file only and overwrites the
    previous export for the same tickers. Returns status="error" with
    error_code="invalid_input" for an empty or oversized ticker list and
    error_code="not_available" when none of the requested companies could be returned.
    """
    try:
        views, not_available = workbook_views(params.tickers)
    except ReportedRequestError as exc:
        return _reported_request_error(exc)
    xlsx_bytes, grid = reported_workbook.export_workbook_bytes(views)
    out_dir = fsi_engine.SKILL_ROOT / "_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^0-9A-Za-z]", "_", "_".join(view["ticker"] for view in views))
    path = out_dir / f"fsi_reported_{tag}.xlsx"
    path.write_bytes(xlsx_bytes)
    return {
        "status": "ok",
        "path": str(path),
        "bytes": len(xlsx_bytes),
        "companies": [view["ticker"] for view in views],
        "company_year_rows": sum(len(company["rows"]) for company in grid["companies"]),
        "formula_cell_count": sum(reported_workbook.inspect_workbook(xlsx_bytes).values()),
        "sheets": list(reported_workbook.SHEETS),
        "not_available": not_available,
    }


def write_reported_figures_to_sheet(params: ReportedWorkbookParams) -> dict[str, Any]:
    """Write the reported annual figures of one to six companies into the configured Google Sheet.

    Replaces the Sheet's four tabs Reported_Figures, Metrics, Comparison and Sources with
    the reported line items for each fiscal year on record, growth and margins as live
    formulas, each company's latest fiscal year side by side, and the XBRL tag and filing of
    every value; then reads the cells the Sheet computed and compares them with the figures
    computed in Python. Returns the Sheet's link and title, the link of the Comparison tab,
    the tickers written, the number of company-year rows, cells and formula cells written,
    a reconciliation (cells checked, cells matching and every cell that differs) and any
    company that could not be returned (not_available, with the reason). It returns no
    figures; call get_reported_financials for the numbers. Use it when the user asks to put
    reported figures or a peer comparison into the Google Sheet. Do not use it for the rate
    and oil scenario model (use write_scenario_to_sheet) or for a downloadable file (use
    export_reported_workbook). It writes only to the one Sheet the deployment is configured
    with, it cannot create or share a spreadsheet, and anything typed into those four tabs is
    replaced. Returns status="error" with error_code="sheet_not_configured" when no Sheet is
    configured, "invalid_input" for an empty or oversized ticker list, "not_available" when
    none of the requested companies could be returned, "sheet_permission_denied" or
    "sheet_not_found" when the Sheet is not shared with the skill's identity, and
    "sheets_unreachable" or "sheets_api_error" for other failures.
    """
    try:
        # Asked for first: without a Sheet there is no reason to look any company up.
        client = sheets_sync.client_from_env()
    except sheets_sync.SheetsError as exc:
        return _error(exc.code, str(exc))
    try:
        views, not_available = workbook_views(params.tickers)
    except ReportedRequestError as exc:
        return _reported_request_error(exc)
    try:
        result = sheets_reported.write_reported(client, views, pace_seconds=sheets_sync.row_pace_seconds())
    except sheets_sync.SheetsError as exc:
        return _error(exc.code, str(exc))
    return {"status": "ok", **result, "not_available": not_available}


# name -> (function, parameter model, keys always present when status == "ok", MCP annotations)
_TOOL_SPECS: dict[str, tuple[Callable[..., dict[str, Any]], type[BaseModel], list[str], dict[str, bool]]] = {
    "run_portfolio_shock_analysis": (
        run_portfolio_shock_analysis,
        BookShockParams,
        ["scenario", "portfolio_totals", "holdings", "contagion_graph", "governance"],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    "lookup_statutory_filing_facts": (
        lookup_statutory_filing_facts,
        TickerLookupParams,
        ["ticker", "cik", "filing_doc_id", "excerpt_sha256", "source_posture", "xbrl", "sensitivities"],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    "evaluate_book_filing_deltas": (
        evaluate_book_filing_deltas,
        FilingDeltaParams,
        ["book_id", "book_label", "rows", "watcher_markdown"],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    "scale_disclosed_sensitivity": (
        scale_disclosed_sensitivity,
        SensitivityScalingParams,
        [
            "multiplier",
            "scaled_pre_tax_m",
            "after_tax_m",
            "eps_delta",
            "extrapolation_warning",
            "non_comparable_warning",
            "notes",
        ],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    "export_formula_workbook": (
        export_formula_workbook,
        WorkbookExportParams,
        ["path", "bytes", "holdings", "formula_cell_count", "has_scenario_sheet_refs", "sheets"],
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    # Reads SEC EDGAR when live lookups are on (open world) and then stores the reduced record locally.
    "get_reported_financials": (
        get_reported_financials,
        ReportedFinancialsParams,
        ["basis", "line_items", "computed_metrics", "companies", "not_available"],
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    # Replaces the content of two tabs of the one configured Sheet, hence destructive.
    "write_scenario_to_sheet": (
        write_scenario_to_sheet,
        BookShockParams,
        [
            "sheet_url",
            "book_id",
            "scenario",
            "scenario_cells",
            "tabs_written",
            "cells_written",
            "holdings",
            "formula_cells",
            "reconciliation",
        ],
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    ),
    "read_scenario_from_sheet": (
        read_scenario_from_sheet,
        SheetReadParams,
        ["sheet_url", "book_id", "scenario", "reconciliation"],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    # Writes a local file; reads SEC EDGAR when live lookups are on.
    "export_reported_workbook": (
        export_reported_workbook,
        ReportedWorkbookParams,
        ["path", "bytes", "companies", "company_year_rows", "formula_cell_count", "sheets", "not_available"],
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    # Replaces the content of four tabs of the one configured Sheet, hence destructive.
    "write_reported_figures_to_sheet": (
        write_reported_figures_to_sheet,
        ReportedWorkbookParams,
        [
            "sheet_url",
            "tabs_written",
            "companies",
            "company_year_rows",
            "cells_written",
            "formula_cells",
            "reconciliation",
            "not_available",
        ],
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    ),
}

AGENT_TOOLS: list[Callable[..., dict[str, Any]]] = [spec[0] for spec in _TOOL_SPECS.values()]
PARAM_MODELS: dict[str, type[BaseModel]] = {name: spec[1] for name, spec in _TOOL_SPECS.items()}

_ERROR_SHAPE = {
    "type": "object",
    "properties": {
        "status": {"const": "error"},
        "error_code": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["status", "error_code", "message"],
}


def tool_definitions() -> dict[str, dict[str, Any]]:
    """Return MCP-shaped definitions (name, description, inputSchema, outputSchema, annotations)."""
    definitions: dict[str, dict[str, Any]] = {}
    for name, (func, params_model, ok_keys, annotations) in _TOOL_SPECS.items():
        definitions[name] = {
            "name": name,
            "description": inspect.getdoc(func) or "",
            "inputSchema": params_model.model_json_schema(),
            "outputSchema": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"status": {"const": "ok"}},
                        "required": ["status", *ok_keys],
                    },
                    _ERROR_SHAPE,
                ]
            },
            "annotations": dict(annotations),
        }
    return definitions


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate ``args`` against the tool's parameter model and call it (used by the eval replay)."""
    if name not in _TOOL_SPECS:
        raise KeyError(f"Unknown tool {name!r}. Known tools: {sorted(_TOOL_SPECS)}")
    func, params_model, _, _ = _TOOL_SPECS[name]
    return func(params_model(**args))
