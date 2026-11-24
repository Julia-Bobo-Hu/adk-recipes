"""Local web workstation server for `fsi-capital-markets-analyst`.

Serves `assets/ui/` plus JSON and OpenXML `.xlsx` endpoints with the standard library
`ThreadingHTTPServer`. Besides the standard library it needs PyYAML and Pydantic (both
installed with the skill) for the evaluation endpoints. It does not host the ADK agent;
`POST /api/eval` with `mode=live` runs the agent in-process and therefore also needs
`google-adk` and Application Default Credentials.

Control inputs for sign-off are decided here, not by the browser: the packet hash is
recomputed from the scenario, the evaluation evidence is the last run this server
performed, and dual control comes from the environment.

`chat-surfaces.html` previews the two chat surfaces. `/api/mcp_app/*` returns what
`scripts/mcp_server.py` returns (the view document and the tool results), so the page
can stand in for an MCP Apps host; `/api/a2ui` returns the A2UI card messages. The
preview is a stand-in: it is not an MCP Apps host and not an A2UI client.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

# Ensure skill root is importable when invoked as `python3 scripts/server.py`
SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import (  # noqa: E402
    a2ui_cards,
    eval_runner,
    filing_compare,
    fsi_engine,
    model_exporter,
    record_decision,
    ripple_view,
    sheets_reported,
    sheets_sync,
    tools,
)

UI_DIR = SKILL_ROOT / "assets" / "ui"

_EVAL_LOCK = threading.Lock()
_LATEST_EVAL: dict[str, Any] | None = None
_EVAL_JOB: dict[str, Any] = {
    "job_id": "",
    "running": False,
    "completed_cases": 0,
    "total_cases": 0,
    "phase": "idle",
    "error": "",
    "report": None,
}

# The one Google Sheet of this workstation, read once at start. Nothing the browser sends can name
# another Sheet, and an evaluation run, which clears FSI_SHEET_ID for its own length, does not hide it.
_SHEET_SETTINGS: tuple[str, str] = (
    os.environ.get(sheets_sync.SHEET_ID_ENV, "").strip(),
    os.environ.get(sheets_sync.SERVICE_ACCOUNT_ENV, "").strip(),
)
# Pause after each row when the workstation writes the model, so the Sheet is seen filling in.
# FSI_SHEET_ROW_PACE_SECONDS overrides it; 0 writes the model in one request.
SHEET_ROW_PACE_SECONDS = sheets_sync.row_pace_seconds(default=0.15)
# Sheet errors the person at the workstation can act on; the others are failures of the call to Google.
_SHEET_ERRORS_TO_ACT_ON = ("sheet_not_configured", "sheet_has_no_model", "invalid_sheet_scenario")


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean setting from the environment."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def signoff_policy() -> dict[str, bool]:
    """Server-side sign-off controls (never taken from the request body)."""
    return {
        "dual_control": _env_flag("FSI_DUAL_CONTROL", True),
        "require_live_eval": _env_flag("FSI_REQUIRE_LIVE_EVAL", False),
    }


def latest_eval() -> dict[str, Any]:
    """Return the evaluation run that sign-off is bound to, running the offline tier once if needed."""
    global _LATEST_EVAL
    with _EVAL_LOCK:
        if _LATEST_EVAL is None:
            if _env_flag("FSI_PRELOAD_LATEST_LIVE", False):
                dataset = fsi_engine.load_sample_book()
                saved_live = eval_runner.load_latest(eval_runner.MODE_LIVE, dataset=dataset)
                if saved_live is not None:
                    _LATEST_EVAL = saved_live
                    return _LATEST_EVAL
            _LATEST_EVAL = eval_runner.run_eval(mode=eval_runner.MODE_OFFLINE)
        return _LATEST_EVAL


def _analysis_for(body: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any]:
    return fsi_engine.analyze_book(
        str(body.get("book_id", "next27_flagship")),
        rate_bps=str(body.get("rate_bps", "25")),
        oil_pct=str(body.get("oil_pct", "0")),
        scenario_id=body.get("scenario_id") or None,
        dataset=dataset,
    )


def build_bootstrap_payload() -> dict[str, Any]:
    """Return initial books, scenarios, default analysis, watcher, eval scorecard and ledger."""
    dataset = fsi_engine.load_sample_book()
    initial_book = "next27_flagship"
    initial_scenario = "fed_up_50_oil_spike"
    analysis = fsi_engine.analyze_book(initial_book, scenario_id=initial_scenario, dataset=dataset)

    return {
        "metadata": dataset["metadata"],
        "books": [
            {"id": bid, "label": b["label"], "tickers": b["tickers"]}
            for bid, b in dataset["books"].items()
        ],
        "scenarios": dataset["scenarios"],
        "analysis": analysis,
        "formula_grid": model_exporter.build_formula_grid(analysis),
        "watcher": filing_compare.evaluate_book_deltas(initial_book, dataset=dataset),
        "eval_report": latest_eval(),
        "last_live_report": eval_runner.load_latest(eval_runner.MODE_LIVE, dataset=dataset),
        "signoff_policy": signoff_policy(),
        "ledger": record_decision.read_ledger_entries(),
    }


def handle_analyze_request(body: dict[str, Any]) -> dict[str, Any]:
    """Recompute the analysis, formula grid and watcher for a book and scenario."""
    dataset = fsi_engine.load_sample_book()
    analysis = _analysis_for(body, dataset)
    return {
        "analysis": analysis,
        "formula_grid": model_exporter.build_formula_grid(analysis),
        "watcher": filing_compare.evaluate_book_deltas(str(body.get("book_id", "next27_flagship")), dataset=dataset),
    }


def _sheet_client() -> sheets_sync.SheetsClient:
    """The client for the workstation's Sheet; raises ``SheetsError`` when none is configured."""
    return sheets_sync.client_for(*_SHEET_SETTINGS)


def handle_sheet_status() -> dict[str, Any]:
    """Whether this workstation has a Google Sheet, whether it can be reached, and where it is."""
    if not _SHEET_SETTINGS[0]:
        return {"configured": False, "reachable": False, "message": "No Google Sheet is configured."}
    try:
        described = _sheet_client().describe()
    except sheets_sync.SheetsError as exc:
        return {"configured": True, "reachable": False, "error_code": exc.code, "message": str(exc)}
    return {
        "configured": True,
        "reachable": True,
        "title": described["title"],
        "sheet_url": described["url"],
        "model_tab_url": sheets_sync.tab_url(described),
        "has_model": all(tab in described["tabs"] for tab in sheets_sync.MANAGED_TABS),
    }


def handle_sheet_write(body: dict[str, Any]) -> dict[str, Any]:
    """Write the scenario on screen into the workstation's Sheet, row by row, and compare the Sheet with the engine.

    The reply holds no impact figures: counts, the Sheet's links and the reconciliation.
    """
    analysis = _analysis_for(body, fsi_engine.load_sample_book())
    return sheets_sync.write_scenario(
        _sheet_client(),
        str(body.get("book_id", "next27_flagship")),
        str(analysis["scenario"]["rate_bps"]),
        str(analysis["scenario"]["oil_pct"]),
        pace_seconds=SHEET_ROW_PACE_SECONDS,
    )


def handle_sheet_read() -> dict[str, Any]:
    """Read the scenario the Sheet holds and return it with the analysis Python computes for it.

    The figures in the reply come from the engine. The Sheet supplies the book and the two
    scenario values, and is compared with the engine (``sheet.reconciliation``).
    """
    read = sheets_sync.read_scenario(_sheet_client())
    computed = handle_analyze_request({"book_id": read["book_id"], **read["scenario"]})
    return {"sheet": read, **computed}


def handle_sheet_reported(body: dict[str, Any]) -> dict[str, Any]:
    """Write the reported figures of the named companies into the workstation's Sheet and compare what the Sheet computed with Python.

    With ``prepare`` set, only the four tabs are made sure of and their links returned, so the
    page can show the tab before the rows arrive. Otherwise ``Reported_Figures`` and
    ``Comparison`` are written one row at a time. The reply holds no figures: counts, the
    Sheet's links, the reconciliation and the companies that could not be returned.
    """
    client = _sheet_client()
    if body.get("prepare"):
        described = sheets_reported.ensure_tabs(client)
        return {"sheet_url": described["url"], **sheets_reported.tab_links(described)}
    raw = body.get("tickers")
    tickers = re.split(r"[\s,;]+", raw) if isinstance(raw, str) else [str(ticker) for ticker in (raw or [])]
    views, not_available = tools.workbook_views(tickers)
    written = sheets_reported.write_reported(client, views, pace_seconds=SHEET_ROW_PACE_SECONDS)
    return {**written, "not_available": not_available}


def handle_eval_request(body: dict[str, Any]) -> dict[str, Any]:
    """Run the evaluation (offline replay or live agent run) and make it the sign-off evidence."""
    global _LATEST_EVAL
    mode = str(body.get("mode", eval_runner.MODE_OFFLINE))
    gates = body.get("gates") if isinstance(body.get("gates"), dict) else None
    report = eval_runner.run_eval(mode=mode, gates=gates)
    eval_runner.save_report(report)
    with _EVAL_LOCK:
        _LATEST_EVAL = report
    return report


def handle_eval_start(body: dict[str, Any]) -> dict[str, Any]:
    """Start an evaluation asynchronously in a background thread so HTTP proxies never time out."""
    global _LATEST_EVAL
    mode = str(body.get("mode", eval_runner.MODE_OFFLINE))
    if mode not in (eval_runner.MODE_OFFLINE, eval_runner.MODE_LIVE):
        raise ValueError(f"Unknown eval mode {mode!r}.")
    gates = body.get("gates") if isinstance(body.get("gates"), dict) else None
    eval_runner.merge_gates(gates)
    cases = eval_runner.load_cases()

    with _EVAL_LOCK:
        if _EVAL_JOB["running"]:
            return {
                "job_id": _EVAL_JOB["job_id"],
                "running": True,
                "completed_cases": _EVAL_JOB["completed_cases"],
                "total_cases": _EVAL_JOB["total_cases"],
                "phase": _EVAL_JOB["phase"],
            }
        job_id = f"eval-{os.urandom(6).hex()}"
        _EVAL_JOB.update(
            {
                "job_id": job_id,
                "running": True,
                "completed_cases": 0,
                "total_cases": len(cases),
                "phase": "starting",
                "error": "",
                "report": None,
            }
        )

    def _on_progress(completed: int, total: int, phase: str) -> None:
        with _EVAL_LOCK:
            if _EVAL_JOB["job_id"] == job_id:
                _EVAL_JOB["completed_cases"] = completed
                _EVAL_JOB["total_cases"] = total
                _EVAL_JOB["phase"] = phase

    def _worker() -> None:
        global _LATEST_EVAL
        try:
            report = eval_runner.run_eval(mode=mode, gates=gates, on_progress=_on_progress)
            eval_runner.save_report(report)
            with _EVAL_LOCK:
                _LATEST_EVAL = report
                _EVAL_JOB.update(
                    {
                        "running": False,
                        "completed_cases": len(cases),
                        "phase": "done",
                        "report": report,
                    }
                )
        except BaseException as exc:  # noqa: BLE001 - capture CancelledError/Exception cleanly for status polling
            with _EVAL_LOCK:
                _EVAL_JOB.update(
                    {
                        "running": False,
                        "phase": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    threading.Thread(target=_worker, name=f"fsi-{job_id}", daemon=True).start()
    return {
        "job_id": job_id,
        "running": True,
        "completed_cases": 0,
        "total_cases": len(cases),
        "phase": "starting",
    }


def handle_eval_status() -> dict[str, Any]:
    """Return the current background evaluation job progress and completed report (if finished)."""
    with _EVAL_LOCK:
        return dict(_EVAL_JOB)


def handle_signoff_request(body: dict[str, Any]) -> dict[str, Any]:
    """Record or refuse a sign-off for the packet this server computes for the given scenario."""
    analysis = _analysis_for(body, fsi_engine.load_sample_book())
    policy = signoff_policy()
    decision = record_decision.record_signoff(
        packet_sha256=analysis["governance"]["packet_sha256"],
        confirmation_text=str(body.get("confirmation_text", "")),
        maker_email=str(body.get("maker_email", "")),
        signer_email=str(body.get("signer_email", "")),
        reason_code=str(body.get("reason_code", "")),
        notes=str(body.get("notes", "")),
        dual_control=policy["dual_control"],
        eval_evidence=eval_runner.evidence_from_report(latest_eval()),
        require_live_eval=policy["require_live_eval"],
    )
    return {
        "decision": decision,
        "ledger": record_decision.read_ledger_entries(),
    }


def handle_mcp_app_resource() -> dict[str, Any]:
    """Return the MCP App view document the way the MCP server's resource read returns it."""
    return {
        "uri": ripple_view.APP_RESOURCE_URI,
        "mimeType": ripple_view.APP_MIME_TYPE,
        "text": ripple_view.load_app_html(),
    }


def handle_mcp_app_call(body: dict[str, Any]) -> dict[str, Any]:
    """Run one of the view's tools (show, recompute, brief) and return its result in MCP wire shape."""
    name = str(body.get("name", ""))
    known = (ripple_view.SHOW_TOOL, ripple_view.RECOMPUTE_TOOL, ripple_view.BRIEF_TOOL)
    if name not in known:
        return {
            "content": [{"type": "text", "text": f"Unknown tool '{name}'. This server has: {', '.join(known)}."}],
            "isError": True,
        }
    arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
    if name == ripple_view.BRIEF_TOOL:
        return ripple_view.brief_tool_result(
            arguments.get("book_id", ripple_view.DEFAULT_BOOK_ID), arguments.get("tickers_csv", "")
        )
    return ripple_view.call_tool_result(
        arguments.get("book_id", ""),
        arguments.get("rate_bps", ripple_view.DEFAULT_RATE_BPS),
        arguments.get("oil_pct", ripple_view.DEFAULT_OIL_PCT),
        arguments.get("book_name", ""),
    )


def handle_a2ui_request(body: dict[str, Any]) -> dict[str, Any]:
    """Build an A2UI card (``card`` is "holding" or "book") for a book and scenario."""
    analysis = _analysis_for(body, fsi_engine.load_sample_book())
    ticker = body.get("ticker")
    messages = a2ui_cards.build_card(
        analysis,
        str(body.get("card", a2ui_cards.CARD_HOLDING)),
        str(ticker) if ticker is not None else None,
    )
    return {
        "version": a2ui_cards.A2UI_VERSION,
        "catalog_id": a2ui_cards.BASIC_CATALOG_ID,
        "mime_type": a2ui_cards.A2UI_MIME_TYPE,
        "surface_id": messages[0]["createSurface"]["surfaceId"],
        "book_id": analysis["book_id"],
        "holdings": [holding["ticker"] for holding in analysis["holdings"]],
        "messages": messages,
    }


class WorkstationRequestHandler(SimpleHTTPRequestHandler):
    """Serves `assets/ui/` static files and `/api/*` endpoints."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._serve_get()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the UI instead of dropping the socket
            self._send_json(
                {"error": f"Server error: {type(exc).__name__}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _serve_get(self) -> None:
        """Answer one GET: an `/api/*` read, the workbook download, or a static file of `assets/ui/`."""
        parsed = urlparse(self.path)
        if parsed.path == "/api/bootstrap":
            self._send_json(build_bootstrap_payload())
            return
        if parsed.path == "/api/eval/status":
            self._send_json(handle_eval_status())
            return
        if parsed.path == "/api/ledger":
            self._send_json(record_decision.read_ledger_entries())
            return
        if parsed.path == "/api/sheet":
            self._send_json(handle_sheet_status())
            return
        if parsed.path == "/api/mcp_app/resource":
            try:
                self._send_json(handle_mcp_app_resource())
            except (OSError, ValueError) as exc:
                self._send_json(
                    {"error": f"Cannot read the MCP App view document: {exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if parsed.path == "/api/export.xlsx":
            qs = parse_qs(parsed.query)
            book_id = qs.get("book_id", ["next27_flagship"])[0]
            scenario_id = qs.get("scenario_id", [None])[0]
            rate_bps = qs.get("rate_bps", ["50"])[0]
            oil_pct = qs.get("oil_pct", ["25"])[0]
            try:
                xlsx_bytes, _ = model_exporter.export_workbook_bytes(
                    book_id,
                    rate_bps=rate_bps,
                    oil_pct=oil_pct,
                    scenario_id=scenario_id or None,
                )
            except (ValueError, ArithmeticError) as exc:
                self._send_json({"error": f"Cannot export workbook: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            fname = "".join(
                ch if ch.isalnum() or ch in "._+-" else "_"
                for ch in f"fsi_{book_id}_rate{rate_bps}_oil{oil_pct}.xlsx"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(xlsx_bytes)))
            self.end_headers()
            self.wfile.write(xlsx_bytes)
            return

        if parsed.path in {"/", "/index.html"}:
            target = UI_DIR / "index.html"
        else:
            target = (UI_DIR / parsed.path.lstrip("/")).resolve()
            if not target.is_relative_to(UI_DIR.resolve()) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return

        ctype, _ = mimetypes.guess_type(str(target))
        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
            if parsed.path == "/api/analyze":
                self._send_json(handle_analyze_request(body))
            elif parsed.path == "/api/eval":
                self._send_json(handle_eval_request(body))
            elif parsed.path == "/api/eval/start":
                self._send_json(handle_eval_start(body), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/signoff":
                self._send_json(handle_signoff_request(body))
            elif parsed.path == "/api/mcp_app/call":
                self._send_json(handle_mcp_app_call(body))
            elif parsed.path == "/api/a2ui":
                self._send_json(handle_a2ui_request(body))
            elif parsed.path == "/api/sheet/write":
                self._send_json(handle_sheet_write(body))
            elif parsed.path == "/api/sheet/read":
                self._send_json(handle_sheet_read())
            elif parsed.path == "/api/sheet/reported":
                self._send_json(handle_sheet_reported(body))
            else:
                self._send_json({"error": f"Unknown endpoint {parsed.path}"}, status=HTTPStatus.NOT_FOUND)
        except sheets_sync.SheetsError as exc:
            status = HTTPStatus.BAD_REQUEST if exc.code in _SHEET_ERRORS_TO_ACT_ON else HTTPStatus.BAD_GATEWAY
            self._send_json({"error": str(exc), "error_code": exc.code}, status=status)
        except tools.ReportedRequestError as exc:
            self._send_json(
                {"error": str(exc), "error_code": exc.code, "not_available": exc.not_available},
                status=HTTPStatus.BAD_REQUEST,
            )
        except (ValueError, ArithmeticError, KeyError) as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the UI instead of dropping the socket
            self._send_json(
                {"error": f"Server error: {type(exc).__name__}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Serve the workstation until interrupted."""
    server = ThreadingHTTPServer((host, port), WorkstationRequestHandler)
    print(f"FSI Capital Markets Analyst workstation listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run Capital Markets Analyst 4-tab web workstation")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
