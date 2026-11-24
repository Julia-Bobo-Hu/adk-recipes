"""Reported figures in the Google Sheet: what is written, how it is paced, and the check of what the Sheet computed.

No network. The Sheet is an in-memory stand-in that keeps what was written and, when read,
computes the formulas in exact decimal arithmetic, the way the workbook tests do. Whether
Google Sheets itself computes the same is checked by a run against a real Sheet.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
import re
import sys
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import (  # noqa: E402
    fsi_engine,
    reported_financials,
    reported_workbook,
    sec_lookup,
    server,
    sheets_reported,
    sheets_sync,
    tools,
)
from scripts.sheets_sync import SheetsError  # noqa: E402

_REFERENCE = re.compile(r"([A-Za-z_]+)!([A-Z]+)(\d+)")


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


class FakeSheet:
    """In-memory spreadsheet behind the ``SheetsClient`` protocol; reads return computed values."""

    def __init__(self) -> None:
        self.tabs: dict[str, list[list[Any]]] = {"Sheet1": [["mine", 1]]}
        self.write_requests = 0
        self.formatting: list[dict[str, Any]] = []
        self.refuse_formatting = False

    def describe(self) -> dict[str, Any]:
        return {
            "url": "https://docs.google.com/spreadsheets/d/FAKE/edit",
            "title": "Fake Sheet",
            "locale": "en_US",
            "tabs": list(self.tabs),
            "tab_ids": {name: 100 + index for index, name in enumerate(self.tabs)},
        }

    def add_tabs(self, titles: list[str]) -> None:
        for title in titles:
            self.tabs[title] = []

    def clear(self, ranges: list[str]) -> None:
        for name in ranges:
            self.tabs[name.partition("!")[0]] = []

    def write(self, data: list[dict[str, Any]]) -> int:
        self.write_requests += 1
        cells = 0
        for block in data:
            tab, _, start = block["range"].partition("!")
            first = int(re.search(r"\d+", start).group())
            rows = self.tabs[tab]
            for offset, values in enumerate(block["values"]):
                while len(rows) < first + offset:
                    rows.append([])
                rows[first + offset - 1] = list(values)
                cells += len(values)
        return cells

    def read(self, ranges: list[str]) -> list[list[list[Any]]]:
        found = []
        for name in ranges:
            tab = name.partition("!")[0]
            rows = [[self._shown(tab, r, c) for c in range(len(row))] for r, row in enumerate(self.tabs[tab])]
            for row in rows:  # the API ends a row at its last non-empty cell
                while row and row[-1] == "":
                    row.pop()
            found.append(rows)
        return found

    def format(self, requests: list[dict[str, Any]]) -> None:
        if self.refuse_formatting:
            raise SheetsError("sheets_api_error", "formatting refused")
        self.formatting.extend(requests)

    def _shown(self, tab: str, row: int, column: int) -> Any:
        rows = self.tabs[tab]
        raw = rows[row][column] if row < len(rows) and column < len(rows[row]) else ""
        if isinstance(raw, str) and raw.startswith("="):
            return float(self._evaluate(raw[1:]))
        return raw[1:] if isinstance(raw, str) and raw.startswith("'") else raw

    def _evaluate(self, formula: str) -> Decimal:
        def value_of(tab: str, reference: str) -> Decimal:
            letters, number = re.fullmatch(r"([A-Z]+)(\d+)", reference).groups()
            shown = self._shown(tab, int(number) - 1, _column_index(letters))
            assert isinstance(shown, (int, float)) and not isinstance(shown, bool), f"{tab}!{reference} holds {shown!r}"
            return Decimal(repr(shown))

        def rounded(value: Decimal, places: int) -> Decimal:
            return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

        expression = _REFERENCE.sub(lambda m: f'value_of("{m.group(1)}", "{m.group(2)}{m.group(3)}")', formula)
        return eval(expression.replace("ROUND(", "rounded("), {"__builtins__": {}}, {"value_of": value_of, "rounded": rounded})  # noqa: S307


@pytest.fixture(autouse=True)
def _bundle_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sec_lookup.LIVE_LOOKUP_ENV, "false")
    monkeypatch.setenv(sec_lookup.STORE_DIR_ENV, str(tmp_path / "store"))


def _views(*tickers: str) -> list[dict[str, Any]]:
    views = []
    for ticker in tickers:
        record, origin = sec_lookup.lookup(ticker)
        views.append(reported_financials.company_view(record, years=reported_financials.KEEP_FISCAL_YEARS, origin=origin))
    return views


def test_write_creates_the_four_tabs_and_the_sheet_computes_the_python_figures() -> None:
    sheet = FakeSheet()
    views = _views("DAL", "UAL", "JPM")
    result = sheets_reported.write_reported(sheet, views)

    assert list(sheet.tabs) == ["Sheet1", *reported_workbook.SHEETS] and sheet.tabs["Sheet1"] == [["mine", 1]]
    assert result["tabs_written"] == list(reported_workbook.SHEETS) and result["companies"] == ["DAL", "UAL", "JPM"]
    assert result["company_year_rows"] == sum(len(view["fiscal_years"]) for view in views) == 12
    assert result["sheet_url"].endswith("/edit") and result["comparison_tab_url"].endswith("#gid=103")
    assert sheet.write_requests == 1 and result["formatted"] is True and sheet.formatting

    grid = reported_workbook.build_workbook_grid(views)
    assert result["formula_cells"] == sum(grid["formula_cells"].values()) > 100
    assert result["cells_written"] == sum(len(row) for rows in grid["sheets"].values() for row in rows)
    check = result["reconciliation"]
    assert check["mismatch_count"] == 0 and check["mismatches"] == []
    # Every Metrics and Comparison cell right of the identifying columns was checked: numbers and reasons alike.
    expected = sum(
        len(row) - len(reported_workbook.ID_HEADERS)
        for tab in sheets_reported.CHECKED_TABS
        for row in grid["sheets"][tab][reported_workbook.FIRST_DATA_ROW - 1 :]
    )
    assert check["cells_checked"] == check["cells_matching"] == expected


def test_a_second_write_replaces_the_tabs_and_leaves_other_tabs_alone() -> None:
    sheet = FakeSheet()
    sheets_reported.write_reported(sheet, _views("DAL", "UAL", "JPM"))
    longer = len(sheet.tabs[reported_workbook.SHEET_REPORTED])
    result = sheets_reported.write_reported(sheet, _views("AAPL"))
    assert result["companies"] == ["AAPL"] and result["reconciliation"]["mismatch_count"] == 0
    assert len(sheet.tabs[reported_workbook.SHEET_REPORTED]) < longer  # no rows of the first write are left behind
    assert sheet.tabs["Sheet1"] == [["mine", 1]]


def test_paced_write_sends_two_tabs_row_by_row_and_ends_with_the_same_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(sheets_reported.time, "sleep", pauses.append)
    views = _views("DAL", "UAL")
    at_once, paced = FakeSheet(), FakeSheet()
    sheets_reported.write_reported(at_once, views)
    result = sheets_reported.write_reported(paced, views, pace_seconds=0.2)

    grid = reported_workbook.build_workbook_grid(views)
    row_by_row = sum(len(grid["sheets"][tab]) for tab in sheets_reported.PACED_TABS)
    assert paced.write_requests == row_by_row + 2 and pauses == [0.2] * row_by_row
    assert paced.tabs == at_once.tabs and result["reconciliation"]["mismatch_count"] == 0
    assert result["cells_written"] == sum(len(row) for rows in grid["sheets"].values() for row in rows)


def test_a_cell_changed_in_the_sheet_is_reported() -> None:
    sheet = FakeSheet()
    views = _views("DAL", "UAL")
    sheets_reported.write_reported(sheet, views)
    grid = reported_workbook.build_workbook_grid(views)

    # Someone types a different revenue for Delta's latest year into the Sheet.
    revenue_column = _column_index(reported_workbook.ITEM_COLUMNS["revenue"])
    sheet.tabs[reported_workbook.SHEET_REPORTED][reported_workbook.FIRST_DATA_ROW - 1][revenue_column] = 99999
    ranges = [f"{tab}!A1:Z99" for tab in sheets_reported.CHECKED_TABS]
    check = sheets_reported.reconcile(grid, dict(zip(sheets_reported.CHECKED_TABS, sheet.read(ranges))))

    assert check["mismatch_count"] > 0 and check["cells_matching"] == check["cells_checked"] - check["mismatch_count"]
    margin_cell = f"Metrics!{reported_workbook.METRIC_COLUMNS['operating_margin_pct']}{reported_workbook.FIRST_DATA_ROW}"
    by_cell = {item["cell"]: item for item in check["mismatches"]}
    assert by_cell[margin_cell]["python_value"] == "9.19" and by_cell[margin_cell]["sheet_value"] != "9.19"
    # United's rows do not depend on Delta's revenue and still match.
    united_first = next(c for c in grid["companies"] if c["ticker"] == "UAL")["rows"][0]["row"]
    assert not any(cell.split("!")[0] == "Metrics" and int(re.search(r"\d+$", cell).group()) >= united_first for cell in by_cell)


def test_formatting_never_fails_a_write_and_a_sheet_error_is_passed_on() -> None:
    plain = FakeSheet()
    plain.refuse_formatting = True
    result = sheets_reported.write_reported(plain, _views("DAL"))
    assert result["formatted"] is False and result["reconciliation"]["mismatch_count"] == 0

    class Unreachable(FakeSheet):
        def describe(self) -> dict[str, Any]:
            raise SheetsError("sheet_permission_denied", "not shared")

    with pytest.raises(SheetsError) as raised:
        sheets_reported.write_reported(Unreachable(), _views("DAL"))
    assert raised.value.code == "sheet_permission_denied"


def test_format_requests_cover_every_tab() -> None:
    views = _views("DAL")
    grid = reported_workbook.build_workbook_grid(views)
    tab_ids = {name: 10 + index for index, name in enumerate(reported_workbook.SHEETS)}
    requests = sheets_reported.format_requests(tab_ids, grid)
    frozen = [r["updateSheetProperties"]["properties"] for r in requests if "updateSheetProperties" in r]
    assert sorted(p["sheetId"] for p in frozen) == sorted(tab_ids.values())
    assert all(p["gridProperties"]["frozenRowCount"] == reported_workbook.HEADER_ROW for p in frozen)
    bold_rows = [r["repeatCell"]["range"] for r in requests if "repeatCell" in r]
    assert all(rng["startRowIndex"] == reported_workbook.HEADER_ROW - 1 for rng in bold_rows) and len(bold_rows) == 4


def test_tools_write_the_workbook_and_the_sheet_and_name_each_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fsi_engine, "SKILL_ROOT", tmp_path)
    exported = tools.call_tool("export_reported_workbook", {"tickers": ["dal", "UAL", "ZZZZ"]})
    assert exported["status"] == "ok" and exported["companies"] == ["DAL", "UAL"]
    assert Path(exported["path"]) == tmp_path / "_output" / "fsi_reported_DAL_UAL.xlsx" and Path(exported["path"]).is_file()
    assert exported["company_year_rows"] == 8 and exported["sheets"] == list(reported_workbook.SHEETS)
    assert exported["formula_cell_count"] == sum(reported_workbook.build_workbook_grid(_views("DAL", "UAL"))["formula_cells"].values())
    assert [company["ticker"] for company in exported["not_available"]] == ["ZZZZ"]
    assert not any(key in exported for key in ("companies_data", "comparison", "fiscal_years"))  # no figures in the reply

    assert tools.call_tool("export_reported_workbook", {"tickers": []})["error_code"] == "invalid_input"
    assert tools.call_tool("export_reported_workbook", {"tickers": list("ABCDEFG")})["error_code"] == "invalid_input"
    none = tools.call_tool("export_reported_workbook", {"tickers": ["ZZZZ"]})
    assert none["status"] == "error" and none["error_code"] == "not_available" and none["not_available"]

    # Without a Sheet the Sheet tool says so before it looks any company up.
    monkeypatch.delenv(sheets_sync.SHEET_ID_ENV, raising=False)
    assert tools.call_tool("write_reported_figures_to_sheet", {"tickers": ["DAL"]})["error_code"] == "sheet_not_configured"

    sheet = FakeSheet()
    monkeypatch.setattr(sheets_sync, "client_from_env", lambda: sheet)
    written = tools.call_tool("write_reported_figures_to_sheet", {"tickers": ["DAL", "UAL", "ZZZZ"]})
    assert written["status"] == "ok" and written["companies"] == ["DAL", "UAL"]
    assert written["reconciliation"]["mismatch_count"] == 0 and written["reconciliation"]["cells_checked"] > 100
    assert written["reported_tab_url"].endswith("#gid=101") and written["comparison_tab_url"].endswith("#gid=103")
    assert [company["ticker"] for company in written["not_available"]] == ["ZZZZ"]
    assert tools.call_tool("write_reported_figures_to_sheet", {"tickers": []})["error_code"] == "invalid_input"

    class Refusing(FakeSheet):
        def describe(self) -> dict[str, Any]:
            raise SheetsError("sheet_permission_denied", "not shared")

    monkeypatch.setattr(sheets_sync, "client_from_env", lambda: Refusing())
    refused = tools.call_tool("write_reported_figures_to_sheet", {"tickers": ["DAL"]})
    assert refused["status"] == "error" and refused["error_code"] == "sheet_permission_denied"


def test_workstation_prepares_the_tabs_then_writes_them_and_refuses_a_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = FakeSheet()
    monkeypatch.setattr(server, "_sheet_client", lambda: sheet)
    monkeypatch.setattr(server, "SHEET_ROW_PACE_SECONDS", 0.0)

    prepared = server.handle_sheet_reported({"prepare": True})
    assert list(sheet.tabs) == ["Sheet1", *reported_workbook.SHEETS]
    assert all(rows == [] for name, rows in sheet.tabs.items() if name != "Sheet1") and sheet.write_requests == 0
    assert prepared["reported_tab_url"].endswith("#gid=101") and prepared["comparison_tab_url"].endswith("#gid=103")

    written = server.handle_sheet_reported({"tickers": "dal, UAL"})
    assert written["companies"] == ["DAL", "UAL"] and written["reconciliation"]["mismatch_count"] == 0
    assert written["reported_tab_url"] == prepared["reported_tab_url"] and written["not_available"] == []
    assert server.handle_sheet_reported({"tickers": ["JPM"]})["companies"] == ["JPM"]

    for bad in ({"tickers": ""}, {}, {"tickers": list("ABCDEFG")}):
        with pytest.raises(tools.ReportedRequestError) as raised:
            server.handle_sheet_reported(bad)
        assert raised.value.code == "invalid_input"

    def no_sheet() -> Any:
        raise SheetsError("sheet_not_configured", "No Google Sheet is configured.")

    monkeypatch.setattr(server, "_sheet_client", no_sheet)
    with pytest.raises(SheetsError):
        server.handle_sheet_reported({"tickers": ["DAL"]})
