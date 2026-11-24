"""Zero-dependency OpenXML (.xlsx) live-formula model exporter and Google Sheets payload builder.

Builds a genuine multi-tab spreadsheet workbook where:
- Sheet 1 (`Scenario_Control`) stores editable Rate (bps) and Oil (%) shock assumptions.
- Sheet 2 (`Portfolio_Shock_Model`) binds 10-K/XBRL inputs and real Excel `<f>` formulas
  for scaled pre-tax impact, after-tax impact, Diluted EPS delta, and `>2x` flags.
- Sheet 3 (`Supply_Chain_Graph`) records 1st-to-3rd-order contagion edges & citations.
- Sheet 4 (`Provenance_Audit`) records each excerpt SHA-256, source posture, disclosure
  status and quoted passage.

The basis and amount cells are left blank when the filing gives no annual income sensitivity
for that factor. The formulas treat a blank basis as "nothing to scale" instead of dividing
by it. The Sheets payload is built here; nothing in this module calls the Sheets API.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import io
from pathlib import Path
import sys
from typing import Any
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
import zipfile

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine  # noqa: E402


def _col_letter(idx_1based: int) -> str:
    result = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


NON_COMPARABLE = "NON-COMPARABLE"
BASIS_OK = "OK"


def row_formulas(row: int | str) -> dict[str, str]:
    """Formulas (without the leading ``=``) for one model row, keyed by column letter.

    A blank basis (column C or E) means the filing gives no annual income sensitivity, so
    the scaled impact is 0 and that factor cannot trigger the extrapolation flag. The flag
    follows the engine's rule: more than 2x the disclosed basis, and a non-zero amount.
    """
    r = str(row)
    rate_beyond = f'IF(C{r}="",FALSE,AND(D{r}<>0,ABS(Scenario_Control!$B$2/C{r})>2))'
    oil_beyond = f'IF(E{r}="",FALSE,AND(F{r}<>0,ABS(Scenario_Control!$B$3/E{r})>2))'
    return {
        "G": f'IF(C{r}="",0,D{r}*(Scenario_Control!$B$2/C{r}))',
        "H": f'IF(E{r}="",0,F{r}*(Scenario_Control!$B$3/E{r}))',
        "I": f"G{r}+H{r}",
        "K": f"I{r}*(1-J{r})",
        "M": f"K{r}/L{r}",
        "O": (
            f'IF(OR({rate_beyond},{oil_beyond}),"EXTRAPOLATION >2x",'
            f'IF(P{r}="{NON_COMPARABLE}","{NON_COMPARABLE}","WITHIN 2x"))'
        ),
    }


def build_formula_grid(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return UI-inspectable cell & Excel-formula representation of the portfolio model."""
    scenario = analysis["scenario"]
    rows: list[dict[str, Any]] = []

    for idx, h in enumerate(analysis["holdings"], start=5):
        r_str = str(idx)
        rows.append(
            {
                "excel_row": idx,
                "ticker": h["ticker"],
                "name": h["name"],
                "inputs": {
                    f"C{r_str}": h["disclosed_basis"]["rate_basis_bps"],
                    f"D{r_str}": h["disclosed_basis"]["rate_pre_tax_m"],
                    f"E{r_str}": h["disclosed_basis"]["oil_basis_pct"],
                    f"F{r_str}": h["disclosed_basis"]["oil_pre_tax_m"],
                    # The rate the engine applied: the XBRL effective rate, or the statutory rate when that
                    # one is not usable (the citation column then says so).
                    f"J{r_str}": h["scaled_impact"]["tax_rate_applied"],
                    f"L{r_str}": h["xbrl"]["diluted_shares_m"],
                    f"N{r_str}": h["xbrl"]["diluted_eps"],
                    f"P{r_str}": NON_COMPARABLE if h["non_comparable_warning"] else BASIS_OK,
                },
                "formulas": {f"{column}{r_str}": formula for column, formula in row_formulas(idx).items()},
                "evaluated": {
                    "rate_scaled_m": h["scaled_impact"]["rate_pre_tax_m"],
                    "oil_scaled_m": h["scaled_impact"]["oil_pre_tax_m"],
                    "total_pre_tax_m": h["scaled_impact"]["total_pre_tax_m"],
                    "after_tax_m": h["scaled_impact"]["after_tax_m"],
                    "eps_delta": h["scaled_impact"]["eps_delta"],
                    "guardrail_status": (
                        "EXTRAPOLATION >2x"
                        if h["extrapolation_warning"]
                        else (NON_COMPARABLE if h["non_comparable_warning"] else "WITHIN 2x")
                    ),
                },
                "tax_rate_basis": h["scaled_impact"]["tax_rate_basis"],
                "conversion_notes": list(h.get("conversion_notes", [])),
                "citation_doc_id": h["provenance"][0]["doc_id"],
                "rate_section": h["provenance"][0]["filing_section"],
                "oil_section": h["provenance"][1]["filing_section"],
                "rate_status": h["disclosed_basis"]["rate_status"],
                "oil_status": h["disclosed_basis"]["oil_status"],
            }
        )

    end_row = 4 + len(rows)
    return {
        "scenario_cells": {
            "Scenario_Control!B2": scenario["rate_bps"],
            "Scenario_Control!B3": scenario["oil_pct"],
            "Scenario_Control!B4": scenario["label"],
        },
        "summary_formulas": {
            f"I{end_row + 1}": f"SUM(I5:I{end_row})",
            f"K{end_row + 1}": f"SUM(K5:K{end_row})",
        },
        "rows": rows,
    }


def _render_sheet_xml(rows_spec: list[list[dict[str, Any]]]) -> bytes:
    """Serialize worksheet rows containing string, number, or live Excel formula cells."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r_idx, row_cells in enumerate(rows_spec, start=1):
        parts.append(f'<row r="{r_idx}">')
        for c_idx, cell in enumerate(row_cells, start=1):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            kind = cell.get("kind", "str")
            style_attr = f' s="{cell["style"]}"' if "style" in cell else ""
            if kind == "num":
                val = escape(str(cell["value"]))
                parts.append(f'<c r="{ref}"{style_attr}><v>{val}</v></c>')
            elif kind == "formula":
                f_expr = escape(str(cell["formula"]))
                cached = escape(str(cell.get("cached", "0")))
                t_attr = ' t="str"' if cell.get("result_type") == "str" else ""
                parts.append(
                    f'<c r="{ref}"{t_attr}{style_attr}><f>{f_expr}</f><v>{cached}</v></c>'
                )
            else:
                text = escape(str(cell.get("value", "")))
                parts.append(
                    f'<c r="{ref}" t="inlineStr"{style_attr}><is><t>{text}</t></is></c>'
                )
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts).encode("utf-8")


def _basis_cell(value: Any) -> dict[str, Any]:
    """A numeric input cell, or an empty cell when the filing gives no basis or amount."""
    return {"value": ""} if value is None else {"kind": "num", "value": value}


def _scenario_note(scenario: dict[str, Any]) -> str:
    odds = scenario.get("market_odds_pct")
    if odds:
        return f"Market odds: {odds}%"
    return "Assumption chosen by the user; no probability is attached"


def _provenance_note(holding: dict[str, Any], prefix: str) -> str:
    """Flags and notes that belong to one factor ("Rate" or "Oil") of a holding."""
    notes = [
        note
        for key in ("non_comparable_flags", "coverage_notes", "basis_notes")
        for note in holding.get(key, [])
        if note.startswith(f"{prefix}:")
    ]
    return " ".join(notes)


def export_workbook_bytes(
    book_id: str = "next27_flagship",
    *,
    rate_bps: str = "25",
    oil_pct: str = "0",
    scenario_id: str | None = None,
    dataset: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build a valid 4-sheet OpenXML .xlsx workbook with live formulas and full provenance."""
    analysis = fsi_engine.analyze_book(
        book_id,
        rate_bps=rate_bps,
        oil_pct=oil_pct,
        scenario_id=scenario_id,
        dataset=dataset,
    )
    grid = build_formula_grid(analysis)
    sc = analysis["scenario"]

    # Sheet 1: Scenario_Control
    sheet1_rows: list[list[dict[str, Any]]] = [
        [{"value": "FSI Capital Markets Analyst — Scenario Control"}, {"value": "Value"}, {"value": "Units / Governance"}],
        [{"value": "Interest Rate Shock"}, {"kind": "num", "value": sc["rate_bps"]}, {"value": "bps parallel shift"}],
        [{"value": "Crude / Jet Fuel Shock"}, {"kind": "num", "value": sc["oil_pct"]}, {"value": "% commodity move"}],
        [{"value": "Scenario Label"}, {"value": sc["label"]}, {"value": _scenario_note(sc)}],
        [{"value": "Packet SHA-256"}, {"value": analysis["governance"]["packet_sha256"]}, {"value": analysis["governance"]["approval_phrase"]}],
    ]

    # Sheet 2: Portfolio_Shock_Model
    headers = [
        "Ticker",
        "Company Name",
        "Rate Basis (bps)",
        "Disclosed Rate Impact ($M)",
        "Oil Basis (%)",
        "Disclosed Oil Impact ($M)",
        "Scaled Rate Pre-Tax ($M) [FORMULA]",
        "Scaled Oil Pre-Tax ($M) [FORMULA]",
        "Total Pre-Tax Impact ($M) [FORMULA]",
        "Tax Rate Applied (XBRL effective rate unless column Q says otherwise)",
        "After-Tax Impact ($M) [FORMULA]",
        "XBRL Diluted Shares (M)",
        "Diluted EPS Impact ($/sh) [FORMULA]",
        "Baseline FY Diluted EPS ($/sh)",
        "Linear Scaling Guardrail [FORMULA]",
        "Basis Check (from the disclosure status)",
        "Primary SEC 10-K Citation",
    ]
    sheet2_rows: list[list[dict[str, Any]]] = [
        [{"value": f"Book: {analysis['book_label']}"}, {"value": f"As-Of: {analysis['governance']['as_of_utc']}"}],
        [{"value": "Every output column (G, H, I, K, M, O) is computed dynamically from Scenario_Control!$B$2:$B$3."}],
        [
            {
                "value": (
                    "A blank basis and amount mean the filing gives no annual income sensitivity for that factor. An oil "
                    "basis taken from a per-gallon or per-barrel disclosure is its percent equivalent at the "
                    "filing's average fuel price (see Provenance_Audit)."
                )
            }
        ],
        [{"value": h} for h in headers],
    ]

    for r in grid["rows"]:
        er = r["excel_row"]
        inp = r["inputs"]
        frm = r["formulas"]
        ev = r["evaluated"]
        sheet2_rows.append(
            [
                {"value": r["ticker"]},
                {"value": r["name"]},
                _basis_cell(inp[f"C{er}"]),
                _basis_cell(inp[f"D{er}"]),
                _basis_cell(inp[f"E{er}"]),
                _basis_cell(inp[f"F{er}"]),
                {"kind": "formula", "formula": frm[f"G{er}"], "cached": ev["rate_scaled_m"]},
                {"kind": "formula", "formula": frm[f"H{er}"], "cached": ev["oil_scaled_m"]},
                {"kind": "formula", "formula": frm[f"I{er}"], "cached": ev["total_pre_tax_m"]},
                {"kind": "num", "value": inp[f"J{er}"]},
                {"kind": "formula", "formula": frm[f"K{er}"], "cached": ev["after_tax_m"]},
                {"kind": "num", "value": inp[f"L{er}"]},
                {"kind": "formula", "formula": frm[f"M{er}"], "cached": ev["eps_delta"]},
                {"kind": "num", "value": inp[f"N{er}"]},
                {
                    "kind": "formula",
                    "formula": frm[f"O{er}"],
                    "cached": ev["guardrail_status"],
                    "result_type": "str",
                },
                {"value": inp[f"P{er}"]},
                {
                    "value": (
                        f"{r['citation_doc_id']} | rate ({r['rate_status']}): {r['rate_section']} | "
                        f"oil ({r['oil_status']}): {r['oil_section']}"
                        + "".join(f" | {note}" for note in r["conversion_notes"])
                    )
                },
            ]
        )

    end_row = 4 + len(grid["rows"])
    # The engine reports only the combined total, so the saved values of the two factor
    # totals are summed here from the same row values their SUM formulas add up.
    rate_total_m = sum((Decimal(r["evaluated"]["rate_scaled_m"]) for r in grid["rows"]), Decimal("0"))
    oil_total_m = sum((Decimal(r["evaluated"]["oil_scaled_m"]) for r in grid["rows"]), Decimal("0"))
    sheet2_rows.append(
        [
            {"value": "TOTAL"},
            {"value": "Portfolio Aggregate"},
            {"value": ""},
            {"value": ""},
            {"value": ""},
            {"value": ""},
            {"kind": "formula", "formula": f"SUM(G5:G{end_row})", "cached": fsi_engine.q2(rate_total_m)},
            {"kind": "formula", "formula": f"SUM(H5:H{end_row})", "cached": fsi_engine.q2(oil_total_m)},
            {
                "kind": "formula",
                "formula": f"SUM(I5:I{end_row})",
                "cached": analysis["portfolio_totals"]["aggregate_pre_tax_impact_m"],
            },
            {"value": ""},
            {
                "kind": "formula",
                "formula": f"SUM(K5:K{end_row})",
                "cached": analysis["portfolio_totals"]["aggregate_after_tax_impact_m"],
            },
            {"value": ""},
            {"value": ""},
            {"value": ""},
            {"value": ""},
            {"value": ""},
            {"value": "Cached values come from the Decimal engine; the formulas recompute in a spreadsheet"},
        ]
    )

    # Sheet 3: Supply_Chain_Graph
    sheet3_rows: list[list[dict[str, Any]]] = [
        [
            {"value": "Edge ID"},
            {"value": "Hop Order"},
            {"value": "Source Ticker"},
            {"value": "Destination Ticker"},
            {"value": "Disclosed Relationship"},
            {"value": "Disclosed Share (%)"},
            {"value": "Share Note"},
            {"value": "Filing Doc ID"},
            {"value": "Section"},
            {"value": "Verbatim 10-K Quote"},
        ]
    ]
    for edge in analysis["contagion_graph"]["edges"]:
        prov = edge["provenance"]
        sheet3_rows.append(
            [
                {"value": edge["edge_id"]},
                {"kind": "num", "value": edge["hop_order"]},
                {"value": edge["src"]},
                {"value": edge["dst"]},
                {"value": edge["relationship"]},
                _basis_cell(edge.get("revenue_share_pct")),
                {"value": edge.get("share_note", "")},
                {"value": prov["doc_id"]},
                {"value": prov["filing_section"]},
                {"value": prov["verbatim_quote"]},
            ]
        )

    # Sheet 4: Provenance_Audit
    sheet4_rows: list[list[dict[str, Any]]] = [
        [
            {"value": "Ticker"},
            {"value": "Risk Factor"},
            {"value": "Doc ID"},
            {"value": "Excerpt SHA-256"},
            {"value": "Source Posture"},
            {"value": "Disclosure Status"},
            {"value": "Passage Role"},
            {"value": "Filing Section"},
            {"value": "Verbatim Quote"},
            {"value": "Flags and Notes"},
        ]
    ]
    for h in analysis["holdings"]:
        for p, prefix in zip(h["provenance"], ("Rate", "Oil")):
            common = [
                {"value": h["ticker"]},
                {"value": p["factor"]},
                {"value": p["doc_id"]},
                {"value": p["sha256"]},
                {"value": p["source_posture"]},
                {"value": p.get("disclosure_status", "")},
            ]
            sheet4_rows.append(
                [
                    *common,
                    {"value": "quoted" if p.get("verbatim_quote") else "no passage"},
                    {"value": p["filing_section"]},
                    {"value": p.get("verbatim_quote", "")},
                    {"value": _provenance_note(h, prefix)},
                ]
            )
            for extra in p.get("supporting_passages", []):
                sheet4_rows.append(
                    [
                        *common,
                        {"value": "supporting"},
                        {"value": extra["section"]},
                        {"value": extra["text"]},
                        {"value": ""},
                    ]
                )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", _WORKBOOK_XML)
        zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        zf.writestr("xl/styles.xml", _STYLES_XML)
        zf.writestr("xl/worksheets/sheet1.xml", _render_sheet_xml(sheet1_rows))
        zf.writestr("xl/worksheets/sheet2.xml", _render_sheet_xml(sheet2_rows))
        zf.writestr("xl/worksheets/sheet3.xml", _render_sheet_xml(sheet3_rows))
        zf.writestr("xl/worksheets/sheet4.xml", _render_sheet_xml(sheet4_rows))

    return buf.getvalue(), grid


def inspect_xlsx_formulas(xlsx_bytes: bytes) -> dict[str, Any]:
    """Verify that an exported .xlsx binary contains live OpenXML <f> formulas."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r") as zf:
        sheet2_xml = zf.read("xl/worksheets/sheet2.xml")
    root = ET.fromstring(sheet2_xml)
    formulas = [el.text or "" for el in root.findall(".//s:f", ns)]
    return {
        "formula_cell_count": len(formulas),
        "has_scenario_sheet_refs": any("Scenario_Control!$B$2" in f for f in formulas),
        "sample_formulas": formulas[:6],
    }


def build_google_sheets_batch_payload(analysis: dict[str, Any]) -> dict[str, Any]:
    """Build a Google Sheets API v4 `spreadsheets.values.batchUpdate` USER_ENTERED payload."""
    grid = build_formula_grid(analysis)
    values: list[list[Any]] = [
        ["Ticker", "Company", "Rate Basis (bps)", "Rate Impact ($M)", "Oil Basis (%)", "Oil Impact ($M)", "Scaled Rate ($M)", "Scaled Oil ($M)", "Total Pre-Tax ($M)", "Tax Rate", "After-Tax ($M)", "Diluted Shares (M)", "dEPS ($/sh)", "FY EPS", "Guardrail", "Basis Check", "Tax Rate Basis"],
    ]

    def number_or_blank(value: Any) -> Any:
        return "" if value is None else float(value)

    def tax_rate_basis(row: dict[str, Any]) -> str:
        """Which rate column J holds: the filing's effective rate, or the engine's note when it applied another."""
        if row["tax_rate_basis"] != fsi_engine.TAX_BASIS_STATUTORY:
            return "XBRL effective tax rate"
        return next((note for note in row["conversion_notes"] if note.startswith("Tax:")), "US federal statutory rate")

    for r in grid["rows"]:
        er = r["excel_row"] - 3  # Offset for compact Google Sheet starting at row 2
        inp = r["inputs"]
        old_er = r["excel_row"]
        formulas = row_formulas(er)
        values.append(
            [
                r["ticker"],
                r["name"],
                number_or_blank(inp[f"C{old_er}"]),
                number_or_blank(inp[f"D{old_er}"]),
                number_or_blank(inp[f"E{old_er}"]),
                number_or_blank(inp[f"F{old_er}"]),
                f"={formulas['G']}",
                f"={formulas['H']}",
                f"={formulas['I']}",
                float(inp[f"J{old_er}"]),
                f"={formulas['K']}",
                float(inp[f"L{old_er}"]),
                f"={formulas['M']}",
                float(inp[f"N{old_er}"]),
                f"={formulas['O']}",
                inp[f"P{old_er}"],
                tax_rate_basis(r),
            ]
        )
    return {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {
                "range": "Scenario_Control!A1:B3",
                "values": [
                    ["Parameter", "Shock"],
                    ["Interest Rate Shock (bps)", float(analysis["scenario"]["rate_bps"])],
                    ["Oil / Fuel Shock (%)", float(analysis["scenario"]["oil_pct"])],
                ],
            },
            {"range": "Portfolio_Shock_Model!A1", "values": values},
        ],
    }


_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet4.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Scenario_Control" sheetId="1" r:id="rId1"/>
    <sheet name="Portfolio_Shock_Model" sheetId="2" r:id="rId2"/>
    <sheet name="Supply_Chain_Graph" sheetId="3" r:id="rId3"/>
    <sheet name="Provenance_Audit" sheetId="4" r:id="rId4"/>
  </sheets>
</workbook>"""

_WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="10"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the live-formula Form 10-K .xlsx model")
    parser.add_argument("--book", default="next27_flagship")
    parser.add_argument("--scenario", default="fed_up_50_oil_spike")
    parser.add_argument(
        "--output",
        default=str(fsi_engine.SKILL_ROOT / "_output" / "sp500_read_the_fine_print_model.xlsx"),
    )
    args = parser.parse_args()

    xlsx_bytes, grid = export_workbook_bytes(args.book, scenario_id=args.scenario)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(xlsx_bytes)
    stats = inspect_xlsx_formulas(xlsx_bytes)
    print(
        f"Exported {out_path} ({len(xlsx_bytes):,} bytes, "
        f"{len(grid['rows'])} holdings, {stats['formula_cell_count']} live Excel formulas)."
    )


if __name__ == "__main__":
    main()
