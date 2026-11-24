"""Google Sheet link: write the scenario model into one Google Sheet and read the analyst's changes back.

The skill may touch exactly one spreadsheet, the one named by ``FSI_SHEET_ID``. A person
creates that Sheet and shares it, as Editor, with the identity the skill runs as. The skill
never creates, shares or deletes a spreadsheet, and the model cannot choose another one.
Two tabs are managed here and are overwritten on every write: ``Scenario_Control`` and
``Portfolio_Shock_Model``. Other tabs are left alone.

- ``write_scenario`` writes the same live-formula model as the .xlsx export, then reads the
  computed cells back and compares them with the Python engine.
- ``read_scenario`` reads the two scenario cells the analyst may have changed, recomputes
  the book in Python for those values and reports where the Sheet differs from the filing
  data: an input cell someone edited, or a computed cell that does not match the engine.

Figures are never taken from the Sheet. The Sheet supplies the two scenario values; the
figures come from ``scripts.fsi_engine``.

Credentials are keyless. With ``FSI_SHEETS_SERVICE_ACCOUNT`` set, Application Default
Credentials mint a short-lived token for that service account (the caller needs
``roles/iam.serviceAccountTokenCreator`` on it). Without it, Application Default
Credentials are used directly, which is the case on a runtime that already runs as the
service account. The only scope requested is the Google Sheets scope. ``google-auth`` and
``requests`` come with ``google-genai``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import functools
import os
import re
import time
from typing import Any, Protocol

from scripts import fsi_engine, model_exporter

SHEET_ID_ENV = "FSI_SHEET_ID"
SERVICE_ACCOUNT_ENV = "FSI_SHEETS_SERVICE_ACCOUNT"
ROW_PACE_ENV = "FSI_SHEET_ROW_PACE_SECONDS"
MAX_ROW_PACE_SECONDS = 1.0
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT_SECONDS = 30
TOKEN_LIFETIME_SECONDS = 900

CONTROL_TAB = "Scenario_Control"
MODEL_TAB = "Portfolio_Shock_Model"
MANAGED_TABS = (CONTROL_TAB, MODEL_TAB)
RATE_CELL = f"{CONTROL_TAB}!B2"
OIL_CELL = f"{CONTROL_TAB}!B3"
BOOK_CELL = f"{CONTROL_TAB}!B4"
# Rows read back from the model tab. A covered book has far fewer holdings.
MAX_MODEL_ROWS = 200
MAX_LISTED = 20

# Model tab columns, as written by model_exporter.build_google_sheets_batch_payload: letter -> index in a row.
INPUT_COLUMNS = {"C": 2, "D": 3, "E": 4, "F": 5, "J": 9, "L": 11, "N": 13, "P": 15}
# Computed columns: letter, index, key in the engine's evaluated row, tolerance for the engine's rounding.
OUTPUT_COLUMNS: tuple[tuple[str, int, str, Decimal | None], ...] = (
    ("G", 6, "rate_scaled_m", Decimal("0.006")),
    ("H", 7, "oil_scaled_m", Decimal("0.006")),
    ("I", 8, "total_pre_tax_m", Decimal("0.011")),
    ("K", 10, "after_tax_m", Decimal("0.011")),
    ("M", 12, "eps_delta", Decimal("0.00011")),
    ("O", 14, "guardrail_status", None),
)
INPUT_TOLERANCE = Decimal("0.000000001")

_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class SheetsError(RuntimeError):
    """The Sheet could not be used. ``code`` is a short reason a tool can return as ``error_code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SheetsClient(Protocol):
    """The few spreadsheet operations this module needs, on one spreadsheet."""

    def describe(self) -> dict[str, Any]:
        """Return ``{"url", "title", "locale", "tabs", "tab_ids"}`` of the spreadsheet."""

    def add_tabs(self, titles: list[str]) -> None:
        """Add empty tabs with these titles."""

    def clear(self, ranges: list[str]) -> None:
        """Remove the values in these ranges."""

    def write(self, data: list[dict[str, Any]]) -> int:
        """Write ``[{"range", "values"}]`` as if typed by a user, so formulas are live. Return the cells written."""

    def read(self, ranges: list[str]) -> list[list[list[Any]]]:
        """Return the unformatted values of each range, one list of rows per range."""

    def format(self, requests: list[dict[str, Any]]) -> None:
        """Apply formatting requests (Sheets API ``batchUpdate`` requests). Values are not touched."""


class GoogleSheetsClient:
    """The Google Sheets API v4 over an authorised HTTPS session. Keyless; see the module docstring."""

    def __init__(self, spreadsheet_id: str, *, service_account: str | None = None, session: Any | None = None) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.service_account = service_account
        self._session = session

    def _authorised_session(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth import impersonated_credentials
            from google.auth.transport.requests import AuthorizedSession

            if self.service_account:
                source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
                credentials: Any = impersonated_credentials.Credentials(
                    source_credentials=source,
                    target_principal=self.service_account,
                    target_scopes=[SHEETS_SCOPE],
                    lifetime=TOKEN_LIFETIME_SECONDS,
                )
            else:
                credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _call(
        self, method: str, path: str = "", *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        identity = self.service_account or "the identity this skill runs as"
        try:
            response = self._authorised_session().request(
                method, f"{API_ROOT}/{self.spreadsheet_id}{path}", params=params, json=body, timeout=TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - credentials and network failures become one tool error
            raise SheetsError(
                "sheets_unreachable", f"Google Sheets could not be reached as {identity}: {type(exc).__name__}: {exc}"[:400]
            ) from exc
        if response.status_code == 200:
            return dict(response.json())
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except ValueError:
            detail = response.text[:200]
        if response.status_code == 403:
            raise SheetsError(
                "sheet_permission_denied",
                f"Google Sheets refused the request. Share the Sheet with {identity} as Editor and check that the "
                f"Google Sheets API is enabled. ({detail[:200]})",
            )
        if response.status_code == 404:
            raise SheetsError("sheet_not_found", f"No spreadsheet with the configured id is visible to {identity}.")
        raise SheetsError("sheets_api_error", f"Google Sheets returned HTTP {response.status_code}: {detail[:300]}")

    def describe(self) -> dict[str, Any]:
        fields = "spreadsheetUrl,properties(title,locale),sheets.properties(sheetId,title)"
        found = self._call("GET", params={"fields": fields})
        properties = found.get("properties") or {}
        tabs = [sheet["properties"] for sheet in found.get("sheets") or []]
        return {
            "url": found.get("spreadsheetUrl", ""),
            "title": properties.get("title", ""),
            "locale": properties.get("locale", ""),
            "tabs": [tab["title"] for tab in tabs],
            "tab_ids": {tab["title"]: int(tab.get("sheetId", 0)) for tab in tabs},
        }

    def add_tabs(self, titles: list[str]) -> None:
        requests = [{"addSheet": {"properties": {"title": title}}} for title in titles]
        self._call("POST", ":batchUpdate", body={"requests": requests})

    def clear(self, ranges: list[str]) -> None:
        self._call("POST", "/values:batchClear", body={"ranges": ranges})

    def write(self, data: list[dict[str, Any]]) -> int:
        written = self._call("POST", "/values:batchUpdate", body={"valueInputOption": "USER_ENTERED", "data": data})
        return int(written.get("totalUpdatedCells") or 0)

    def read(self, ranges: list[str]) -> list[list[list[Any]]]:
        found = self._call("GET", "/values:batchGet", params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"})
        return [list(value_range.get("values") or []) for value_range in found.get("valueRanges") or []]

    def format(self, requests: list[dict[str, Any]]) -> None:
        self._call("POST", ":batchUpdate", body={"requests": requests})


def sheet_id_from(text: str) -> str | None:
    """The spreadsheet id in ``text``, which may be the id itself or the Sheet's link."""
    cleaned = text.strip()
    in_link = _SHEET_URL_RE.search(cleaned)
    if in_link:
        return in_link.group(1)
    return cleaned if _SHEET_ID_RE.match(cleaned) else None


@functools.lru_cache(maxsize=8)
def _google_client(sheet_id: str, service_account: str | None) -> GoogleSheetsClient:
    """One client per Sheet and identity, kept for the life of the process: its session renews its own token."""
    return GoogleSheetsClient(sheet_id, service_account=service_account)


def client_for(sheet: str, service_account: str | None = None) -> SheetsClient:
    """The client for one Sheet, named by its id or its link.

    Raises:
        SheetsError: ``sheet`` is empty, or is neither an id nor a link (``sheet_not_configured``).
    """
    configured = sheet.strip()
    if not configured:
        raise SheetsError(
            "sheet_not_configured",
            f"No Google Sheet is configured. Set {SHEET_ID_ENV} to the id or the link of a Sheet that is shared "
            "with the skill's identity as Editor.",
        )
    sheet_id = sheet_id_from(configured)
    if sheet_id is None:
        raise SheetsError("sheet_not_configured", f"{SHEET_ID_ENV} is neither a spreadsheet id nor a Google Sheets link.")
    return _google_client(sheet_id, (service_account or "").strip() or None)


def client_from_env() -> SheetsClient:
    """The client for the Sheet ``FSI_SHEET_ID`` names, acting as ``FSI_SHEETS_SERVICE_ACCOUNT`` when that is set.

    Raises:
        SheetsError: ``FSI_SHEET_ID`` is unset, or is neither an id nor a link (``sheet_not_configured``).
    """
    return client_for(os.environ.get(SHEET_ID_ENV, ""), os.environ.get(SERVICE_ACCOUNT_ENV, ""))


def row_pace_seconds(default: float = 0.0) -> float:
    """The pause after each model row that ``FSI_SHEET_ROW_PACE_SECONDS`` asks for, between 0 and one second.

    Zero means the model is written in one request. An unset, empty or unreadable value gives ``default``.
    """
    raw = os.environ.get(ROW_PACE_ENV, "").strip()
    try:
        seconds = float(raw) if raw else default
    except ValueError:
        seconds = default
    return min(max(seconds, 0.0), MAX_ROW_PACE_SECONDS)


def tab_url(described: dict[str, Any], tab: str = MODEL_TAB) -> str:
    """The Sheet's link opened on one tab; the plain link when the Sheet has no such tab."""
    base = str(described.get("url") or "").split("#")[0].split("?")[0]
    tab_id = (described.get("tab_ids") or {}).get(tab)
    return base if tab_id is None else f"{base}#gid={tab_id}"


def _as_decimal(value: Any) -> Decimal | None:
    """A cell value as a number, or None when the cell is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    """Plain decimal text without an exponent or trailing zeros: 50.0 -> '50', -12.50 -> '-12.5'."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _shown(value: Any) -> str:
    """How a cell value is quoted in a finding: an empty cell by name, a computed number to four decimals."""
    if value is None or value == "":
        return "(empty)"
    if isinstance(value, float):
        return _decimal_text(Decimal(repr(value)).quantize(Decimal("0.0001")))
    return str(value)


def _cell(row: list[Any], index: int) -> Any:
    """A cell of a row as the API returns it: rows end at their last non-empty cell."""
    return row[index] if index < len(row) else ""


def _same_input(sheet_value: Any, filing_value: Any) -> bool:
    """True when an input cell still holds the filing value (an empty cell stands for 'no figure')."""
    if filing_value is None or filing_value == "":
        return sheet_value is None or sheet_value == ""
    expected, found = _as_decimal(filing_value), _as_decimal(sheet_value)
    if expected is None:
        return str(sheet_value).strip() == str(filing_value).strip()
    return found is not None and abs(found - expected) <= max(INPUT_TOLERANCE, abs(expected) * INPUT_TOLERANCE)


def reconcile(sheet_rows: list[list[Any]], analysis: dict[str, Any]) -> dict[str, Any]:
    """Compare the model tab, as read from the Sheet, with the Python engine's result for the same scenario.

    Args:
        sheet_rows: The rows of ``Portfolio_Shock_Model`` from row 1 (the header) down, unformatted.
        analysis: ``fsi_engine.analyze_book`` for the book and the scenario the Sheet holds.

    Returns:
        ``rows_checked`` and ``rows_matching`` (a row matches when it has no finding);
        ``edited_inputs``, input cells whose value differs from the filing data the engine
        uses; ``mismatches``, computed cells that differ from the engine's figure by more than
        its rounding, and holdings missing from the tab; ``unexpected_tickers``, rows whose
        ticker is not a holding of the book. The two lists show at most ``MAX_LISTED`` entries
        and the counts give the totals.
    """
    grid = model_exporter.build_formula_grid(analysis)
    by_ticker: dict[str, tuple[int, list[Any]]] = {}
    for offset, row in enumerate(sheet_rows[1:], start=2):
        ticker = str(_cell(row, 0)).strip().upper()
        if ticker and ticker not in by_ticker:
            by_ticker[ticker] = (offset, row)

    edited: list[dict[str, str]] = []
    mismatches: list[dict[str, str]] = []
    matching = 0
    for expected in grid["rows"]:
        ticker, excel_row = expected["ticker"], expected["excel_row"]
        if ticker not in by_ticker:
            mismatches.append({"ticker": ticker, "cell": MODEL_TAB, "sheet_value": "(row missing)", "engine_value": "holding of the book"})
            continue
        sheet_row_number, row = by_ticker[ticker]
        findings_before = len(edited) + len(mismatches)
        for letter, index in INPUT_COLUMNS.items():
            filing_value = expected["inputs"][f"{letter}{excel_row}"]
            if not _same_input(_cell(row, index), filing_value):
                edited.append(
                    {
                        "ticker": ticker,
                        "cell": f"{MODEL_TAB}!{letter}{sheet_row_number}",
                        "sheet_value": _shown(_cell(row, index)),
                        "filing_value": _shown(filing_value),
                    }
                )
        for letter, index, key, tolerance in OUTPUT_COLUMNS:
            engine_value = expected["evaluated"][key]
            found = _cell(row, index)
            if tolerance is None:
                same = str(found).strip() == str(engine_value)
            else:
                number = _as_decimal(found)
                same = number is not None and abs(number - Decimal(str(engine_value))) <= tolerance
            if not same:
                mismatches.append(
                    {
                        "ticker": ticker,
                        "cell": f"{MODEL_TAB}!{letter}{sheet_row_number}",
                        "sheet_value": _shown(found),
                        "engine_value": str(engine_value),
                    }
                )
        if len(edited) + len(mismatches) == findings_before:
            matching += 1

    holdings = {row["ticker"] for row in grid["rows"]}
    return {
        "rows_checked": len(grid["rows"]),
        "rows_matching": matching,
        "edited_input_count": len(edited),
        "edited_inputs": edited[:MAX_LISTED],
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:MAX_LISTED],
        "unexpected_tickers": sorted(ticker for ticker in by_ticker if ticker not in holdings)[:MAX_LISTED],
    }


# Number formats of the model tab: column index -> (type, pattern).
_NUMBER_FORMATS: dict[int, tuple[str, str]] = {
    **{index: ("NUMBER", "#,##0.00") for index in (3, 5, 6, 7, 8, 10)},
    9: ("PERCENT", "0.0%"),
    11: ("NUMBER", "#,##0.0"),
    12: ("NUMBER", "0.0000"),
    13: ("NUMBER", "0.00"),
}
_INPUT_CELL_COLOUR = {"red": 1.0, "green": 0.95, "blue": 0.6}


def format_requests(tab_ids: dict[str, int], holdings: int) -> list[dict[str, Any]]:
    """The look of the two managed tabs: bold headers, a frozen header row, number formats and the two input cells.

    Display only. The values, and what ``read_scenario`` reads back, are the same with or without it.
    It is applied before the values are written, so rows that arrive one at a time already look right.
    """
    control, model = tab_ids[CONTROL_TAB], tab_ids[MODEL_TAB]

    def cells(sheet_id: int, rows: tuple[int, int], columns: tuple[int, int] | None = None) -> dict[str, int]:
        found = {"sheetId": sheet_id, "startRowIndex": rows[0], "endRowIndex": rows[1]}
        if columns is not None:
            found.update({"startColumnIndex": columns[0], "endColumnIndex": columns[1]})
        return found

    bold = {"userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "WRAP", "verticalAlignment": "BOTTOM"}}
    bold_fields = "userEnteredFormat(textFormat.bold,wrapStrategy,verticalAlignment)"
    requests: list[dict[str, Any]] = [
        {"repeatCell": {"range": cells(model, (0, 1)), "cell": bold, "fields": bold_fields}},
        {"repeatCell": {"range": cells(control, (0, 1)), "cell": bold, "fields": bold_fields}},
        {
            "updateSheetProperties": {
                "properties": {"sheetId": model, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": cells(control, (1, 3), (1, 2)),
                "cell": {"userEnteredFormat": {"backgroundColor": _INPUT_CELL_COLOUR, "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        },
    ]
    for column, (kind, pattern) in _NUMBER_FORMATS.items():
        requests.append(
            {
                "repeatCell": {
                    "range": cells(model, (1, 1 + holdings), (column, column + 1)),
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )
    return requests


def resize_requests(tab_ids: dict[str, int]) -> list[dict[str, Any]]:
    """Fit the column widths of the two managed tabs to what was written. Display only."""
    return [
        {"autoResizeDimensions": {"dimensions": {"sheetId": tab_ids[tab], "dimension": "COLUMNS", "startIndex": 0, "endIndex": last}}}
        for tab, last in ((MODEL_TAB, 16), (CONTROL_TAB, 2))
    ]


def _formatted(client: SheetsClient, build: Any) -> bool:
    """Apply display formatting. It never fails a write: False means the Sheet kept its previous look."""
    try:
        client.format(build())
    except (SheetsError, KeyError):
        return False
    return True


def _write_blocks(client: SheetsClient, blocks: list[dict[str, Any]], pace_seconds: float) -> int:
    """Write the blocks in one request or, when paced, the model tab one row at a time. Return the cells written."""
    if pace_seconds <= 0:
        return client.write(blocks)
    cells = 0
    for block in blocks:
        if block["range"].partition("!")[0] != MODEL_TAB:
            cells += client.write([block])
            continue
        for row_number, row in enumerate(block["values"], start=1):
            cells += client.write([{"range": f"{MODEL_TAB}!A{row_number}", "values": [row]}])
            time.sleep(pace_seconds)
    return cells


def write_scenario(
    client: SheetsClient, book_id: str, rate_bps: str, oil_pct: str, *, pace_seconds: float = 0.0
) -> dict[str, Any]:
    """Write the live-formula model for a book and scenario into the Sheet, then check what the Sheet computed.

    Args:
        client: The configured Sheet.
        book_id: A covered book.
        rate_bps: Rate shock in basis points, as decimal text.
        oil_pct: Oil shock in percent, as decimal text.
        pace_seconds: When above zero, the model tab is written one row at a time with this pause
            after each row, so that someone watching the Sheet sees it fill in. The cells written
            are the same; it costs one request per row (the Sheets API allows 60 write requests a
            minute for one user).

    Raises:
        ValueError: Unknown book.
        ArithmeticError: A shock that is not a number.
        SheetsError: The Sheet could not be read or written.
    """
    analysis = fsi_engine.analyze_book(book_id, rate_bps=rate_bps, oil_pct=oil_pct)
    described = client.describe()
    missing = [tab for tab in MANAGED_TABS if tab not in described["tabs"]]
    if missing:
        client.add_tabs(missing)
        described = client.describe()
    client.clear(list(MANAGED_TABS))

    payload = model_exporter.build_google_sheets_batch_payload(analysis)
    book_block = {
        "range": f"{CONTROL_TAB}!A4:B7",
        "values": [
            ["Book", book_id],
            ["Book label", analysis["book_label"]],
            # The apostrophe keeps the timestamp as text; Sheets does not display it.
            ["Filing data as of (UTC)", "'" + str(analysis["governance"]["as_of_utc"])],
            ["Note", "Change B2 and B3 only. The agent rewrites both of its tabs each time it writes the model."],
        ],
    }
    # The control tab first, then the model, so the formulas never point at an empty scenario.
    model_blocks = [block for block in payload["data"] if block["range"].partition("!")[0] == MODEL_TAB]
    data = [*(block for block in payload["data"] if block not in model_blocks), book_block, *model_blocks]

    tab_ids = described.get("tab_ids") or {}
    styled = _formatted(client, lambda: format_requests(tab_ids, len(analysis["holdings"])))
    cells_written = _write_blocks(client, data, pace_seconds)
    formatted = _formatted(client, lambda: resize_requests(tab_ids)) and styled
    model_rows = client.read([f"{MODEL_TAB}!A1:P{MAX_MODEL_ROWS}"])[0]
    formula_cells = sum(
        1 for block in data for row in block["values"] for value in row if isinstance(value, str) and value.startswith("=")
    )
    return {
        "sheet_url": described["url"],
        "model_tab_url": tab_url(described),
        "sheet_title": described["title"],
        "book_id": book_id,
        "scenario": {"rate_bps": str(analysis["scenario"]["rate_bps"]), "oil_pct": str(analysis["scenario"]["oil_pct"])},
        "tabs_written": list(MANAGED_TABS),
        "cells_written": cells_written,
        "holdings": len(analysis["holdings"]),
        "formula_cells": formula_cells,
        "scenario_cells": {"rate_bps": RATE_CELL, "oil_pct": OIL_CELL},
        "formatted": formatted,
        "reconciliation": reconcile(model_rows, analysis),
    }


def _scenario_number(rows: list[list[Any]], row_index: int, cell_name: str) -> str:
    value = _cell(rows[row_index], 1) if row_index < len(rows) else ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SheetsError("invalid_sheet_scenario", f"{cell_name} holds {_shown(value)}, which is not a number.")
    return _decimal_text(Decimal(repr(value)))


def read_scenario(client: SheetsClient, book_id: str | None = None) -> dict[str, Any]:
    """Read the scenario the Sheet holds now and compare the Sheet with the engine for that scenario.

    Args:
        client: The configured Sheet.
        book_id: Overrides the book recorded in the Sheet.

    Raises:
        ValueError: Unknown book.
        SheetsError: The Sheet could not be read, a scenario cell is not a number
            (``invalid_sheet_scenario``), or the Sheet does not say which book it holds
            (``sheet_has_no_model``).
    """
    described = client.describe()
    if any(tab not in described["tabs"] for tab in MANAGED_TABS):
        raise SheetsError("sheet_has_no_model", "The Sheet does not hold the scenario model yet. Write the model into it first.")
    control_rows, model_rows = client.read([f"{CONTROL_TAB}!A1:B7", f"{MODEL_TAB}!A1:P{MAX_MODEL_ROWS}"])
    rate_bps = _scenario_number(control_rows, 1, RATE_CELL)
    oil_pct = _scenario_number(control_rows, 2, OIL_CELL)
    recorded = str(_cell(control_rows[3], 1)).strip() if len(control_rows) > 3 else ""
    book = (book_id or "").strip() or recorded
    if not book:
        raise SheetsError(
            "sheet_has_no_model", f"{BOOK_CELL} does not name a book. Write the model into the Sheet first, or name the book."
        )
    analysis = fsi_engine.analyze_book(book, rate_bps=rate_bps, oil_pct=oil_pct)
    return {
        "sheet_url": described["url"],
        "sheet_title": described["title"],
        "book_id": book,
        "scenario": {"rate_bps": rate_bps, "oil_pct": oil_pct},
        "scenario_cells": {"rate_bps": RATE_CELL, "oil_pct": OIL_CELL},
        "reconciliation": reconcile(model_rows, analysis),
        "figures": "Not returned here. Call run_portfolio_shock_analysis with this book and these two values.",
    }
