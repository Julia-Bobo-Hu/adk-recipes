"""Data and model-facing summary behind the ripple-graph chat view.

The view shows a covered book under a rate and oil scenario: each holding's scaled
impact, the disclosed customer and supplier links, and the filing passage behind every
figure and link. Two callers use this module: ``scripts/mcp_server.py`` (the MCP App
tool) and ``scripts/server.py`` (the workstation's preview of that view).

Nothing is computed here. The numbers come from ``tools.run_portfolio_shock_analysis``,
the same deterministic tool the agent calls, so the view cannot disagree with it. This
module only selects fields, lists the covered books and the bundled scenario presets,
and writes the plain-text summary a host's model reads in place of the view.

It imports neither the MCP SDK nor ADK, so the workstation can use it without them.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

from scripts import fsi_engine, tools

# Tool the model calls, and the tool only the view may call (its book and scenario controls).
SHOW_TOOL = "show_ripple_graph"
RECOMPUTE_TOOL = "recompute_ripple_graph"
# Tool only the view may call for its brief: three fiscal years of reported figures of the selection.
BRIEF_TOOL = "brief_selection"
MAX_BRIEF_TICKERS = 4

APP_RESOURCE_URI = "ui://fsi-capital-markets-analyst/ripple-graph.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"
APP_HTML_PATH = fsi_engine.SKILL_ROOT / "assets" / "mcp-app" / "ripple.html"

# Limits documented for MCP Apps hosts: the view document and one stored tool result.
MAX_APP_HTML_BYTES = 10 * 1024 * 1024
MAX_TOOL_RESULT_BYTES = 1024 * 1024

DEFAULT_BOOK_ID = "next27_flagship"
DEFAULT_RATE_BPS = "50"
DEFAULT_OIL_PCT = "25"

_GOVERNANCE_KEYS = ("packet_sha256", "source_posture", "data_notice", "as_of_utc")


def _decimal_text(value: Any) -> Any:
    """Accept a JSON number where a decimal string is expected.

    A model sometimes sends ``50`` for a parameter declared as the string ``"50"``. The
    number is turned back into its shortest decimal text, which ``Decimal`` then reads
    exactly; it never takes part in arithmetic as a binary float.
    """
    if isinstance(value, bool):
        raise ValueError("must be a decimal number such as '25' or '-20', not a boolean")
    if isinstance(value, (int, float)):
        return str(value)
    return value


DecimalText = Annotated[str, BeforeValidator(_decimal_text)]


class RippleViewError(ValueError):
    """The view cannot be built for the requested book or scenario."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RippleView(BaseModel):
    """Structured result of the ripple-graph tool: the fields the view renders."""

    book_id: str = Field(description="Identifier of the covered book that was analysed.")
    book_label: str = Field(description="Display name of that book.")
    books: list[dict[str, Any]] = Field(
        description="Every covered book (id, label, tickers), so the view can offer a book selector."
    )
    scenario_presets: list[dict[str, Any]] = Field(
        description="Named scenarios bundled with the dataset (id, label, rate_bps, oil_pct), offered as "
        "one-click choices in the view. They are annotations, not market forecasts."
    )
    scenario: dict[str, Any] = Field(
        description="The scenario applied: rate_bps in basis points and oil_pct in percent, as decimal strings."
    )
    portfolio_totals: dict[str, Any] = Field(
        description="Sum over the holdings whose filing states a convertible figure: pre-tax and after-tax impact "
        "in USD millions (decimal strings). Also the number of holdings in the book and how many carry an "
        "extrapolation or non-comparable warning. A holding without a convertible figure adds nothing to the sums."
    )
    holdings: list[dict[str, Any]] = Field(
        description="One record per holding: disclosed basis, scaled pre-tax and after-tax impact in USD "
        "millions, diluted EPS change in USD per share, warnings, notes, and for each factor the citation "
        "(doc_id, sha256, filing_section, verbatim_quote, source_posture)."
    )
    contagion_graph: dict[str, Any] = Field(
        description="Disclosed customer and supplier links reached from the shocked holdings: nodes with "
        "their hop order, edges with relationship, revenue share in percent when the filing states one, and "
        "the citation behind each edge."
    )
    governance: dict[str, Any] = Field(
        description="SHA-256 of this analysis packet, the source posture of the data, its data notice and "
        "its as-of time (UTC)."
    )


def list_books(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Covered books in dataset order."""
    return [
        {"id": book_id, "label": book["label"], "tickers": list(book["tickers"])}
        for book_id, book in dataset["books"].items()
    ]


def list_scenario_presets(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Named scenarios bundled with the dataset, with their shocks as decimal strings."""
    return [
        {"id": preset["id"], "label": preset["label"], "rate_bps": str(preset["rate_bps"]), "oil_pct": str(preset["oil_pct"])}
        for preset in dataset["scenarios"]
    ]


def _normalized(text: str) -> str:
    """Lower-case letters and digits only, so 'Mega-Cap Tech & AI' and 'mega cap tech ai' compare equal."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def describe_books(dataset: dict[str, Any] | None = None) -> str:
    """The covered books as ``id (display name)``, for a parameter description or an error message."""
    data = dataset or fsi_engine.load_sample_book()
    return "; ".join(f"{book['id']} ({book['label']})" for book in list_books(data))


def resolve_book_id(book_id: str = "", book_name: str = "") -> str:
    """Return the id of the covered book named by ``book_id``, by ``book_name``, or by both.

    Either value may hold the id or the display name, whole or a distinctive part of it
    ("Mega-Cap Tech" for "Mega-Cap Tech & AI Supply Chain"). With neither, the default book
    is returned.

    Raises:
        RippleViewError: ``unknown_book`` when a value names no covered book or more than one,
            or when the two values name different books.
    """
    dataset = fsi_engine.load_sample_book()
    names = {book["id"]: (_normalized(book["id"]), _normalized(book["label"])) for book in list_books(dataset)}

    def find(text: str) -> str | None:
        wanted = _normalized(str(text))
        if not wanted:
            return None
        exact = [found for found, forms in names.items() if wanted in forms]
        if len(exact) == 1:
            return exact[0]
        partial = [found for found, (_, label) in names.items() if wanted in label or label in wanted]
        if len(partial) == 1:
            return partial[0]
        problem = "matches more than one covered book" if partial else "is not a covered book"
        raise RippleViewError("unknown_book", f"'{text}' {problem}. Covered books: {describe_books(dataset)}.")

    by_id, by_name = find(book_id), find(book_name)
    if by_id and by_name and by_id != by_name:
        raise RippleViewError(
            "unknown_book",
            f"book_id '{book_id}' and book_name '{book_name}' name different books. Give one of them. "
            f"Covered books: {describe_books(dataset)}.",
        )
    return by_id or by_name or DEFAULT_BOOK_ID


def build_ripple_view(
    book_id: str = DEFAULT_BOOK_ID,
    rate_bps: str = DEFAULT_RATE_BPS,
    oil_pct: str = DEFAULT_OIL_PCT,
) -> dict[str, Any]:
    """Return the structured view for a book and scenario.

    Raises:
        RippleViewError: for a book that is not covered (``unknown_book``) or a shock that
            is not a plain decimal number (``invalid_input``).
    """
    try:
        shocks = [str(_decimal_text(value)).strip() for value in (rate_bps, oil_pct)]
    except ValueError as exc:
        raise RippleViewError("invalid_input", f"rate_bps and oil_pct {exc}.") from exc
    params = tools.BookShockParams(book_id=str(book_id).strip(), rate_bps=shocks[0], oil_pct=shocks[1])
    result = tools.run_portfolio_shock_analysis(params)
    if result.get("status") != "ok":
        raise RippleViewError(str(result.get("error_code", "error")), str(result.get("message", "Analysis failed.")))

    governance = result["governance"]
    dataset = fsi_engine.load_sample_book()
    return {
        "book_id": result["book_id"],
        "book_label": result["book_label"],
        "books": list_books(dataset),
        "scenario_presets": list_scenario_presets(dataset),
        "scenario": result["scenario"],
        "portfolio_totals": result["portfolio_totals"],
        "holdings": result["holdings"],
        "contagion_graph": result["contagion_graph"],
        # The typed sign-off confirmation stays in the workstation: the view is read-only.
        "governance": {key: governance[key] for key in _GOVERNANCE_KEYS},
    }


def format_decimal(value: str, places: int = 2) -> str:
    """Format a decimal string with thousands separators, without leaving ``Decimal``."""
    return f"{Decimal(value):,.{places}f}"


def format_signed(value: str) -> str:
    """Format a shock with an explicit sign: ``'50'`` -> ``'+50'``, ``'-20'`` -> ``'-20'``, ``'0'`` -> ``'0'``."""
    number = Decimal(value)
    return f"{number:+f}" if number != 0 else "0"


def has_convertible_figure(holding: dict[str, Any]) -> bool:
    """True when the filing states an annual income-statement sensitivity for at least one factor."""
    basis = holding["disclosed_basis"]
    return fsi_engine.STATUS_INCOME_STATEMENT in (basis["rate_status"], basis["oil_status"])


def summarize_for_model(view: dict[str, Any]) -> str:
    """Plain-text summary of the view for the host's model and for hosts that render no view.

    It states the figures and the links, and deliberately carries no filing passage: the
    passages are shown in the view, and a model that has not been given one must not quote one.
    """
    scenario = view["scenario"]
    totals = view["portfolio_totals"]
    governance = view["governance"]
    graph = view["contagion_graph"]
    holdings = view["holdings"]
    converted = [holding for holding in holdings if has_convertible_figure(holding)]
    unconverted = [holding for holding in holdings if not has_convertible_figure(holding)]
    # The total adds only the holdings with a figure, so the sentence that states it says how many those are.
    sum_scope = (
        f"Sum over the {len(converted)} of {totals['holdings_count']} holdings whose filing states a convertible figure"
        if unconverted
        else f"Sum over all {totals['holdings_count']} holdings"
    )

    lines = [
        f"Ripple-graph view: {view['book_label']} (book_id {view['book_id']}), "
        f"rates {format_signed(scenario['rate_bps'])} bp, oil {format_signed(scenario['oil_pct'])}%.",
        f"Source posture: {governance['source_posture']}. Data as of {governance['as_of_utc']}.",
        f"{sum_scope}: pre-tax {format_decimal(totals['aggregate_pre_tax_impact_m'])} "
        f"USD million, after-tax {format_decimal(totals['aggregate_after_tax_impact_m'])} USD million.",
        "",
        f"Holdings whose filing states an annual income-statement sensitivity ({len(converted)}):",
    ]
    for holding in converted:
        impact = holding["scaled_impact"]
        flag = " Extrapolated: the shock is more than 2x the disclosed basis." if holding["extrapolation_warning"] else ""
        lines.append(
            f"- {holding['ticker']} ({holding['name']}): pre-tax {format_decimal(impact['total_pre_tax_m'])} USD million, "
            f"after-tax {format_decimal(impact['after_tax_m'])} USD million, diluted EPS {impact['eps_delta']} USD per share. "
            f"Source: {holding['provenance'][0]['doc_id']}.{flag}"
        )
        for note in holding.get("conversion_notes", []):
            lines.append(f"  Note on {holding['ticker']}: {note}")
    lines.append("")
    lines.append(
        f"Holdings with no convertible figure ({len(unconverted)}). Their impact is shown as 0 because the "
        "filing gives nothing to convert, not because the issuer has no exposure:"
    )
    for holding in unconverted:
        basis = holding["disclosed_basis"]
        lines.append(f"- {holding['ticker']} ({holding['name']}): rate {basis['rate_status']}, oil {basis['oil_status']}.")
    lines.append("")
    lines.append(f"Disclosed customer and supplier links ({len(graph['edges'])}):")
    for edge in graph["edges"]:
        share = (
            f"{edge['revenue_share_pct']}% revenue share disclosed"
            if edge["revenue_share_pct"] is not None
            else "no revenue share disclosed"
        )
        lines.append(
            f"- {edge['src']} -> {edge['dst']}: {edge['relationship']}, {share}. Source: {edge['provenance']['doc_id']}."
        )
    lines.extend(
        [
            "",
            "The interactive view shows the filing passage, document id and SHA-256 behind every figure and link. "
            "This text carries no filing passage: do not quote filing text from memory; point the user to the view.",
            "Every figure is a linear scaling of the issuer's own disclosed sensitivity, computed by the tool in "
            "decimal arithmetic. Do not recompute or extend the figures, and do not present them as a forecast. "
            "Not investment advice.",
            "If you restate a figure, copy it exactly as written here, with its sign: a negative impact is a loss "
            "and a positive one a gain. Add no figure, cause or characterisation of your own. Write a link as "
            "'A -> B' in plain text, without LaTeX or other markup.",
        ]
    )
    return "\n".join(lines)


def call_tool_result(
    book_id: str = "",
    rate_bps: str = DEFAULT_RATE_BPS,
    oil_pct: str = DEFAULT_OIL_PCT,
    book_name: str = "",
) -> dict[str, Any]:
    """Return the tool result in MCP wire shape: ``content``, ``structuredContent``, ``isError``.

    ``content`` is the text the host's model reads; ``structuredContent`` is what the view
    draws. An unknown book or a non-numeric shock gives an error result (``isError`` true,
    no structured content), not an exception, so the model can tell the user what went
    wrong. The MCP server and the workstation preview both return exactly this.
    """
    try:
        # The book may be given by id or by display name; a name that fits no book is an error, never the default.
        view = build_ripple_view(resolve_book_id(book_id, book_name), rate_bps, oil_pct)
    except RippleViewError as exc:
        return {"content": [{"type": "text", "text": f"{exc.code}: {exc}"}], "isError": True}
    return {
        "content": [{"type": "text", "text": summarize_for_model(view)}],
        "structuredContent": view,
        "isError": False,
    }


def load_app_html(path: Path | None = None) -> str:
    """Read the view document (one self-contained HTML file) from the skill's assets.

    It is read on every call, so an edit shows up the next time a host opens the view.

    Raises:
        FileNotFoundError: if the document is missing.
        ValueError: if it is empty or larger than MCP Apps hosts accept.
    """
    target = path or APP_HTML_PATH
    raw = target.read_bytes()
    if not raw.strip():
        raise ValueError(f"{target} is empty.")
    if len(raw) > MAX_APP_HTML_BYTES:
        raise ValueError(f"{target} is {len(raw)} bytes; MCP Apps hosts accept at most {MAX_APP_HTML_BYTES}.")
    return raw.decode("utf-8")


def build_brief(book_id: str = DEFAULT_BOOK_ID, tickers_csv: str = "") -> dict[str, Any]:
    """Three fiscal years of reported figures, and the period-over-period figure, of up to four companies.

    The view draws this when the user asks for a brief of the selected issuer or link. The values
    are those of ``tools.get_reported_financials`` and ``tools.evaluate_book_filing_deltas``;
    nothing is computed here.

    Raises:
        RippleViewError: ``invalid_input`` when no ticker is given, or the error of the
            reported-figures tool when none of the tickers has figures.
    """
    tickers = [part.strip().upper() for part in str(tickers_csv).replace(";", ",").split(",") if part.strip()]
    tickers = tickers[:MAX_BRIEF_TICKERS]
    if not tickers:
        raise RippleViewError("invalid_input", "tickers_csv must name at least one ticker.")
    reported = tools.get_reported_financials(tools.ReportedFinancialsParams(tickers=tickers, fiscal_years=3))
    if reported.get("status") != "ok" or not reported.get("companies"):
        raise RippleViewError(
            str(reported.get("error_code", "not_available")),
            str(reported.get("message", f"No reported figures are available for {', '.join(tickers)}.")),
        )
    deltas = tools.evaluate_book_filing_deltas(tools.FilingDeltaParams(book_id=str(book_id).strip()))
    delta_rows = {row["ticker"]: row for row in deltas.get("rows", [])} if deltas.get("status") == "ok" else {}

    summaries: list[dict[str, Any]] = []
    for company in reported["companies"]:
        ticker = company["ticker"]
        years = []
        for year in company.get("fiscal_years", []):
            filed, computed = year.get("reported", {}), year.get("computed", {})
            years.append(
                {
                    "fy": year["fiscal_year"],
                    "period_end": year.get("period_end"),
                    "revenue_m": (filed.get("revenue") or {}).get("value"),
                    "op_inc_m": (filed.get("operating_income") or {}).get("value"),
                    "net_inc_m": (filed.get("net_income") or {}).get("value"),
                    "rev_growth_pct": computed.get("revenue_growth_pct"),
                    "op_margin_pct": computed.get("operating_margin_pct"),
                }
            )
        delta = delta_rows.get(ticker, {})
        summaries.append(
            {
                "ticker": ticker,
                "name": company.get("name", ticker),
                "source_posture": company.get("source_posture"),
                "years": years,
                "delta_metric": delta.get("metric"),
                "delta_prior": delta.get("prior_value"),
                "delta_current": delta.get("current_value"),
                "delta_summary": delta.get("change_summary"),
                "delta_doc_id": delta.get("current_doc_id"),
            }
        )
    return {
        "status": "ok",
        "book_id": str(book_id).strip(),
        "tickers": tickers,
        "summaries": summaries,
        "not_available": reported.get("not_available", []),
    }


def brief_tool_result(book_id: str = DEFAULT_BOOK_ID, tickers_csv: str = "") -> dict[str, Any]:
    """The brief in MCP wire shape. A failure is an error result with ``code: message`` as its text."""
    try:
        brief = build_brief(book_id, tickers_csv)
    except RippleViewError as exc:
        return {"content": [{"type": "text", "text": f"{exc.code}: {exc}"}], "isError": True}
    listed = ", ".join(summary["ticker"] for summary in brief["summaries"])
    text = f"Reported Form 10-K figures of {listed} for up to three fiscal years, drawn in the view."
    return {"content": [{"type": "text", "text": text}], "structuredContent": brief, "isError": False}
