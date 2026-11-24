"""Shared test doubles for the skill's tests."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine, model_exporter, sheets_sync  # noqa: E402

_RANGE_RE = re.compile(r"^(?P<tab>[^!]+)(?:!(?P<column>[A-Z]+)(?P<row>\d+)(?::[A-Z]+\d*)?)?$")
# Computed columns of the model tab: index in a row -> key of the engine's evaluated row.
_COMPUTED = {6: "rate_scaled_m", 7: "oil_scaled_m", 8: "total_pre_tax_m", 10: "after_tax_m", 12: "eps_delta"}
_GUARDRAIL_INDEX = 14


def _column_index(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - 64)
    return index - 1


class FakeSheet:
    """An in-memory spreadsheet with the operations of ``sheets_sync.SheetsClient``.

    Formulas are not evaluated. When the model tab is read, its computed columns are filled
    from the Python engine for the book and scenario the control tab holds, which is what a
    spreadsheet that computes the formulas correctly shows. An edited input therefore does
    not change the computed columns here, as it would in a real spreadsheet.
    """

    def __init__(self, tabs: list[str] | None = None) -> None:
        self.url = "https://docs.google.com/spreadsheets/d/FAKE-SHEET-ID-0000000000000/edit"
        self.title = "Desk scenario sheet"
        self.tabs: dict[str, list[list[Any]]] = {title: [] for title in (tabs or ["Sheet1"])}
        self.calls: list[str] = []
        self.formatting: list[dict[str, Any]] = []
        self.refuse_formatting = False

    # ------------------------------------------------------------------ SheetsClient

    def describe(self) -> dict[str, Any]:
        self.calls.append("describe")
        tab_ids = {title: 100 + index for index, title in enumerate(self.tabs)}
        return {"url": self.url, "title": self.title, "locale": "en_US", "tabs": list(self.tabs), "tab_ids": tab_ids}

    def add_tabs(self, titles: list[str]) -> None:
        self.calls.append(f"add_tabs {titles}")
        for title in titles:
            self.tabs[title] = []

    def clear(self, ranges: list[str]) -> None:
        self.calls.append(f"clear {ranges}")
        for name in ranges:
            self.tabs[_RANGE_RE.match(name)["tab"]] = []

    def write(self, data: list[dict[str, Any]]) -> int:
        self.calls.append(f"write {[block['range'] for block in data]}")
        cells = 0
        for block in data:
            parsed = _RANGE_RE.match(block["range"])
            first_row = int(parsed["row"] or 1) - 1
            first_column = _column_index(parsed["column"] or "A")
            for row_offset, values in enumerate(block["values"]):
                for column_offset, value in enumerate(values):
                    self._put(parsed["tab"], first_row + row_offset, first_column + column_offset, value)
                    cells += 1
        return cells

    def read(self, ranges: list[str]) -> list[list[list[Any]]]:
        self.calls.append(f"read {ranges}")
        found = []
        for name in ranges:
            tab = _RANGE_RE.match(name)["tab"]
            rows = self._computed_model_rows() if tab == sheets_sync.MODEL_TAB else self.tabs[tab]
            found.append(self._as_returned(rows))
        return found

    def format(self, requests: list[dict[str, Any]]) -> None:
        self.calls.append(f"format {len(requests)} requests")
        if self.refuse_formatting:
            raise sheets_sync.SheetsError("sheets_api_error", "formatting refused")
        self.formatting.extend(requests)

    # ------------------------------------------------------------------ helpers for tests

    def set(self, tab: str, cell: str, value: Any) -> None:
        """Type a value into one cell, as the analyst would."""
        parsed = re.match(r"^([A-Z]+)(\d+)$", cell)
        self._put(tab, int(parsed[2]) - 1, _column_index(parsed[1]), value)

    def row_of(self, ticker: str) -> int:
        """The 1-based row of a holding on the model tab."""
        rows = self.tabs[sheets_sync.MODEL_TAB]
        return next(index for index, row in enumerate(rows, start=1) if row and row[0] == ticker)

    def _put(self, tab: str, row: int, column: int, value: Any) -> None:
        rows = self.tabs[tab]
        while len(rows) <= row:
            rows.append([])
        while len(rows[row]) <= column:
            rows[row].append("")
        # A leading apostrophe marks text and is not part of the stored value.
        rows[row][column] = value[1:] if isinstance(value, str) and value.startswith("'") else value

    def _computed_model_rows(self) -> list[list[Any]]:
        rows = [list(row) for row in self.tabs[sheets_sync.MODEL_TAB]]
        control = self.tabs.get(sheets_sync.CONTROL_TAB) or []
        try:
            rate, oil, book = control[1][1], control[2][1], control[3][1]
            analysis = fsi_engine.analyze_book(book, rate_bps=str(rate), oil_pct=str(oil))
        except (IndexError, ValueError, ArithmeticError):
            return rows
        evaluated = {row["ticker"]: row["evaluated"] for row in model_exporter.build_formula_grid(analysis)["rows"]}
        for row in rows[1:]:
            engine = evaluated.get(row[0] if row else "")
            if engine is None:
                continue
            for index, key in _COMPUTED.items():
                row[index] = float(engine[key])
            row[_GUARDRAIL_INDEX] = engine["guardrail_status"]
        return rows

    @staticmethod
    def _as_returned(rows: list[list[Any]]) -> list[list[Any]]:
        """The API drops empty cells at the end of a row and empty rows at the end of a range."""
        trimmed = []
        for row in rows:
            cells = list(row)
            while cells and cells[-1] in ("", None):
                cells.pop()
            trimmed.append(cells)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return trimmed


@pytest.fixture
def fake_sheet(monkeypatch: pytest.MonkeyPatch) -> FakeSheet:
    """The Sheet tools use an in-memory spreadsheet instead of Google Sheets."""
    sheet = FakeSheet()
    monkeypatch.setattr(sheets_sync, "client_from_env", lambda: sheet)
    return sheet
