"""MCP server that shows the Form 10-K supply-chain ripple graph as an MCP App.

An MCP App is an MCP tool that ships its own view. ``show_ripple_graph`` points at a
``ui://`` resource (one self-contained HTML document, ``assets/mcp-app/ripple.html``);
a host that supports MCP Apps renders that document in a sandboxed frame and passes it
the tool result. A host that does not support them shows the tool's text result.

Scope, on purpose:

* By default (``stdio`` transport, declared in ``plugins/fsi/mcp.json``), the server
  exposes one read-only tool for the model, one tool only the view may call (its
  controls), and one resource over the bundled dataset.
* With ``--transport streamable-http`` (for Cloud Run and Gemini Enterprise Custom MCP
  Server connectors), ``build_streamable_http_app`` serves ``POST /mcp`` and ``GET /healthz``,
  and ``--full-suite`` optionally adds the deterministic Form 10-K lookup, filing-delta,
  and configured Google Sheet sync tools.
* The numbers come from the same deterministic tools the skill's ADK agent calls.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from mcp import types  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402

from scripts import ripple_view, tools  # noqa: E402

SERVER_NAME = "fsi-capital-markets-analyst"
SERVER_VERSION = "1.0.0"
VIEW_TITLE = "10-K ripple graph"

# Read-only, repeatable, and closed-world: the tool reads only the bundled dataset.
READ_ONLY = types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# Shown inline in the conversation, and openable from the host's side pane and sidebar.
# Hosts that do not know the second key ignore it.
SHOW_META = {
    "ui": {"resourceUri": ripple_view.APP_RESOURCE_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": VIEW_TITLE},
            {"type": "global", "title": VIEW_TITLE},
        ]
    },
}
# Callable by the view only; the host keeps it out of the model's tool list.
APP_ONLY_META = {"ui": {"visibility": ["app"]}}

_BOOK_PARAMS = tools.BookShockParams.model_fields
_BOOKS_TEXT = ripple_view.describe_books()
BookId = Annotated[
    str,
    Field(
        description=f"Id of the covered book: {_BOOKS_TEXT}. Leave empty when book_name is given; with neither, "
        f"the default book {ripple_view.DEFAULT_BOOK_ID} is shown."
    ),
]
BookName = Annotated[
    str,
    Field(
        description="Display name of the covered book, for example 'Mega-Cap Tech & AI Supply Chain'. An "
        "alternative to book_id: give one of the two."
    ),
]
RateBps = Annotated[ripple_view.DecimalText, Field(description=_BOOK_PARAMS["rate_bps"].description)]
OilPct = Annotated[ripple_view.DecimalText, Field(description=_BOOK_PARAMS["oil_pct"].description)]
RippleResult = Annotated[types.CallToolResult, ripple_view.RippleView]

# How a host's model is asked to handle what these tools return. That model is not the skill's evaluated
# agent and nothing reviews its answer, so the rules are short and about copying, not about judgement.
HOST_MODEL_RULES = (
    "The figures are computed by the tools. If you restate one, copy it exactly as the tool wrote it, with its "
    "sign: a negative impact is a loss and a positive one a gain. Add no figure, cause or characterisation of "
    "your own, do not recompute or extend the figures, and do not quote filing text a tool did not return to "
    "you. Write a link as 'A -> B' in plain text, without LaTeX or other markup. Do not recommend an action: "
    "this is not investment advice."
)


class StrictArgumentsServer(MCPServer):
    """An ``MCPServer`` that refuses an argument a tool does not declare.

    The SDK drops an unknown argument and runs the tool with its defaults. A model that guesses a
    parameter name then gets the default book under the name of the book it asked for. Refusing
    the call instead tells the model which arguments exist, so it can correct itself.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            declared = sorted((tool.parameters or {}).get("properties", {}))
            unknown = sorted(set(arguments or {}) - set(declared))
            if unknown:
                hint = f" Covered books: {_BOOKS_TEXT}." if "book_id" in declared else ""
                raise ToolError(
                    f"{name} has no argument named {', '.join(unknown)}. "
                    f"It takes: {', '.join(declared) or 'no arguments'}.{hint}"
                )
        return await super().call_tool(name, arguments, context)


server = StrictArgumentsServer(
    SERVER_NAME,
    title="FSI Capital Markets Analyst: ripple-graph view",
    version=SERVER_VERSION,
    instructions=(
        "Shows one read-only view: how an interest-rate and oil-price scenario moves through a covered book "
        "of issuers, with the Form 10-K passage behind every figure. " + HOST_MODEL_RULES
    ),
    log_level="WARNING",
)


def _call(book_id: str, rate_bps: str, oil_pct: str, book_name: str = "") -> types.CallToolResult:
    """Build the tool result: text for the model, structured content for the view."""
    result = ripple_view.call_tool_result(book_id, rate_bps, oil_pct, book_name)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=block["text"]) for block in result["content"]],
        structured_content=result.get("structuredContent"),
        is_error=result["isError"],
    )


@server.tool(name=ripple_view.SHOW_TOOL, title=VIEW_TITLE, annotations=READ_ONLY, meta=SHOW_META)
def show_ripple_graph(
    book_id: BookId = "",
    rate_bps: RateBps = ripple_view.DEFAULT_RATE_BPS,
    oil_pct: OilPct = ripple_view.DEFAULT_OIL_PCT,
    book_name: BookName = "",
) -> RippleResult:
    """Show how an interest-rate and oil-price scenario moves through a covered book, as an interactive view.

    Returns each holding's scaled pre-tax and after-tax impact in USD millions and its
    diluted EPS change in USD per share, the disclosed customer and supplier links reached
    from the shocked holdings, and the Form 10-K passage, document id and SHA-256 behind
    every figure and link. Each figure is a linear scaling of the issuer's own disclosed
    sensitivity, computed in decimal arithmetic; a holding whose filing gives no
    convertible figure shows 0 and the reason. Use it when the user wants to see or explore
    a rate or oil shock across a book. Every argument is optional: with none it shows the
    default book at +50 bp and oil +25%. Name the book with book_id or with book_name (its
    display name); an argument this tool does not declare is refused, and a name that fits
    no covered book is an error, never the default book. Do not use it for a passage the user pastes, for
    period-over-period filing changes, or for a workbook export; this server has no tool
    for those (they belong to the skill's ADK agent). It reads only the dataset bundled
    with the skill and changes nothing. Returns an error for a book that is not covered or
    a shock that is not a plain decimal number.
    """
    return _call(book_id, rate_bps, oil_pct, book_name)


@server.tool(
    name=ripple_view.RECOMPUTE_TOOL,
    title="Recompute the ripple graph",
    annotations=READ_ONLY,
    meta=APP_ONLY_META,
)
def recompute_ripple_graph(
    book_id: BookId = "",
    rate_bps: RateBps = ripple_view.DEFAULT_RATE_BPS,
    oil_pct: OilPct = ripple_view.DEFAULT_OIL_PCT,
    book_name: BookName = "",
) -> RippleResult:
    """Recompute the ripple-graph view when the user changes the book or the scenario inside the view.

    Takes the same arguments and returns the same result as show_ripple_graph. It exists
    so the view's own controls do not appear as model tool calls: only the view may call
    it. Returns an error for a book that is not covered or a shock that is not a plain
    decimal number.
    """
    return _call(book_id, rate_bps, oil_pct, book_name)


@server.resource(
    ripple_view.APP_RESOURCE_URI,
    name="ripple_graph_view",
    title=VIEW_TITLE,
    description="Interactive ripple graph with the filing passage behind every figure and link.",
    mime_type=ripple_view.APP_MIME_TYPE,
    # No `csp` entry: the document loads nothing from the network, so the host's
    # restrictive default policy applies unchanged.
    meta={"ui": {"prefersBorder": True}},
)
def ripple_graph_view() -> str:
    """Return the view document."""
    return ripple_view.load_app_html()


# What the view tool's description says about its limits when it is the server's only model-facing tool.
_VIEW_ONLY_LIMITS = (
    "Do not use it for a passage the user pastes, for period-over-period filing changes, or for a workbook "
    "export; this server has no tool for those (they belong to the skill's ADK agent)."
)
# The same sentence under --full-suite, where sibling tools answer those questions.
_FULL_SUITE_LIMITS = (
    "Do not use it for a company's reported results (use lookup_3yr_reported_financials), for "
    "period-over-period filing changes (use audit_book_10k_deltas) or for one issuer's market-risk disclosure "
    "(use lookup_issuer_10k_facts). It does not write the Google Sheet (use sync_scenario_to_google_sheet), and "
    "this server has no tool for a passage the user pastes or for a workbook export."
)


def _full_suite_show_description() -> str:
    """The view tool's description with its sibling tools named, for the full-suite server."""
    text = " ".join((show_ripple_graph.__doc__ or "").split())
    if _VIEW_ONLY_LIMITS not in text:
        raise RuntimeError("show_ripple_graph's docstring no longer holds the sentence the full suite replaces.")
    return text.replace(_VIEW_ONLY_LIMITS, _FULL_SUITE_LIMITS)


def _error_text(payload: dict[str, Any]) -> str | None:
    """The error of a tool dictionary as one line, or None when the tool succeeded."""
    if payload.get("status") == "error":
        return f"{payload.get('error_code', 'error')}: {payload.get('message', 'The tool failed.')}"
    return None


def reported_text(payload: dict[str, Any]) -> str:
    """Reported figures as one row per line item and one column per fiscal year.

    A host shows a long result to its model only in part. The pretty-printed JSON of one company is
    about 17,000 characters; this table is about a fifth of that and holds every reported value.
    """
    if (error := _error_text(payload)) is not None:
        return error
    lines = [
        "Annual figures as reported on Form 10-K, read from SEC XBRL company facts. One column per fiscal year, "
        "newest first. 'not tagged' means the company reports no value under the standard tags; it is not zero.",
    ]
    for company in payload.get("companies", []):
        years = company.get("fiscal_years", [])
        columns = " | ".join(f"FY{year['fiscal_year']} (ends {year['period_end']})" for year in years)
        lines.append("")
        lines.append(
            f"{company['ticker']} ({company['name']}). Source posture: {company.get('source_posture')}; "
            f"data: {company.get('data_origin')}, retrieved {company.get('retrieved_at_utc')}."
        )
        lines.append(f"Reported line item [unit] | {columns}")
        for key, item in payload.get("line_items", {}).items():
            values = [(year.get("reported", {}).get(key) or {}).get("value") for year in years]
            if any(value is not None for value in values):
                cells = " | ".join("not tagged" if value is None else str(value) for value in values)
                lines.append(f"{item['label']} [{item['unit']}] | {cells}")
        lines.append(f"Computed in Python from the rows above [unit] | {columns}")
        for key, metric in payload.get("computed_metrics", {}).items():
            values = [year.get("computed", {}).get(key) for year in years]
            if any(value is not None for value in values):
                cells = " | ".join("not computed" if value is None else str(value) for value in values)
                lines.append(f"{metric['label']} [{metric.get('unit', '')}] | {cells}")
        for year in years:
            for key, reason in (year.get("not_computed") or {}).items():
                label = payload.get("computed_metrics", {}).get(key, {}).get("label", key)
                lines.append(f"FY{year['fiscal_year']} {label}: not computed, {reason}.")
    for missing in payload.get("not_available", []):
        lines.append(f"Not available: {json.dumps(missing)}")
    lines.extend(["", HOST_MODEL_RULES])
    return "\n".join(lines)


def filing_delta_text(payload: dict[str, Any]) -> str:
    """Period-over-period figures of a book as one line per holding."""
    if (error := _error_text(payload)) is not None:
        return error
    lines = [
        f"Period-over-period figures: {payload.get('book_label')} (book_id {payload.get('book_id')}). One "
        "disclosed figure per holding, at two period ends.",
    ]
    for row in payload.get("rows", []):
        lines.append(
            f"- {row['ticker']} ({row['name']}): {row['metric']}. {row['prior_period_end']}: {row['prior_value']}; "
            f"{row['current_period_end']}: {row['current_value']}. {row['change_summary']} "
            f"Basis: {row['basis']}. Source: {row['current_doc_id']} ({row['source_posture']})."
        )
    lines.extend(["", HOST_MODEL_RULES])
    return "\n".join(lines)


def _json_result(payload: dict[str, Any], text: str | None = None) -> types.CallToolResult:
    """Wrap a deterministic tool dictionary as an MCP CallToolResult.

    ``text`` is what the host's model reads; without one it is the payload as compact JSON.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text or json.dumps(payload, separators=(",", ":")))],
        structured_content=payload,
        is_error=payload.get("status") == "error",
    )


def _split_tickers(tickers_csv: str) -> list[str]:
    return [t.strip().upper() for t in tickers_csv.replace(";", ",").split(",") if t.strip()][:6]


def _build_full_suite_server() -> MCPServer:
    """Build an MCPServer exposing the ripple MCP App plus Form 10-K & Google Sheet tools."""
    full_instructions = (
        "Form 10-K tools for a covered book of issuers: a ripple-graph view, reported annual figures, "
        "period-over-period figures and a Google Sheet model. Call these tools (show_ripple_graph, "
        "lookup_3yr_reported_financials, audit_book_10k_deltas, lookup_issuer_10k_facts, "
        "sync_scenario_to_google_sheet, sync_comps_to_google_sheet, read_scenario_from_google_sheet) rather "
        "than reading the plugin's files, running a script or searching elsewhere when one of them answers "
        "the question. " + HOST_MODEL_RULES
    )
    suite = StrictArgumentsServer(
        SERVER_NAME,
        title="FSI Capital Markets 10-K Analyst: MCP App & Sheet Suite",
        version=SERVER_VERSION,
        instructions=full_instructions,
        log_level="WARNING",
    )
    sheet_write = types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
    suite.tool(
        name=ripple_view.SHOW_TOOL,
        title=VIEW_TITLE,
        description=_full_suite_show_description(),
        annotations=READ_ONLY,
        meta=SHOW_META,
    )(show_ripple_graph)
    suite.tool(name=ripple_view.RECOMPUTE_TOOL, title="Recompute the ripple graph", annotations=READ_ONLY, meta=APP_ONLY_META)(
        recompute_ripple_graph
    )
    suite.resource(
        ripple_view.APP_RESOURCE_URI,
        name="ripple_graph_view",
        title=VIEW_TITLE,
        description="Interactive ripple graph with the filing passage behind every figure and link.",
        mime_type=ripple_view.APP_MIME_TYPE,
        meta={"ui": {"prefersBorder": True}},
    )(ripple_graph_view)

    @suite.tool(
        name="push_scenario_from_widget",
        title="Push widget scenario to Google Sheet",
        annotations=READ_ONLY,
        meta=APP_ONLY_META,
    )
    def push_scenario_from_widget(
        book_id: Annotated[str, Field(description="Covered book id.")] = ripple_view.DEFAULT_BOOK_ID,
        rate_bps: Annotated[str, Field(description="Interest-rate shock in bps.")] = ripple_view.DEFAULT_RATE_BPS,
        oil_pct: Annotated[str, Field(description="Oil/fuel shock in percent.")] = ripple_view.DEFAULT_OIL_PCT,
    ) -> types.CallToolResult:
        """Write Scenario_Control and Portfolio_Shock_Model to the configured Google Sheet from the widget."""
        return _json_result(
            tools.write_scenario_to_sheet(
                tools.BookShockParams(book_id=book_id, rate_bps=str(rate_bps), oil_pct=str(oil_pct))
            )
        )

    @suite.tool(
        name="push_comps_from_widget",
        title="Push 3-year reported comps to Google Sheet",
        annotations=READ_ONLY,
        meta=APP_ONLY_META,
    )
    def push_comps_from_widget(
        tickers_csv: Annotated[str, Field(description="Comma-separated tickers (1 to 6), e.g. 'AAPL,MSFT,NVDA'.")],
    ) -> types.CallToolResult:
        """Write Reported_Figures, Metrics, Comparison, and Sources to the configured Google Sheet from the widget."""
        return _json_result(
            tools.write_reported_figures_to_sheet(tools.ReportedWorkbookParams(tickers=_split_tickers(tickers_csv)))
        )

    @suite.tool(
        name=ripple_view.BRIEF_TOOL,
        title="Three-year Form 10-K figures of the selection",
        annotations=READ_ONLY,
        meta=APP_ONLY_META,
    )
    def brief_selection(
        book_id: Annotated[str, Field(description="Covered book id.")] = ripple_view.DEFAULT_BOOK_ID,
        tickers_csv: Annotated[str, Field(description="Comma-separated tickers (1 to 4), e.g. 'MSFT' or 'SWKS,AAPL'.")] = "AAPL",
    ) -> types.CallToolResult:
        """Return three fiscal years of reported Form 10-K figures and the period-over-period figure of the selection."""
        result = ripple_view.brief_tool_result(book_id, tickers_csv)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=block["text"]) for block in result["content"]],
            structured_content=result.get("structuredContent"),
            is_error=result["isError"],
        )

    @suite.tool(name="lookup_issuer_10k_facts", title="Lookup Form 10-K XBRL & Item 7A facts", annotations=READ_ONLY)
    def lookup_issuer_10k_facts(
        ticker: Annotated[str, Field(description="Stock ticker symbol, e.g. NVDA, AAPL, SWKS, QRVO, INTC, DAL, JPM.")],
    ) -> types.CallToolResult:
        """Return the XBRL metrics and the market-risk (Item 7A) disclosure held for one covered issuer.

        Returns revenue and long-term debt in USD millions, the effective tax rate as a fraction, diluted
        shares in millions and diluted EPS in USD per share, each with its XBRL tag and period; the
        interest-rate and oil disclosure records with their quoted Form 10-K passages; the filing's
        accession number and URL; and the source posture of the data. Use it to see what one issuer's
        filing says about its rate or commodity sensitivity. Do not use it for several years of reported
        results (use lookup_3yr_reported_financials) or to compute a scenario (use show_ripple_graph).
        Returns an error with not_in_coverage when the ticker is not covered; then say so and supply no
        figure from memory or from a search.
        """
        return _json_result(tools.lookup_statutory_filing_facts(tools.TickerLookupParams(ticker=ticker)))

    @suite.tool(name="audit_book_10k_deltas", title="Audit YoY Form 10-K filing deltas", annotations=READ_ONLY)
    def audit_book_10k_deltas(
        book_id: Annotated[str, Field(description=_BOOK_PARAMS["book_id"].description)] = ripple_view.DEFAULT_BOOK_ID,
    ) -> types.CallToolResult:
        """Report one period-over-period change per holding of a covered book, from each issuer's latest Form 10-K.

        Returns, per holding, one disclosed metric with its prior and current value and the change, and
        where the two values come from: a quoted passage of the current filing that states both periods,
        or long-term debt in USD millions at two balance-sheet dates from XBRL. The text is one line per
        holding. Use it for questions about what changed since the prior period across a book. Do not use
        it for a rate or oil scenario (use show_ripple_graph) or for a company's reported results (use
        lookup_3yr_reported_financials). Returns an error for a book that is not covered.
        """
        payload = tools.evaluate_book_filing_deltas(tools.FilingDeltaParams(book_id=book_id))
        return _json_result(payload, filing_delta_text(payload))

    @suite.tool(
        name="lookup_3yr_reported_financials",
        title="Lookup 3-year Form 10-K reported financials & peer ratios",
        annotations=READ_ONLY,
    )
    def lookup_3yr_reported_financials(
        tickers_csv: Annotated[str, Field(description="Comma-separated tickers (1 to 6), e.g. 'SWKS,QRVO,AAPL'.")],
        fiscal_years: Annotated[int, Field(description="Number of latest fiscal years (1, 2, or 3).")] = 3,
    ) -> types.CallToolResult:
        """Return the annual figures one to six companies reported on Form 10-K, read from SEC XBRL data.

        Returns, for each of the latest one to three fiscal years: revenue, cost of revenue, gross profit,
        R&D, operating income, interest expense, pre-tax income, income tax, net income, operating cash
        flow, capital expenditure, dividends, share repurchases, cash, total assets, shareholders' equity
        and long-term debt in USD millions, diluted EPS in USD per share, diluted shares in millions and
        the effective tax rate; then year-over-year changes, growth rates, margins and free cash flow
        computed in Python. The text is a table with one column per fiscal year; the structured result adds
        the XBRL tag and the Form 10-K accession number of every value. Use it for any question about a
        company's reported results, growth, margins, cash flow or debt, or for a peer comparison, rather
        than a web or document search: these are the filed figures, exact to the unit shown. Do not use it
        for a rate or oil scenario (use show_ripple_graph) or for an issuer's market-risk disclosure (use
        lookup_issuer_10k_facts). It holds no share prices, valuation multiples, estimates, guidance or
        quarterly figures. A company that is not available is listed as such with the reason and no
        figures. Returns an error for an empty ticker list or when none of the companies could be returned.
        """
        fy = max(1, min(3, int(fiscal_years)))
        payload = tools.get_reported_financials(
            tools.ReportedFinancialsParams(tickers=_split_tickers(tickers_csv), fiscal_years=fy)
        )
        return _json_result(payload, reported_text(payload))

    @suite.tool(name="read_scenario_from_google_sheet", title="Read scenario back from Google Sheet", annotations=READ_ONLY)
    def read_scenario_from_google_sheet() -> types.CallToolResult:
        """Read the scenario the user set in the configured Google Sheet and check the Sheet against the filing data.

        Reads the rate (basis points) and oil (percent) cells of the Scenario_Control tab, recomputes the
        book in Python for those two values and compares the Sheet with that result. Returns the Sheet's
        link and title, the book, the two scenario values as decimal strings, and a reconciliation: rows
        checked, rows matching, input cells that no longer equal the filing data and computed cells that
        differ from Python by more than rounding. It returns no impact figures: call show_ripple_graph with
        the returned book_id, rate_bps and oil_pct for those. Use it when the user says they changed the
        Sheet or asks what the Sheet shows now. Do not use it to write (use sync_scenario_to_google_sheet).
        Returns an error when no Sheet is configured, when the model was never written to it, when a
        scenario cell is not a number, or when the Sheet cannot be read.
        """
        return _json_result(tools.read_scenario_from_sheet(tools.SheetReadParams()))

    @suite.tool(name="sync_scenario_to_google_sheet", title="Write formula shock model to Google Sheet", annotations=sheet_write)
    def sync_scenario_to_google_sheet(
        book_id: Annotated[str, Field(description=_BOOK_PARAMS["book_id"].description)] = ripple_view.DEFAULT_BOOK_ID,
        rate_bps: Annotated[str, Field(description=_BOOK_PARAMS["rate_bps"].description)] = ripple_view.DEFAULT_RATE_BPS,
        oil_pct: Annotated[str, Field(description=_BOOK_PARAMS["oil_pct"].description)] = ripple_view.DEFAULT_OIL_PCT,
    ) -> types.CallToolResult:
        """Write the live-formula rate and oil model of a covered book into the configured Google Sheet.

        Replaces the Sheet's two tabs Scenario_Control and Portfolio_Shock_Model with the filing inputs
        and live formulas for the given scenario, then reads the computed cells back and compares them
        with Python. Returns the Sheet's link and title, the book and scenario written, the two cells the
        user may change, the number of holdings, cells and formula cells written, and a reconciliation of
        every row. It returns no impact figures (use show_ripple_graph for those). Use it when the user
        asks to put, build or refresh the scenario model in the Google Sheet. Do not use it for reported
        figures or a peer comparison (use sync_comps_to_google_sheet). It writes only to the one Sheet the
        deployment is configured with, cannot create or share a spreadsheet, and replaces anything typed
        into those two tabs. Returns an error when no Sheet is configured or it cannot be reached, for a
        book that is not covered and for a shock that is not a number.
        """
        return _json_result(
            tools.write_scenario_to_sheet(
                tools.BookShockParams(book_id=book_id, rate_bps=str(rate_bps), oil_pct=str(oil_pct))
            )
        )

    @suite.tool(name="sync_comps_to_google_sheet", title="Write 3-year peer comps tabs to Google Sheet", annotations=sheet_write)
    def sync_comps_to_google_sheet(
        tickers_csv: Annotated[str, Field(description="Comma-separated tickers (1 to 6), e.g. 'AAPL,MSFT,NVDA'.")] = "AAPL,MSFT,NVDA",
    ) -> types.CallToolResult:
        """Write the reported annual figures of one to six companies into the configured Google Sheet.

        Replaces the Sheet's four tabs Reported_Figures, Metrics, Comparison and Sources with the reported
        line items for each fiscal year, growth and margins as live formulas, each company's latest fiscal
        year side by side, and the XBRL tag and filing of every value; then reads the computed cells back
        and compares them with Python. Returns the Sheet's link, the tickers written, the number of rows,
        cells and formula cells written, a reconciliation, and any company that could not be returned. It
        returns no figures (use lookup_3yr_reported_financials for those). Use it when the user asks to
        put reported figures or a peer comparison into the Google Sheet. Do not use it for the rate and oil
        model (use sync_scenario_to_google_sheet). It writes only to the one Sheet the deployment is
        configured with and replaces anything typed into those four tabs. Returns an error when no Sheet
        is configured or it cannot be reached, for an empty ticker list and when none of the companies
        could be returned.
        """
        return _json_result(
            tools.write_reported_figures_to_sheet(tools.ReportedWorkbookParams(tickers=_split_tickers(tickers_csv)))
        )

    return suite


def build_streamable_http_app(*, full_suite: bool = False) -> Any:
    """Build an ASGI Streamable HTTP app serving ``POST /mcp`` and ``GET /healthz``."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    target = _build_full_suite_server() if full_suite else server
    inner = target.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path in ("/", "/healthz"):
                resp = JSONResponse(
                    {
                        "status": "ok",
                        "service": SERVER_NAME,
                        "mcp_endpoint": "/mcp",
                        "resource_uri": ripple_view.APP_RESOURCE_URI,
                        "full_suite": full_suite,
                        "sheet_configured": bool(os.environ.get("FSI_SHEET_ID")),
                    }
                )
                await resp(scope, receive, send)
                return
            if path.startswith("/mcp"):
                headers: list[tuple[bytes, bytes]] = []
                saw_accept = False
                for key, val in scope.get("headers", []):
                    if key.lower() == b"accept":
                        saw_accept = True
                        decoded = val.decode("latin-1")
                        if "application/json" not in decoded or "text/event-stream" not in decoded:
                            val = b"application/json, text/event-stream"
                    headers.append((key, val))
                if not saw_accept:
                    headers.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope, headers=headers)
        await inner(scope, receive, send)

    return asgi_app


def main(argv: list[str] | None = None) -> None:
    """Serve over stdio (default) or Streamable HTTP (``--transport streamable-http``)."""
    parser = argparse.ArgumentParser(description="FSI Capital Markets 10-K Ripple Graph MCP App server.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help="Include Form 10-K lookup, filing-delta, and configured Google Sheet tools (Streamable HTTP).",
    )
    args = parser.parse_args(argv)
    if args.transport == "stdio":
        (_build_full_suite_server() if args.full_suite else server).run("stdio")
        return
    import uvicorn

    uvicorn.run(build_streamable_http_app(full_suite=args.full_suite), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
