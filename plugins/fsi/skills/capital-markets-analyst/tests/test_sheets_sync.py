"""The Google Sheet link: what is written, what is read back, and how the Sheet is checked against the engine.

No network. The spreadsheet is the in-memory one of ``conftest.py`` and the Google client
gets a stand-in HTTPS session. That Google Sheets itself computes the engine's figures from
these formulas is a separate, live check (see "Google Sheet" in the README).
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import eval_runner, fsi_engine, model_exporter, server, sheets_sync, tools  # noqa: E402

CONTROL, MODEL = sheets_sync.CONTROL_TAB, sheets_sync.MODEL_TAB
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AB"
BOOK = "next27_flagship"


def test_write_replaces_the_two_managed_tabs_with_the_live_model_and_reconciles(fake_sheet: Any) -> None:
    fake_sheet.set("Sheet1", "A1", "the analyst's own notes")
    result = sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")

    control, model = fake_sheet.tabs[CONTROL], fake_sheet.tabs[MODEL]
    assert control[1][1] == 50.0 and control[2][1] == 25.0
    assert control[3] == ["Book", BOOK]
    # The timestamp is sent as text (leading apostrophe), so the Sheet does not reformat it.
    assert control[5][1] == fsi_engine.load_sample_book()["metadata"]["as_of_utc"]
    assert fake_sheet.tabs["Sheet1"] == [["the analyst's own notes"]]

    assert len(model) == 13 and model[0][0] == "Ticker"
    united = model[fake_sheet.row_of("UAL") - 1]
    assert united[6].startswith("=IF(") and "Scenario_Control!$B$2" in united[6] and "Scenario_Control!$B$3" in united[7]

    analysis = fsi_engine.analyze_book(BOOK, rate_bps="50", oil_pct="25")
    payload = model_exporter.build_google_sheets_batch_payload(analysis)
    assert result["cells_written"] == sum(len(row) for block in payload["data"] for row in block["values"]) + 8
    assert result["formula_cells"] == 12 * 6 and result["holdings"] == 12
    assert result["sheet_url"] == fake_sheet.url and result["tabs_written"] == [CONTROL, MODEL]
    assert {key: Decimal(value) for key, value in result["scenario"].items()} == {"rate_bps": 50, "oil_pct": 25}
    assert result["scenario_cells"] == {"rate_bps": "Scenario_Control!B2", "oil_pct": "Scenario_Control!B3"}
    reconciliation = result["reconciliation"]
    assert reconciliation["rows_checked"] == reconciliation["rows_matching"] == 12
    assert reconciliation["edited_input_count"] == reconciliation["mismatch_count"] == 0
    # No impact figure leaves through this function.
    assert "portfolio_totals" not in result and "contagion_graph" not in result
    # Tabs are added and cleared before anything is written, and the model is read back last.
    # The look of the cells is set before the values arrive and the column widths after them.
    assert [call.split(" ")[0] for call in fake_sheet.calls] == [
        "describe", "add_tabs", "describe", "clear", "format", "write", "format", "read",
    ]  # fmt: skip
    assert result["model_tab_url"] == f"{fake_sheet.url}#gid={fake_sheet.describe()['tab_ids'][MODEL]}"


def test_a_paced_write_sends_the_model_one_row_at_a_time_and_writes_the_same_cells(fake_sheet: Any) -> None:
    at_once = sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    cells_at_once = {tab: [list(row) for row in rows] for tab, rows in fake_sheet.tabs.items()}
    fake_sheet.calls.clear()

    paced = sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25", pace_seconds=0.001)

    writes = [call for call in fake_sheet.calls if call.startswith("write")]
    # Two blocks for the control tab, then the header and the twelve holdings, one request each.
    assert len(writes) == 2 + 13
    assert writes[0].startswith(f"write ['{CONTROL}!") and writes[1].startswith(f"write ['{CONTROL}!")
    assert writes[2] == f"write ['{MODEL}!A1']" and writes[-1] == f"write ['{MODEL}!A13']"
    assert fake_sheet.tabs == cells_at_once
    assert paced["cells_written"] == at_once["cells_written"]
    assert paced["reconciliation"]["rows_matching"] == 12


def test_row_pace_setting_is_read_from_the_environment_and_kept_between_zero_and_one(
    fake_sheet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sheets_sync.ROW_PACE_ENV, raising=False)
    assert sheets_sync.row_pace_seconds() == 0.0 and sheets_sync.row_pace_seconds(default=0.15) == 0.15
    for raw, expected in (("0.2", 0.2), ("0", 0.0), ("5", 1.0), ("-1", 0.0), ("fast", 0.0), ("", 0.0)):
        monkeypatch.setenv(sheets_sync.ROW_PACE_ENV, raw)
        assert sheets_sync.row_pace_seconds() == expected, raw

    # The chat tool writes in one request unless the setting asks for rows.
    monkeypatch.delenv(sheets_sync.ROW_PACE_ENV)
    tools.call_tool("write_scenario_to_sheet", {"book_id": BOOK, "rate_bps": "50", "oil_pct": "25"})
    assert len([call for call in fake_sheet.calls if call.startswith("write")]) == 1
    fake_sheet.calls.clear()
    monkeypatch.setenv(sheets_sync.ROW_PACE_ENV, "0.001")
    tools.call_tool("write_scenario_to_sheet", {"book_id": BOOK, "rate_bps": "50", "oil_pct": "25"})
    assert len([call for call in fake_sheet.calls if call.startswith("write")]) == 2 + 13


def test_the_tab_link_opens_the_model_tab_when_the_sheet_has_one() -> None:
    described = {"url": "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing#gid=0", "tab_ids": {MODEL: 77}}
    assert sheets_sync.tab_url(described) == "https://docs.google.com/spreadsheets/d/abc/edit#gid=77"
    assert sheets_sync.tab_url({"url": "https://docs.google.com/spreadsheets/d/abc/edit", "tab_ids": {}}) == (
        "https://docs.google.com/spreadsheets/d/abc/edit"
    )


def test_formatting_is_display_only_and_never_fails_a_write(fake_sheet: Any) -> None:
    result = sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    assert result["formatted"] is True
    ids = fake_sheet.describe()["tab_ids"]
    kinds = [next(iter(request)) for request in fake_sheet.formatting]
    assert kinds.count("updateSheetProperties") == 1 and kinds.count("autoResizeDimensions") == 2
    # Every request names one of the two managed tabs, and none of them carries a value.
    managed = {ids[CONTROL], ids[MODEL]}
    for request in fake_sheet.formatting:
        body = next(iter(request.values()))
        target = body.get("range") or body.get("dimensions") or body.get("properties")
        assert target["sheetId"] in managed
        assert "userEnteredValue" not in json.dumps(request)
    # The two cells the user changes are the highlighted ones: Scenario_Control!B2:B3.
    highlighted = [r["repeatCell"]["range"] for r in fake_sheet.formatting if "backgroundColor" in json.dumps(r)]
    assert highlighted == [
        {"sheetId": ids[CONTROL], "startRowIndex": 1, "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 2}
    ]
    # Number formats stop at the last holding.
    formats = [r["repeatCell"]["range"] for r in fake_sheet.formatting if "numberFormat" in json.dumps(r)]
    assert formats and all(found["startRowIndex"] == 1 and found["endRowIndex"] == 13 for found in formats)

    fake_sheet.refuse_formatting = True
    refused = sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    assert refused["formatted"] is False and refused["reconciliation"]["rows_matching"] == 12


def test_a_second_write_leaves_no_row_of_the_previous_book(fake_sheet: Any) -> None:
    sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    calls_before = len(fake_sheet.calls)
    result = sheets_sync.write_scenario(fake_sheet, "megacap_tech", "25", "0")

    tickers = {row[0] for row in fake_sheet.tabs[MODEL][1:]}
    assert tickers == set(fsi_engine.load_sample_book()["books"]["megacap_tech"]["tickers"]) and "UAL" not in tickers
    assert result["reconciliation"]["rows_checked"] == result["reconciliation"]["rows_matching"] == len(tickers)
    assert fake_sheet.tabs[CONTROL][3] == ["Book", "megacap_tech"]
    # The tabs existed the second time, so none was added.
    assert not any(call.startswith("add_tabs") for call in fake_sheet.calls[calls_before:])


def test_read_returns_the_scenario_the_user_typed_and_no_figures(fake_sheet: Any) -> None:
    sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    fake_sheet.set(CONTROL, "B2", -12.5)
    fake_sheet.set(CONTROL, "B3", 40)

    result = sheets_sync.read_scenario(fake_sheet)

    assert result["book_id"] == BOOK and result["scenario"] == {"rate_bps": "-12.5", "oil_pct": "40"}
    assert result["reconciliation"]["rows_checked"] == result["reconciliation"]["rows_matching"] == 12
    assert set(result) == {"sheet_url", "sheet_title", "book_id", "scenario", "scenario_cells", "reconciliation", "figures"}
    assert "run_portfolio_shock_analysis" in result["figures"]

    # A whole number that the spreadsheet returns as a float reads as a whole number.
    fake_sheet.set(CONTROL, "B2", 50.0)
    assert sheets_sync.read_scenario(fake_sheet)["scenario"]["rate_bps"] == "50"

    # The book can be named instead; rows that are not its holdings are then reported.
    other = sheets_sync.read_scenario(fake_sheet, "megacap_tech")
    assert other["book_id"] == "megacap_tech" and "UAL" in other["reconciliation"]["unexpected_tickers"]


def test_an_edited_input_a_wrong_result_and_a_missing_row_are_reported(fake_sheet: Any) -> None:
    sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    row = fake_sheet.row_of("UAL")
    filing_value = fake_sheet.tabs[MODEL][row - 1][5]

    fake_sheet.set(MODEL, f"F{row}", -1.0)
    reconciliation = sheets_sync.read_scenario(fake_sheet)["reconciliation"]
    assert reconciliation["rows_matching"] == 11 and reconciliation["edited_input_count"] == 1
    edited = reconciliation["edited_inputs"][0]
    assert edited["ticker"] == "UAL" and edited["cell"] == f"Portfolio_Shock_Model!F{row}" and edited["sheet_value"] == "-1"
    assert Decimal(edited["filing_value"]) == Decimal(str(filing_value))

    fake_sheet.set(MODEL, f"F{row}", filing_value)
    analysis = fsi_engine.analyze_book(BOOK, rate_bps="50", oil_pct="25")
    rows = fake_sheet.read([MODEL])[0]
    assert sheets_sync.reconcile(rows, analysis)["rows_matching"] == 12

    # Half a cent on a USD-millions cell is the engine's rounding, not a mismatch.
    close = [list(cells) for cells in rows]
    close[row - 1][8] += 0.004
    assert sheets_sync.reconcile(close, analysis)["mismatch_count"] == 0

    wrong = [list(cells) for cells in rows]
    wrong[row - 1][12] += 0.01
    wrong[row - 1][14] = "#ERROR!"
    wrong = [cells for cells in wrong if cells[0] != "DAL"] + [["ZZZ", "Not in the book"]]
    reconciliation = sheets_sync.reconcile(wrong, analysis)
    assert reconciliation["rows_checked"] == 12 and reconciliation["rows_matching"] == 10
    found = {(item["ticker"], item["cell"].rpartition("!")[2][:1]): item for item in reconciliation["mismatches"]}
    assert found[("UAL", "O")]["sheet_value"] == "#ERROR!" and ("UAL", "M") in found
    # A number the spreadsheet computed is quoted to four decimals, not as a long float.
    assert found[("UAL", "M")]["sheet_value"] == "-6.9463" and found[("UAL", "M")]["engine_value"] == "-6.9563"
    assert found[("DAL", "P")] == {
        "ticker": "DAL",
        "cell": "Portfolio_Shock_Model",
        "sheet_value": "(row missing)",
        "engine_value": "holding of the book",
    }
    assert reconciliation["mismatch_count"] == 3 and reconciliation["unexpected_tickers"] == ["ZZZ"]


def test_a_sheet_without_the_model_or_with_a_scenario_that_is_not_a_number_is_refused(fake_sheet: Any) -> None:
    with pytest.raises(sheets_sync.SheetsError) as empty:
        sheets_sync.read_scenario(fake_sheet)
    assert empty.value.code == "sheet_has_no_model"

    sheets_sync.write_scenario(fake_sheet, BOOK, "50", "25")
    # Text is refused even when it looks like a number: the cell has to hold a number.
    for typed in ("abc", "", True, "50"):
        fake_sheet.set(CONTROL, "B2", typed)
        with pytest.raises(sheets_sync.SheetsError) as refused:
            sheets_sync.read_scenario(fake_sheet)
        assert refused.value.code == "invalid_sheet_scenario" and "Scenario_Control!B2" in str(refused.value)

    fake_sheet.set(CONTROL, "B2", 50)
    fake_sheet.set(CONTROL, "B4", "")
    with pytest.raises(sheets_sync.SheetsError) as unnamed:
        sheets_sync.read_scenario(fake_sheet)
    assert unnamed.value.code == "sheet_has_no_model" and "Scenario_Control!B4" in str(unnamed.value)
    assert sheets_sync.read_scenario(fake_sheet, BOOK)["book_id"] == BOOK
    with pytest.raises(ValueError):
        sheets_sync.read_scenario(fake_sheet, "no_such_book")


def test_the_sheet_is_named_by_configuration_only(monkeypatch: pytest.MonkeyPatch) -> None:
    link = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
    assert sheets_sync.sheet_id_from(link) == SHEET_ID and sheets_sync.sheet_id_from(f" {SHEET_ID} ") == SHEET_ID
    assert sheets_sync.sheet_id_from("my sheet") is None and sheets_sync.sheet_id_from("") is None

    monkeypatch.delenv(sheets_sync.SHEET_ID_ENV, raising=False)
    monkeypatch.delenv(sheets_sync.SERVICE_ACCOUNT_ENV, raising=False)
    for configured in (None, "my sheet"):
        if configured is not None:
            monkeypatch.setenv(sheets_sync.SHEET_ID_ENV, configured)
        with pytest.raises(sheets_sync.SheetsError) as refused:
            sheets_sync.client_from_env()
        assert refused.value.code == "sheet_not_configured" and sheets_sync.SHEET_ID_ENV in str(refused.value)

    monkeypatch.setenv(sheets_sync.SHEET_ID_ENV, link)
    client = sheets_sync.client_from_env()
    assert isinstance(client, sheets_sync.GoogleSheetsClient)
    assert client.spreadsheet_id == SHEET_ID and client.service_account is None
    monkeypatch.setenv(sheets_sync.SERVICE_ACCOUNT_ENV, "sheets@example-project.iam.gserviceaccount.com")
    acting_as = sheets_sync.client_from_env()
    assert acting_as.service_account == "sheets@example-project.iam.gserviceaccount.com" and acting_as is not client
    # The same Sheet and identity give the same client, so a token and a connection are reused between calls.
    assert sheets_sync.client_from_env() is acting_as
    assert sheets_sync.client_for(link, " sheets@example-project.iam.gserviceaccount.com ") is acting_as


class Reply:
    """Stand-in for an HTTP response."""

    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class StubSession:
    """Stand-in for the authorised HTTPS session: replays replies and records the requests."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_google_client_sends_the_expected_requests_and_names_each_failure() -> None:
    session = StubSession(
        Reply(200, {"spreadsheetUrl": "https://sheet", "properties": {"title": "Desk", "locale": "en_US"}, "sheets": [{"properties": {"sheetId": 7, "title": "Sheet1"}}]}),
        Reply(200, {}),
        Reply(200, {}),
        Reply(200, {"totalUpdatedCells": 222}),
        Reply(200, {"valueRanges": [{"range": "A", "values": [[1, 2]]}, {"range": "B"}]}),
    )
    account = "sheets@example-project.iam.gserviceaccount.com"
    client = sheets_sync.GoogleSheetsClient(SHEET_ID, service_account=account, session=session)

    assert client.describe() == {
        "url": "https://sheet",
        "title": "Desk",
        "locale": "en_US",
        "tabs": ["Sheet1"],
        "tab_ids": {"Sheet1": 7},
    }
    client.add_tabs([CONTROL, MODEL])
    client.clear([CONTROL, MODEL])
    assert client.write([{"range": f"{CONTROL}!A1", "values": [["a"]]}]) == 222
    assert client.read([f"{CONTROL}!A1:B7", f"{MODEL}!A1:P200"]) == [[[1, 2]], []]

    root = f"{sheets_sync.API_ROOT}/{SHEET_ID}"
    assert [(method, url) for method, url, _ in session.requests] == [
        ("GET", root),
        ("POST", root + ":batchUpdate"),
        ("POST", root + "/values:batchClear"),
        ("POST", root + "/values:batchUpdate"),
        ("GET", root + "/values:batchGet"),
    ]
    assert all(kwargs["timeout"] == sheets_sync.TIMEOUT_SECONDS for _, _, kwargs in session.requests)
    assert session.requests[1][2]["json"] == {
        "requests": [{"addSheet": {"properties": {"title": CONTROL}}}, {"addSheet": {"properties": {"title": MODEL}}}]
    }
    assert session.requests[2][2]["json"] == {"ranges": [CONTROL, MODEL]}
    # Values are written as typed by a user, so formulas are live; they are read back unformatted.
    assert session.requests[3][2]["json"]["valueInputOption"] == "USER_ENTERED"
    assert session.requests[4][2]["params"] == {
        "ranges": [f"{CONTROL}!A1:B7", f"{MODEL}!A1:P200"],
        "valueRenderOption": "UNFORMATTED_VALUE",
    }

    failures = (
        (Reply(403, {"error": {"message": "The caller does not have permission"}}), "sheet_permission_denied"),
        (Reply(404, {"error": {"message": "Requested entity was not found."}}), "sheet_not_found"),
        (Reply(500, "<html>upstream error</html>"), "sheets_api_error"),
        (ConnectionError("no route to host"), "sheets_unreachable"),
    )
    for reply, code in failures:
        failing = sheets_sync.GoogleSheetsClient(SHEET_ID, service_account=account, session=StubSession(reply))
        with pytest.raises(sheets_sync.SheetsError) as raised:
            failing.describe()
        assert raised.value.code == code
        if code in ("sheet_permission_denied", "sheet_not_found", "sheets_unreachable"):
            # The message names the identity the Sheet has to be shared with.
            assert account in str(raised.value)


def test_sheet_tools_return_structured_results_and_errors(fake_sheet: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    written = tools.call_tool("write_scenario_to_sheet", {"book_id": BOOK, "rate_bps": "50", "oil_pct": "25"})
    assert written["status"] == "ok" and written["reconciliation"]["rows_matching"] == 12

    fake_sheet.set(CONTROL, "B3", 40)
    read = tools.call_tool("read_scenario_from_sheet", {})
    assert read["status"] == "ok" and read["scenario"] == {"rate_bps": "50", "oil_pct": "40"}
    # The figures for the scenario read from the Sheet come from the engine, through the usual tool.
    analysis = tools.call_tool("run_portfolio_shock_analysis", {"book_id": read["book_id"], **read["scenario"]})
    united = next(holding for holding in analysis["holdings"] if holding["ticker"] == "UAL")
    assert Decimal(united["scaled_impact"]["eps_delta"]) < Decimal("-6.9563")

    assert tools.call_tool("write_scenario_to_sheet", {"book_id": BOOK, "rate_bps": "abc"})["error_code"] == "invalid_input"
    assert tools.call_tool("write_scenario_to_sheet", {"book_id": "nope"})["error_code"] == "unknown_book"
    fake_sheet.set(CONTROL, "B2", "soon")
    assert tools.call_tool("read_scenario_from_sheet", {})["error_code"] == "invalid_sheet_scenario"

    def refuse() -> Any:
        raise sheets_sync.SheetsError("sheet_permission_denied", "Share the Sheet with the service account as Editor.")

    monkeypatch.setattr(sheets_sync, "client_from_env", refuse)
    for name, args in (("write_scenario_to_sheet", {"book_id": BOOK}), ("read_scenario_from_sheet", {})):
        assert tools.call_tool(name, args) == {
            "status": "error",
            "error_code": "sheet_permission_denied",
            "message": "Share the Sheet with the service account as Editor.",
        }


def test_workstation_writes_and_reads_its_one_sheet_and_takes_no_sheet_from_the_browser(
    fake_sheet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_SHEET_SETTINGS", (SHEET_ID, ""))
    monkeypatch.setattr(server, "_sheet_client", lambda: fake_sheet)
    monkeypatch.setattr(server, "SHEET_ROW_PACE_SECONDS", 0.001)

    status = server.handle_sheet_status()
    assert status == {
        "configured": True,
        "reachable": True,
        "title": fake_sheet.title,
        "sheet_url": fake_sheet.url,
        "model_tab_url": fake_sheet.url,
        "has_model": False,
    }

    # A Sheet named by the browser is ignored: the write goes to the workstation's own Sheet.
    written = server.handle_sheet_write(
        {"book_id": BOOK, "rate_bps": "50", "oil_pct": "25", "sheet_id": "another-sheet", "spreadsheet_id": "another-sheet"}
    )
    assert written["sheet_url"] == fake_sheet.url and written["reconciliation"]["rows_matching"] == 12
    assert len([call for call in fake_sheet.calls if call.startswith("write")]) == 2 + 13
    assert "holdings_detail" not in written and "portfolio_totals" not in written
    assert server.handle_sheet_status()["has_model"] is True

    # A scenario preset is resolved by the engine before it is written.
    preset = server.handle_sheet_write({"book_id": BOOK, "scenario_id": "fed_up_50_oil_spike"})
    assert {key: Decimal(value) for key, value in preset["scenario"].items()} == {"rate_bps": 50, "oil_pct": 25}

    fake_sheet.set(CONTROL, "B3", 40)
    read = server.handle_sheet_read()
    assert read["sheet"]["scenario"] == {"rate_bps": "50", "oil_pct": "40"}
    assert read["sheet"]["reconciliation"]["rows_matching"] == 12
    # The figures the page then shows are the engine's figures for the scenario read from the Sheet.
    expected = fsi_engine.analyze_book(BOOK, rate_bps="50", oil_pct="40")
    assert read["analysis"]["portfolio_totals"] == expected["portfolio_totals"]
    assert set(read) == {"sheet", "analysis", "formula_grid", "watcher"}

    fake_sheet.set(CONTROL, "B2", "soon")
    with pytest.raises(sheets_sync.SheetsError) as refused:
        server.handle_sheet_read()
    assert refused.value.code == "invalid_sheet_scenario"


def test_workstation_without_a_sheet_reports_it_and_keeps_the_fifth_tab_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_SHEET_SETTINGS", ("", ""))
    assert server.handle_sheet_status() == {
        "configured": False,
        "reachable": False,
        "message": "No Google Sheet is configured.",
    }
    for call in (lambda: server.handle_sheet_write({"book_id": BOOK}), server.handle_sheet_read):
        with pytest.raises(sheets_sync.SheetsError) as refused:
            call()
        assert refused.value.code == "sheet_not_configured"

    def unreachable() -> Any:
        raise sheets_sync.SheetsError("sheet_permission_denied", "Share the Sheet with the service account as Editor.")

    monkeypatch.setattr(server, "_SHEET_SETTINGS", (SHEET_ID, ""))
    monkeypatch.setattr(server, "_sheet_client", unreachable)
    assert server.handle_sheet_status() == {
        "configured": True,
        "reachable": False,
        "error_code": "sheet_permission_denied",
        "message": "Share the Sheet with the service account as Editor.",
    }

    ui = SKILL_ROOT / "assets" / "ui"
    page = (ui / "index.html").read_text(encoding="utf-8")
    script = (ui / "app.js").read_text(encoding="utf-8")
    # The tab is in the page but hidden; the script shows it only when the server reports a Sheet.
    assert '<button id="tab-btn-gsheet" class="tab-btn" data-tab="gsheet" hidden>' in page
    assert 'id="panel-gsheet"' in page and 'id="gsheet-frame"' in page
    assert 'if (!status.configured) return;' in script
    assert script.index('if (!status.configured) return;') < script.index('getElementById("tab-btn-gsheet").hidden = false')
    for endpoint in ('"/api/sheet"', '"/api/sheet/write"', '"/api/sheet/read"'):
        assert endpoint in script
    assert ".tab-btn[hidden]" in (ui / "styles.css").read_text(encoding="utf-8")


def test_without_a_configured_sheet_the_tools_say_so_and_an_evaluation_never_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sheets_sync.SHEET_ID_ENV, raising=False)
    for name in ("write_scenario_to_sheet", "read_scenario_from_sheet"):
        refused = tools.call_tool(name, {})
        assert refused["status"] == "error" and refused["error_code"] == "sheet_not_configured"
        assert sheets_sync.SHEET_ID_ENV in refused["message"]

    # A deployment with a Sheet still evaluates without one, so an evaluation cannot write to it.
    monkeypatch.setenv(sheets_sync.SHEET_ID_ENV, SHEET_ID)
    with eval_runner.bundled_data_only():
        assert sheets_sync.SHEET_ID_ENV not in os.environ
        assert tools.call_tool("write_scenario_to_sheet", {})["error_code"] == "sheet_not_configured"
    assert os.environ[sheets_sync.SHEET_ID_ENV] == SHEET_ID


def test_the_sheet_says_which_tax_rate_each_row_uses(fake_sheet: Any) -> None:
    """The Sheet showed 0.21 for one holding with no word on why; its last column now names the basis of column J."""
    analysis = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="25")
    payload = model_exporter.build_google_sheets_batch_payload(analysis)
    header, *rows = next(block["values"] for block in payload["data"] if block["range"].startswith("Portfolio_Shock_Model"))
    assert header[-1] == "Tax Rate Basis" and len(header) == 17 and all(len(row) == 17 for row in rows)
    basis = {row[0]: (row[9], row[-1]) for row in rows}
    assert basis["AAPL"] == (0.17, "XBRL effective tax rate")
    assert basis["INTC"][0] == 0.21
    assert basis["INTC"][1].startswith("Tax: after-tax impact and EPS use the US federal statutory rate of 21%")
    # The extra column is text beside the model: what is read back and reconciled is still columns A to P.
    written = sheets_sync.write_scenario(fake_sheet, "megacap_tech", "50", "25")
    assert fake_sheet.tabs[MODEL][5][16].startswith("Tax: after-tax impact and EPS use the US federal statutory rate")
    assert written["reconciliation"]["mismatch_count"] == 0 and written["reconciliation"]["rows_matching"] == 5
