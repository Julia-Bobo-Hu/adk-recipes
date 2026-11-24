"""Reported figures in the Google Sheet: write the four reported-figures tabs and check what the Sheet computed.

Companion of ``scripts.sheets_sync`` (the scenario model) for ``scripts.reported_workbook``. The
same rules hold: one configured Sheet, the tabs managed here (``Reported_Figures``, ``Metrics``,
``Comparison``, ``Sources``) are overwritten on each write, other tabs are left alone, and no
figure is taken from the Sheet. After writing, the cells Google Sheets computed are read back and
compared with the figures computed in Python.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import time
from typing import Any

from scripts import reported_workbook
from scripts.sheets_sync import SheetsClient, SheetsError

# A percentage is rounded to two decimals in Python and by ROUND(...,2) in the Sheet.
TOLERANCE = Decimal("0.0051")
# Tabs written one row at a time when the write is paced, so that someone watching sees them fill in.
PACED_TABS = (reported_workbook.SHEET_REPORTED, reported_workbook.SHEET_COMPARISON)
CHECKED_TABS = (reported_workbook.SHEET_METRICS, reported_workbook.SHEET_COMPARISON)
MAX_LISTED = 20
_IDENTITY_WIDTHS = (70, 230, 85, 95)
_VALUE_WIDTH = 130


def _column_letter(index: int) -> str:
    letters = ""
    number = index + 1
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def format_requests(tab_ids: dict[str, int], grid: dict[str, Any]) -> list[dict[str, Any]]:
    """The look of the four tabs: a bold, frozen header row and fixed column widths. Display only."""
    header_index = reported_workbook.HEADER_ROW - 1
    requests: list[dict[str, Any]] = []
    for tab in reported_workbook.SHEETS:
        sheet_id = tab_ids[tab]
        columns = max(len(row) for row in grid["sheets"][tab])
        requests.append(
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": header_index, "endRowIndex": header_index + 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "WRAP", "verticalAlignment": "BOTTOM"}},
                    "fields": "userEnteredFormat(textFormat.bold,wrapStrategy,verticalAlignment)",
                }
            }
        )
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": reported_workbook.HEADER_ROW}},
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )
        for index in range(columns):
            width = _IDENTITY_WIDTHS[index] if index < len(_IDENTITY_WIDTHS) else _VALUE_WIDTH
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
                        "properties": {"pixelSize": width},
                        "fields": "pixelSize",
                    }
                }
            )
    return requests


def ensure_tabs(client: SheetsClient) -> dict[str, Any]:
    """Add whichever of the four tabs the Sheet lacks. Returns the Sheet's description, which then lists all four."""
    described = client.describe()
    missing = [tab for tab in reported_workbook.SHEETS if tab not in described["tabs"]]
    if missing:
        client.add_tabs(missing)
        described = client.describe()
    return described


def tab_links(described: dict[str, Any]) -> dict[str, str]:
    """Links that open the Sheet on ``Reported_Figures`` and on ``Comparison``; the Sheet's own link for an unknown tab."""
    url = described.get("url", "")
    tab_ids = described.get("tab_ids") or {}

    def link(tab: str) -> str:
        return f"{url}#gid={tab_ids[tab]}" if url and tab in tab_ids else url

    return {
        "reported_tab_url": link(reported_workbook.SHEET_REPORTED),
        "comparison_tab_url": link(reported_workbook.SHEET_COMPARISON),
    }


def _formatted(client: SheetsClient, tab_ids: dict[str, int], grid: dict[str, Any]) -> bool:
    """Apply display formatting. It never fails a write: False means the Sheet kept its previous look."""
    try:
        client.format(format_requests(tab_ids, grid))
    except (SheetsError, KeyError):
        return False
    return True


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return Decimal(repr(value)) if isinstance(value, (int, float)) else Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def reconcile(grid: dict[str, Any], sheet_rows: dict[str, list[list[Any]]]) -> dict[str, Any]:
    """Compare what the Sheet shows on ``Metrics`` and ``Comparison`` with the figures computed in Python.

    Every formula cell must hold a number within rounding of the Python figure, and every cell that
    gives a reason instead of a number must still give it.

    Args:
        grid: ``reported_workbook.build_workbook_grid`` for the views that were written.
        sheet_rows: The unformatted rows of each checked tab as read from the Sheet, from row 1 down.
    """
    checked = 0
    mismatches: list[dict[str, str]] = []
    for tab in CHECKED_TABS:
        found_rows = sheet_rows.get(tab, [])
        for row_number, row in enumerate(grid["sheets"][tab], start=1):
            if row_number < reported_workbook.FIRST_DATA_ROW:
                continue
            found_row = found_rows[row_number - 1] if row_number - 1 < len(found_rows) else []
            for index, cell in enumerate(row):
                if index < len(reported_workbook.ID_HEADERS):
                    continue
                found = found_row[index] if index < len(found_row) else ""
                if cell.get("kind") == "formula":
                    expected, number = Decimal(str(cell["cached"])), _as_decimal(found)
                    same = number is not None and abs(number - expected) <= TOLERANCE
                    wanted = str(cell["cached"])
                else:
                    wanted = str(cell.get("value", ""))
                    same = str(found).strip() == wanted or (cell.get("kind") == "num" and _as_decimal(found) == Decimal(wanted))
                checked += 1
                if not same:
                    mismatches.append(
                        {
                            "cell": f"{tab}!{_column_letter(index)}{row_number}",
                            "sheet_value": "(empty)" if found in ("", None) else str(found),
                            "python_value": wanted,
                        }
                    )
    return {
        "cells_checked": checked,
        "cells_matching": checked - len(mismatches),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:MAX_LISTED],
    }


def _write_blocks(client: SheetsClient, blocks: list[dict[str, Any]], pace_seconds: float) -> int:
    """Write the tabs in one request or, when paced, ``PACED_TABS`` one row at a time. Return the cells written."""
    if pace_seconds <= 0:
        return client.write(blocks)
    cells = 0
    for block in blocks:
        tab = block["range"].partition("!")[0]
        if tab not in PACED_TABS:
            cells += client.write([block])
            continue
        for row_number, row in enumerate(block["values"], start=1):
            cells += client.write([{"range": f"{tab}!A{row_number}", "values": [row]}])
            time.sleep(pace_seconds)
    return cells


def write_reported(client: SheetsClient, views: list[dict[str, Any]], *, pace_seconds: float = 0.0) -> dict[str, Any]:
    """Write the reported figures of these companies into the Sheet's four tabs, then check what the Sheet computed.

    Args:
        client: The configured Sheet.
        views: ``reported_financials.company_view`` results holding every fiscal year on record.
        pace_seconds: When above zero, ``Reported_Figures`` and ``Comparison`` are written one row
            at a time with this pause after each row. The cells written are the same; it costs one
            request per row.

    Raises:
        SheetsError: The Sheet could not be read or written.
    """
    grid = reported_workbook.build_workbook_grid(views)
    payload = reported_workbook.build_sheets_payload(grid)
    tabs = list(reported_workbook.SHEETS)

    described = ensure_tabs(client)
    client.clear(tabs)
    formatted = _formatted(client, described.get("tab_ids") or {}, grid)
    # The payload lists Reported_Figures first, so the formulas of the other tabs never point at empty cells.
    cells_written = _write_blocks(client, payload["data"], pace_seconds)

    ranges = [f"{tab}!A1:{_column_letter(max(len(row) for row in grid['sheets'][tab]) - 1)}{len(grid['sheets'][tab])}" for tab in CHECKED_TABS]
    sheet_rows = dict(zip(CHECKED_TABS, client.read(ranges)))
    return {
        "sheet_url": described["url"],
        **tab_links(described),
        "sheet_title": described["title"],
        "tabs_written": tabs,
        "companies": [company["ticker"] for company in grid["companies"]],
        "company_year_rows": sum(len(company["rows"]) for company in grid["companies"]),
        "cells_written": cells_written,
        "formula_cells": sum(grid["formula_cells"].values()),
        "formatted": formatted,
        "reconciliation": reconcile(grid, sheet_rows),
    }
