"""Reported-figures workbook: the reported annual figures of one to six companies as a spreadsheet.

Builds an OpenXML .xlsx workbook, and the matching Google Sheets payload, with four tabs:

- ``Reported_Figures``: one row per company and fiscal year, one column per reported line
  item. A blank cell is a line item the company does not report under the XBRL tags this
  skill reads; it is not zero.
- ``Metrics``: the same rows, one column per computed metric. Where
  ``scripts.reported_financials`` computed the metric, the cell is a live formula over
  ``Reported_Figures`` and its saved value is the figure computed in Python. Where it did
  not, the cell gives the reason instead of a number.
- ``Comparison``: one row per company for its latest fiscal year, linked to the two tabs
  above.
- ``Sources``: one row per reported value, with its XBRL tag, accession number, form and
  filing date, and the first-reported value when a later filing restated it.

No figure is decided here. The values, and whether a metric can be computed at all, come
from ``scripts.reported_financials``; this module lays them out and writes the spreadsheet
formula that states the same calculation. Nothing in this module calls the Sheets API.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
import io
import re
from typing import Any
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

from scripts import reported_financials

# The cell writer and the fixed workbook parts are the ones the scenario workbook uses.
from scripts.model_exporter import _ROOT_RELS_XML, _STYLES_XML, _col_letter, _render_sheet_xml

SHEET_REPORTED = "Reported_Figures"
SHEET_METRICS = "Metrics"
SHEET_COMPARISON = "Comparison"
SHEET_SOURCES = "Sources"
SHEETS = (SHEET_REPORTED, SHEET_METRICS, SHEET_COMPARISON, SHEET_SOURCES)

HEADER_ROW = 4
FIRST_DATA_ROW = 5
ID_HEADERS = ("Ticker", "Company", "Fiscal year", "Period end")
NOT_AVAILABLE = "n/a: "
# Metrics that compare a fiscal year with the one before it.
PRIOR_YEAR_METRICS = frozenset(
    {
        "revenue_change",
        "revenue_growth_pct",
        "operating_income_growth_pct",
        "net_income_change",
        "net_income_growth_pct",
        "diluted_eps_growth_pct",
    }
)

Cell = dict[str, Any]
Rows = list[list[Cell]]

REPORTED_NOTES = (
    "Reported annual figures from Form 10-K filings, read from SEC XBRL company facts. Amounts are in USD millions "
    "unless the column heading says otherwise.",
    "A blank cell is a line item the company does not report under the XBRL tags this skill reads. It is not zero.",
    "Each value is the one the most recent Form 10-K reports for that fiscal year. The Sources tab gives the XBRL tag "
    "and the filing of every value, and the first-reported value when it was restated.",
)
METRICS_NOTES = (
    "Growth, margins and free cash flow. Each number is a live formula over Reported_Figures; the value saved in the "
    "file is the figure computed in Python. Percentages are rounded to two decimals.",
    "A cell that starts with 'n/a:' is a metric that was not computed, with the reason. It is not zero.",
    "Change a value on Reported_Figures and the formulas here and on Comparison recalculate. Not investment advice.",
)
COMPARISON_NOTES = (
    "Each row is that company's latest fiscal year; fiscal years can end on different dates.",
    "Every number is linked to Reported_Figures or Metrics.",
    "",
)
SOURCES_NOTES = (
    reported_financials.DATA_NOTICE,
    "One row per reported value. 'First reported as' is filled when a later Form 10-K restated the value.",
    "",
)
SOURCES_HEADERS = (
    "Ticker",
    "Company",
    "Fiscal year",
    "Period end",
    "Line item",
    "Value",
    "Unit",
    "XBRL tag",
    "Accession number",
    "Form",
    "Filed",
    "First reported as",
    "SEC source",
    "Retrieved (UTC)",
    "Record SHA-256",
    "Data origin",
)


def _columns(keys: Sequence[str]) -> dict[str, str]:
    """Column letter of each key; the first key sits right of the four identifying columns."""
    return {key: _col_letter(len(ID_HEADERS) + 1 + index) for index, key in enumerate(keys)}


ITEM_COLUMNS = _columns([item.key for item in reported_financials.LINE_ITEMS])
METRIC_COLUMNS = _columns([metric.key for metric in reported_financials.COMPUTED_METRICS])
COMPARISON_COLUMNS = _columns([*reported_financials.COMPARISON_ITEMS, *reported_financials.COMPARISON_METRICS])


def metric_formula(metric: str, row: int, prior_row: int | None = None) -> str:
    """The spreadsheet formula, without the leading ``=``, that states how one metric is calculated.

    Args:
        metric: A key of ``reported_financials.COMPUTED_METRICS``.
        row: The fiscal year's row on ``Reported_Figures``.
        prior_row: The row of the fiscal year before it; the year-over-year metrics need it.

    Raises:
        KeyError: Unknown metric.
        ValueError: A year-over-year metric without a prior row.
    """
    if metric in PRIOR_YEAR_METRICS and prior_row is None:
        raise ValueError(f"{metric} needs the row of the prior fiscal year")

    def at(key: str, where: int | None = None) -> str:
        return f"{SHEET_REPORTED}!{ITEM_COLUMNS[key]}{row if where is None else where}"

    def change(key: str) -> str:
        return f"{at(key)}-{at(key, prior_row)}"

    def growth(key: str) -> str:
        return f"ROUND(({change(key)})/{at(key, prior_row)}*100,2)"

    def share_of_revenue(*keys: str) -> str:
        """``keys[0]`` minus any further keys, as a percentage of revenue."""
        return f"ROUND(({'-'.join(at(key) for key in keys)})/{at('revenue')}*100,2)"

    formulas: dict[str, Callable[[], str]] = {
        "revenue_change": lambda: change("revenue"),
        "revenue_growth_pct": lambda: growth("revenue"),
        "operating_income_growth_pct": lambda: growth("operating_income"),
        "net_income_change": lambda: change("net_income"),
        "net_income_growth_pct": lambda: growth("net_income"),
        "diluted_eps_growth_pct": lambda: growth("diluted_eps"),
        "gross_margin_pct": lambda: share_of_revenue("gross_profit"),
        "operating_margin_pct": lambda: share_of_revenue("operating_income"),
        "pretax_margin_pct": lambda: share_of_revenue("pretax_income"),
        "net_margin_pct": lambda: share_of_revenue("net_income"),
        "rd_pct_of_revenue": lambda: share_of_revenue("research_and_development"),
        "effective_tax_rate_pct": lambda: f"ROUND({at('effective_tax_rate')}*100,2)",
        "free_cash_flow": lambda: f"{at('operating_cash_flow')}-{at('capital_expenditure')}",
        "free_cash_flow_margin_pct": lambda: share_of_revenue("operating_cash_flow", "capital_expenditure"),
    }
    return formulas[metric]()


def _text(value: Any) -> Cell:
    return {"value": "" if value is None else str(value)}


def _number(value: Any) -> Cell:
    """A numeric cell, or an empty cell when there is no value."""
    return {"value": ""} if value is None or value == "" else {"kind": "num", "value": value}


def _linked(formula: str, saved: Any) -> Cell:
    """A live formula whose saved value is the figure computed in Python."""
    return {"kind": "formula", "formula": formula, "cached": saved}


def _top(notes: Sequence[str], headers: Sequence[str]) -> Rows:
    """Three note rows, then the header row, so that data starts on ``FIRST_DATA_ROW`` on every sheet."""
    return [[_text(note)] for note in notes] + [[_text(header) for header in headers]]


def _not_available(reasons: dict[str, str], key: str) -> Cell:
    return _text(NOT_AVAILABLE + reasons.get(key, "not computed"))


def count_formulas(rows: Rows) -> int:
    """Number of live formula cells in one sheet's rows."""
    return sum(1 for row in rows for cell in row if cell.get("kind") == "formula")


def build_workbook_grid(views: list[dict[str, Any]]) -> dict[str, Any]:
    """Lay company views out as the four sheets.

    Args:
        views: ``reported_financials.company_view`` results, one per company. Give each view
            every fiscal year the record holds, so that a year-over-year metric finds the row
            of the year before it; without that row the Python figure is written as a plain
            number.

    Returns:
        ``sheets``: the rows of each sheet, as cells in the form ``_render_sheet_xml`` takes;
        ``companies``: for each company, the spreadsheet row of each fiscal year;
        ``formula_cells``: the number of live formula cells on each sheet.
    """
    items = reported_financials.LINE_ITEMS
    metrics = reported_financials.COMPUTED_METRICS
    reported: Rows = _top(REPORTED_NOTES, [*ID_HEADERS, *(f"{item.label} ({item.unit})" for item in items)])
    computed: Rows = _top(METRICS_NOTES, [*ID_HEADERS, *(f"{metric.label} ({metric.unit})" for metric in metrics)])
    labels = {item.key: f"{item.label} ({item.unit})" for item in items}
    labels.update({metric.key: f"{metric.label} ({metric.unit})" for metric in metrics})
    comparison: Rows = _top(COMPARISON_NOTES, [*ID_HEADERS, *(labels[key] for key in COMPARISON_COLUMNS)])
    sources: Rows = _top(SOURCES_NOTES, SOURCES_HEADERS)

    companies: list[dict[str, Any]] = []
    row = FIRST_DATA_ROW
    for view in views:
        years = view["fiscal_years"]
        latest_row = row
        placed: list[dict[str, Any]] = []
        for index, year in enumerate(years):
            prior_row = row + 1 if index + 1 < len(years) else None
            identity = [_text(view["ticker"]), _text(view["name"]), _number(year["fiscal_year"]), _text(year["period_end"])]
            reported.append(
                [*identity, *(_number((year["reported"].get(item.key) or {}).get("value")) for item in items)]
            )

            metric_cells: list[Cell] = []
            for metric in metrics:
                value = year["computed"].get(metric.key)
                if value is None:
                    metric_cells.append(_not_available(year["not_computed"], metric.key))
                elif metric.key in PRIOR_YEAR_METRICS and prior_row is None:
                    metric_cells.append(_number(value))
                else:
                    metric_cells.append(_linked(metric_formula(metric.key, row, prior_row), value))
            computed.append([*identity, *metric_cells])

            for item in items:
                value = year["reported"].get(item.key)
                if value is None:
                    continue
                filing = view["filings"].get(value["accession_number"], {})
                sources.append(
                    [
                        *identity,
                        _text(item.label),
                        _number(value["value"]),
                        _text(item.unit),
                        _text(value.get("tag") or view["tags"].get(item.key)),
                        _text(value["accession_number"]),
                        _text(filing.get("form")),
                        _text(filing.get("filed")),
                        _number(value.get("restated_from")),
                        _text(view["source_url"]),
                        _text(view["retrieved_at_utc"]),
                        _text(view["facts_sha256"]),
                        _text(view["data_origin"]),
                    ]
                )
            placed.append({"fiscal_year": year["fiscal_year"], "period_end": year["period_end"], "row": row})
            row += 1

        latest = years[0]
        cells = [_text(view["ticker"]), _text(view["name"]), _number(latest["fiscal_year"]), _text(latest["period_end"])]
        for key in reported_financials.COMPARISON_ITEMS:
            value = (latest["reported"].get(key) or {}).get("value")
            if value is None:
                cells.append(_text(NOT_AVAILABLE + "not tagged"))
            else:
                cells.append(_linked(f"{SHEET_REPORTED}!{ITEM_COLUMNS[key]}{latest_row}", value))
        for key in reported_financials.COMPARISON_METRICS:
            value = latest["computed"].get(key)
            if value is None:
                cells.append(_not_available(latest["not_computed"], key))
            else:
                cells.append(_linked(f"{SHEET_METRICS}!{METRIC_COLUMNS[key]}{latest_row}", value))
        comparison.append(cells)
        companies.append({"ticker": view["ticker"], "name": view["name"], "rows": placed})

    sheets = {SHEET_REPORTED: reported, SHEET_METRICS: computed, SHEET_COMPARISON: comparison, SHEET_SOURCES: sources}
    return {
        "sheets": sheets,
        "companies": companies,
        "formula_cells": {name: count_formulas(rows) for name, rows in sheets.items()},
    }


def _package(sheets: Sequence[tuple[str, Rows]]) -> bytes:
    """Zip the sheets into an .xlsx file."""
    numbered = list(enumerate(sheets, start=1))
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for number, _ in numbered
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(
            f'<sheet name="{escape(name)}" sheetId="{number}" r:id="rId{number}"/>' for number, (name, _) in numbered
        )
        + "</sheets></workbook>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{number}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{number}.xml"/>'
            for number, _ in numbered
        )
        + f'<Relationship Id="rId{len(numbered) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", _ROOT_RELS_XML)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/styles.xml", _STYLES_XML)
        for number, (_, rows) in numbered:
            archive.writestr(f"xl/worksheets/sheet{number}.xml", _render_sheet_xml(rows))
    return buffer.getvalue()


def export_workbook_bytes(views: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    """Build the four-sheet .xlsx workbook for these company views. Returns the file and its grid."""
    grid = build_workbook_grid(views)
    return _package([(name, grid["sheets"][name]) for name in SHEETS]), grid


def inspect_workbook(xlsx_bytes: bytes) -> dict[str, int]:
    """Number of live ``<f>`` formula cells on each sheet of a workbook built here, read from the file."""
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    counts: dict[str, int] = {}
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        for number, name in enumerate(SHEETS, start=1):
            root = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
            counts[name] = len(root.findall(".//s:f", namespace))
    return counts


_READ_AS_SOMETHING_ELSE = re.compile(r"^[=+\-'0-9]|^(true|false)$", re.IGNORECASE)


def _sheets_value(cell: Cell) -> Any:
    """One cell as a ``USER_ENTERED`` value: a JSON number, a formula, or text that stays text."""
    kind = cell.get("kind", "str")
    if kind == "formula":
        return "=" + str(cell["formula"])
    if kind == "num":
        number = Decimal(str(cell["value"]))
        return int(number) if number == number.to_integral_value() else float(number)
    text = str(cell.get("value", ""))
    # Sheets would read "2025-12-31" as a date and an accession number as arithmetic. A leading
    # apostrophe means "keep this as text" and is not displayed.
    return "'" + text if _READ_AS_SOMETHING_ELSE.match(text) else text


def build_sheets_payload(grid: dict[str, Any]) -> dict[str, Any]:
    """The Google Sheets API v4 ``spreadsheets.values.batchUpdate`` body for the same four tabs.

    The tabs must exist in the spreadsheet under the names in ``SHEETS``. Values are sent as
    ``USER_ENTERED`` so that the formulas are live.
    """
    return {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": f"{name}!A1", "values": [[_sheets_value(cell) for cell in row] for row in grid["sheets"][name]]}
            for name in SHEETS
        ],
    }
