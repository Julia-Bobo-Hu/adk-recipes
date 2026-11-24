"""The reported-figures workbook: layout, live formulas that state the Python calculation, and the Sheets payload.

No network and no model. The bundled SEC XBRL records of the 16 issuers are the input. The
formulas are checked with a small evaluator in this file, not with a spreadsheet application.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
import io
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine, reported_financials, reported_workbook, sec_lookup  # noqa: E402

BUNDLED = sorted(fsi_engine.load_sample_book()["companies"])
SPREADSHEET_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REFERENCE = re.compile(r"Reported_Figures!([A-Z]+)(\d+)")


@pytest.fixture(autouse=True)
def _bundle_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sec_lookup.LIVE_LOOKUP_ENV, "false")
    monkeypatch.setenv(sec_lookup.STORE_DIR_ENV, str(tmp_path / "store"))


def _views(*tickers: str, years: int = reported_financials.KEEP_FISCAL_YEARS) -> list[dict[str, Any]]:
    views = []
    for ticker in tickers:
        record, origin = sec_lookup.lookup(ticker)
        views.append(reported_financials.company_view(record, years=years, origin=origin))
    return views


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _cell(rows: list[list[dict[str, Any]]], reference: str) -> dict[str, Any]:
    letters, number = re.fullmatch(r"([A-Z]+)(\d+)", reference).groups()
    row = rows[int(number) - 1]
    column = _column_index(letters)
    return row[column] if column < len(row) else {"value": ""}


def _evaluate(formula: str, reported_rows: list[list[dict[str, Any]]]) -> Decimal:
    """What a spreadsheet would compute for a Metrics formula, in exact decimal arithmetic."""

    def value_of(reference: str) -> Decimal:
        cell = _cell(reported_rows, reference)
        assert cell.get("kind") == "num", f"{reference} is referenced by a formula but holds no number"
        return Decimal(str(cell["value"]))

    def rounded(value: Decimal, places: int) -> Decimal:
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

    expression = _REFERENCE.sub(lambda match: f'value_of("{match.group(1)}{match.group(2)}")', formula)
    expression = expression.replace("ROUND(", "rounded(")
    return eval(expression, {"__builtins__": {}}, {"value_of": value_of, "rounded": rounded})  # noqa: S307 - our own formulas


def test_every_computed_metric_has_a_formula() -> None:
    keys = {metric.key for metric in reported_financials.COMPUTED_METRICS}
    assert reported_workbook.PRIOR_YEAR_METRICS <= keys
    for metric in reported_financials.COMPUTED_METRICS:
        formula = reported_workbook.metric_formula(metric.key, 5, 6)
        assert "Reported_Figures!" in formula, metric.key
        if metric.unit == reported_financials.PERCENT:
            assert formula.startswith("ROUND(") and formula.endswith(",2)"), metric.key
        assert ("6" in formula) == (metric.key in reported_workbook.PRIOR_YEAR_METRICS), metric.key
    assert reported_workbook.metric_formula("operating_margin_pct", 5) == "ROUND((Reported_Figures!I5)/Reported_Figures!E5*100,2)"
    assert reported_workbook.metric_formula("revenue_growth_pct", 5, 6) == (
        "ROUND((Reported_Figures!E5-Reported_Figures!E6)/Reported_Figures!E6*100,2)"
    )
    with pytest.raises(KeyError):
        reported_workbook.metric_formula("no_such_metric", 5, 6)
    with pytest.raises(ValueError, match="prior fiscal year"):
        reported_workbook.metric_formula("revenue_growth_pct", 5)


def test_every_formula_recalculates_to_the_python_figure_for_all_bundled_companies() -> None:
    views = _views(*BUNDLED)
    grid = reported_workbook.build_workbook_grid(views)
    reported_rows = grid["sheets"][reported_workbook.SHEET_REPORTED]
    metric_rows = grid["sheets"][reported_workbook.SHEET_METRICS]

    checked = 0
    explained = 0
    for row in metric_rows[reported_workbook.FIRST_DATA_ROW - 1 :]:
        for cell in row[len(reported_workbook.ID_HEADERS) :]:
            if cell.get("kind") == "formula":
                assert _evaluate(cell["formula"], reported_rows) == Decimal(str(cell["cached"])), cell
                checked += 1
            else:
                assert str(cell["value"]).startswith(reported_workbook.NOT_AVAILABLE), cell
                explained += 1
    computed_in_python = sum(len(year["computed"]) for view in views for year in view["fiscal_years"])
    not_computed_in_python = sum(len(year["not_computed"]) for view in views for year in view["fiscal_years"])
    # Every figure Python computed is a formula, and every one it declined is a reason: nothing in between.
    assert (checked, explained) == (computed_in_python, not_computed_in_python)
    assert checked > 500 and grid["formula_cells"][reported_workbook.SHEET_METRICS] == checked

    # A year-over-year formula never reaches into the next company's rows.
    last_row_of = {company["ticker"]: company["rows"][-1]["row"] for company in grid["companies"]}
    for company in grid["companies"]:
        for placed in company["rows"]:
            for cell in metric_rows[placed["row"] - 1]:
                for _, number in _REFERENCE.findall(str(cell.get("formula", ""))):
                    assert placed["row"] <= int(number) <= last_row_of[company["ticker"]], (company["ticker"], cell)


def test_comparison_links_each_company_latest_year() -> None:
    grid = reported_workbook.build_workbook_grid(_views("DAL", "UAL", "JPM"))
    comparison = grid["sheets"][reported_workbook.SHEET_COMPARISON]
    rows = {row[0]["value"]: row for row in comparison[reported_workbook.FIRST_DATA_ROW - 1 :]}
    assert list(rows) == ["DAL", "UAL", "JPM"]

    united_row = next(c for c in grid["companies"] if c["ticker"] == "UAL")["rows"][0]["row"]
    revenue = rows["UAL"][len(reported_workbook.ID_HEADERS)]
    assert revenue == {"kind": "formula", "formula": f"Reported_Figures!E{united_row}", "cached": "59070"}
    margin_column = reported_workbook.COMPARISON_COLUMNS["operating_margin_pct"]
    margin = _cell(comparison, f"{margin_column}{reported_workbook.FIRST_DATA_ROW + 1}")
    assert margin["formula"] == f"Metrics!{reported_workbook.METRIC_COLUMNS['operating_margin_pct']}{united_row}"
    assert margin["cached"] == "7.98"

    # A bank reports no gross profit: the comparison says so instead of showing a number.
    gross_column = reported_workbook.COMPARISON_COLUMNS["gross_margin_pct"]
    bank = _cell(comparison, f"{gross_column}{reported_workbook.FIRST_DATA_ROW + 2}")
    assert bank.get("kind") != "formula" and bank["value"].startswith("n/a: ") and "gross_profit" in bank["value"]


def test_a_missing_figure_is_blank_or_explained_and_never_zero() -> None:
    views = _views("DAL")
    grid = reported_workbook.build_workbook_grid(views)
    reported_rows = grid["sheets"][reported_workbook.SHEET_REPORTED]
    metric_rows = grid["sheets"][reported_workbook.SHEET_METRICS]
    first, last = grid["companies"][0]["rows"][0]["row"], grid["companies"][0]["rows"][-1]["row"]

    assert "cost_of_revenue" in views[0]["fiscal_years"][0]["not_tagged"]
    assert _cell(reported_rows, f"{reported_workbook.ITEM_COLUMNS['cost_of_revenue']}{first}") == {"value": ""}
    gross = _cell(metric_rows, f"{reported_workbook.METRIC_COLUMNS['gross_margin_pct']}{first}")
    assert gross == {"value": "n/a: gross_profit is not tagged"}
    # The oldest year on record has no year before it.
    growth = _cell(metric_rows, f"{reported_workbook.METRIC_COLUMNS['revenue_growth_pct']}{last}")
    assert growth == {"value": "n/a: the record holds no prior fiscal year"}


def test_a_shorter_view_keeps_the_python_figure_when_the_prior_year_is_not_on_the_sheet() -> None:
    views = _views("DAL", years=3)
    grid = reported_workbook.build_workbook_grid(views)
    third = grid["companies"][0]["rows"][-1]["row"]
    growth = _cell(grid["sheets"][reported_workbook.SHEET_METRICS], f"{reported_workbook.METRIC_COLUMNS['revenue_growth_pct']}{third}")
    assert growth == {"kind": "num", "value": views[0]["fiscal_years"][2]["computed"]["revenue_growth_pct"]}
    # A same-year metric of that row is still a live formula.
    margin = _cell(grid["sheets"][reported_workbook.SHEET_METRICS], f"{reported_workbook.METRIC_COLUMNS['operating_margin_pct']}{third}")
    assert margin["kind"] == "formula"


def test_sources_lists_every_reported_value_with_its_tag_and_filing() -> None:
    views = _views("BA", "HXL")
    grid = reported_workbook.build_workbook_grid(views)
    rows = grid["sheets"][reported_workbook.SHEET_SOURCES][reported_workbook.FIRST_DATA_ROW - 1 :]
    assert len(rows) == sum(len(year["reported"]) for view in views for year in view["fiscal_years"])
    headers = [cell["value"] for cell in grid["sheets"][reported_workbook.SHEET_SOURCES][reported_workbook.HEADER_ROW - 1]]
    assert tuple(headers) == reported_workbook.SOURCES_HEADERS

    def column(row: list[dict[str, Any]], header: str) -> Any:
        return row[headers.index(header)].get("value")

    for row in rows:
        assert re.fullmatch(r"us-gaap:[A-Za-z]+", column(row, "XBRL tag")), row
        assert re.fullmatch(r"\d{10}-\d{2}-\d{6}", column(row, "Accession number")), row
        assert column(row, "Form") == "10-K" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", column(row, "Filed")), row
        assert column(row, "SEC source").startswith("https://data.sec.gov/") and len(column(row, "Record SHA-256")) == 64

    # A restated value carries the first-reported figure, and a value-level tag wins over the item's usual tag.
    restated = next(r for r in rows if r[0]["value"] == "BA" and column(r, "Line item") == "Cost of revenue" and column(r, "Period end") == "2022-12-31")
    assert (column(restated, "Value"), column(restated, "First reported as")) == ("63078", "63106")
    hexcel = next(r for r in rows if r[0]["value"] == "HXL" and column(r, "Line item") == "Net income" and column(r, "Period end") == "2022-12-31")
    assert column(hexcel, "XBRL tag") == "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic"


def test_the_file_is_a_workbook_with_four_sheets_and_live_formulas() -> None:
    xlsx, grid = reported_workbook.export_workbook_bytes(_views("DAL", "UAL"))
    assert xlsx[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(xlsx)) as archive:
        names = set(archive.namelist())
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        metrics_sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
    assert {f"xl/worksheets/sheet{n}.xml" for n in range(1, 5)} <= names and "xl/styles.xml" in names
    assert [sheet.get("name") for sheet in workbook.findall(".//s:sheet", SPREADSHEET_NS)] == list(reported_workbook.SHEETS)

    counts = reported_workbook.inspect_workbook(xlsx)
    assert counts == grid["formula_cells"]
    assert counts["Reported_Figures"] == 0 and counts["Sources"] == 0
    assert counts["Metrics"] > 60 and counts["Comparison"] >= 14

    # Delta's latest operating margin: the formula is in the file and its saved value is the Python figure.
    cells = {cell.get("r"): cell for cell in metrics_sheet.findall(".//s:c", SPREADSHEET_NS)}
    margin = cells[f"{reported_workbook.METRIC_COLUMNS['operating_margin_pct']}5"]
    assert margin.find("s:f", SPREADSHEET_NS).text == "ROUND((Reported_Figures!I5)/Reported_Figures!E5*100,2)"
    assert margin.find("s:v", SPREADSHEET_NS).text == "9.19"


def test_sheets_payload_keeps_text_as_text_and_formulas_live() -> None:
    grid = reported_workbook.build_workbook_grid(_views("DAL", "UAL"))
    payload = reported_workbook.build_sheets_payload(grid)
    assert payload["valueInputOption"] == "USER_ENTERED"
    assert [block["range"] for block in payload["data"]] == [f"{name}!A1" for name in reported_workbook.SHEETS]
    reported, metrics, _, sources = (block["values"] for block in payload["data"])

    first = reported[reported_workbook.FIRST_DATA_ROW - 1]
    assert first[:4] == ["DAL", "Delta Air Lines, Inc.", 2025, "'2025-12-31"]
    assert first[_column_index(reported_workbook.ITEM_COLUMNS["revenue"])] == 63364
    assert first[_column_index(reported_workbook.ITEM_COLUMNS["effective_tax_rate"])] == 0.191
    assert first[_column_index(reported_workbook.ITEM_COLUMNS["cost_of_revenue"])] == ""
    margin = metrics[reported_workbook.FIRST_DATA_ROW - 1][_column_index(reported_workbook.METRIC_COLUMNS["operating_margin_pct"])]
    assert margin == "=ROUND((Reported_Figures!I5)/Reported_Figures!E5*100,2)"
    accession = sources[reported_workbook.FIRST_DATA_ROW - 1][reported_workbook.SOURCES_HEADERS.index("Accession number")]
    assert accession.startswith("'0000027904-")
    # Same shape as the workbook: a row of the payload has a value for every cell of the sheet's row.
    for name, block in zip(reported_workbook.SHEETS, payload["data"]):
        assert [len(row) for row in block["values"]] == [len(row) for row in grid["sheets"][name]]
