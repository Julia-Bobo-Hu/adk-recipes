"""Offline runnability, Decimal math, graph, OpenXML formula, sign-off and layout tests."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import io
from pathlib import Path
import re
import sys
import types
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = SKILL_ROOT.parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import (  # noqa: E402
    cleanup,
    edgar_xbrl_mcp,
    eval_runner,
    export_app,
    filing_compare,
    fsi_engine,
    model_exporter,
    record_decision,
    setup,
)

PRACTICE_EVIDENCE = {
    "run_id": "test-offline-run",
    "mode": eval_runner.MODE_OFFLINE,
    "verdict": record_decision.VERDICT_DETERMINISTIC_ONLY,
}


def test_agent_runnability() -> None:
    """scripts/agent.py imports and defines a real ADK root_agent with the skill's tools."""
    import importlib

    from google.adk import agents

    module = importlib.import_module("scripts.agent")
    assert isinstance(module.root_agent, agents.LlmAgent)
    assert [tool.__name__ for tool in module.root_agent.tools] == [
        "run_portfolio_shock_analysis",
        "lookup_statutory_filing_facts",
        "evaluate_book_filing_deltas",
        "scale_disclosed_sensitivity",
        "export_formula_workbook",
        "get_reported_financials",
        "write_scenario_to_sheet",
        "read_scenario_from_sheet",
        "export_reported_workbook",
        "write_reported_figures_to_sheet",
    ]
    assert module.app.root_agent is module.root_agent


def test_decimal_item7a_scaling_and_xbrl_eps_reconciliation() -> None:
    """Verify exact Decimal scaling, tax adjustment, and diluted EPS conversion."""
    dataset = fsi_engine.load_sample_book()
    res = fsi_engine.analyze_book("next27_flagship", scenario_id="fed_up_50_oil_spike")
    by_ticker = {h["ticker"]: h for h in res["holdings"]}

    # UAL at +50 bp and +25% oil, recomputed here from the disclosed figures:
    #   rate: +77 per 100 bp (140 more interest income, 63 more interest expense) -> +38.50
    #   oil:  -116 per USD 1 per barrel; 25% of 2.44 USD/gal * 42 gal = 25.62 USD/bbl -> -2971.92
    xbrl = dataset["companies"]["UAL"]["xbrl"]
    rate = Decimal("77") * Decimal("50") / Decimal("100")
    oil = Decimal("-116") * (Decimal("25") / Decimal("100") * Decimal("2.44") * Decimal("42"))
    after_tax = (rate + oil) * (Decimal("1") - Decimal(xbrl["effective_tax_rate"]))
    eps = after_tax / Decimal(xbrl["diluted_shares_m"])

    ual = by_ticker["UAL"]
    assert ual["scaled_impact"]["rate_pre_tax_m"] == fsi_engine.q2(rate) == "38.50"
    assert ual["scaled_impact"]["oil_pre_tax_m"] == fsi_engine.q2(oil) == "-2971.92"
    assert ual["scaled_impact"]["total_pre_tax_m"] == "-2933.42"
    assert ual["scaled_impact"]["after_tax_m"] == fsi_engine.q2(after_tax) == "-2285.13"
    assert ual["scaled_impact"]["eps_delta"] == fsi_engine.q4(eps) == "-6.9563"
    assert ual["scaled_impact"]["oil_multiplier"] == "25.62"
    assert ual["extrapolation_warning"] is True  # 25.62x a one-dollar-per-barrel basis
    assert ual["disclosed_basis"]["oil_basis_unit"] == "usd_per_barrel"
    # A derived figure says so, and names its components.
    assert any("Derived net figure" in note for note in ual["basis_notes"])
    components = dataset["companies"]["UAL"]["sensitivities"]["rate"]["components"]
    assert sum(Decimal(c["amount_m"]) for c in components) == Decimal("77")

    # DAL: a per-gallon disclosure converted with the filing's own average price.
    dal = by_ticker["DAL"]
    assert dal["scaled_impact"]["oil_pre_tax_m"] == "-2300.00"  # -40 per cent/gal * 57.5 cents
    assert dal["disclosed_basis"]["oil_basis_pct"] == "0.4347826087"

    # A disclosure that is not an annual income amount produces no EPS, and says why.
    for ticker, status in (("BA", "not_quantified"), ("MSFT", "fair_value_only"), ("GE", "not_convertible")):
        holding = by_ticker[ticker]
        assert holding["disclosed_basis"]["rate_status"] == status
        assert holding["disclosed_basis"]["rate_basis_bps"] is None
        # No disclosed amount either: the filing states none, which is not a stated zero.
        assert holding["disclosed_basis"]["rate_pre_tax_m"] is None
        assert holding["scaled_impact"]["total_pre_tax_m"] == "0.00"
        assert holding["scaled_impact"]["eps_delta"] == "0.0000"
        assert holding["non_comparable_warning"] is True

    # No oil passage: a coverage note, not a warning, and no quotation is fabricated.
    jpm = by_ticker["JPM"]
    assert jpm["non_comparable_warning"] is False
    assert jpm["coverage_notes"] and jpm["coverage_notes"][0].startswith("Oil:")
    assert "verbatim_quote" not in jpm["provenance"][1]
    assert jpm["provenance"][1]["disclosure_status"] == "none_found"

    # A zero result carries no sign.
    flat = fsi_engine.analyze_book("next27_flagship", rate_bps="25", oil_pct="0")
    assert {h["scaled_impact"]["oil_pre_tax_m"] for h in flat["holdings"]} == {"0.00"}


def test_pasted_passage_scaler_matches_book_engine_and_rejects_bad_input() -> None:
    """The ad-hoc scaler reproduces the book engine and refuses fair-value figures as EPS."""
    xbrl = fsi_engine.load_sample_book()["companies"]["DAL"]["xbrl"]
    dal = next(
        h
        for h in fsi_engine.analyze_book("next27_flagship", rate_bps="25", oil_pct="0")["holdings"]
        if h["ticker"] == "DAL"
    )
    scaled = fsi_engine.scale_disclosed_sensitivity(
        disclosed_basis="100",
        disclosed_pre_tax_impact_m="-11",
        scenario_shock="25",
        effective_tax_rate=xbrl["effective_tax_rate"],
        diluted_shares_m=xbrl["diluted_shares_m"],
    )
    assert scaled["scaled_pre_tax_m"] == dal["scaled_impact"]["total_pre_tax_m"] == "-2.75"
    assert scaled["after_tax_m"] == dal["scaled_impact"]["after_tax_m"] == "-2.22"
    assert scaled["eps_delta"] == dal["scaled_impact"]["eps_delta"] == "-0.0034"
    assert scaled["extrapolation_warning"] is False

    stretched = fsi_engine.scale_disclosed_sensitivity(
        disclosed_basis="100",
        disclosed_pre_tax_impact_m="-11",
        scenario_shock="250",
        effective_tax_rate="0.215",
        diluted_shares_m="645.0",
    )
    assert stretched["extrapolation_warning"] is True

    fair_value = fsi_engine.scale_disclosed_sensitivity(
        disclosed_basis="100",
        disclosed_pre_tax_impact_m="2950",
        scenario_shock="25",
        effective_tax_rate="0.15",
        diluted_shares_m="748",
        income_statement_comparable=False,
    )
    assert fair_value["non_comparable_warning"] is True
    assert fair_value["eps_delta"] == "0.0000"

    for bad in (
        {"disclosed_basis": "0"},
        {"effective_tax_rate": "1.2"},
        {"diluted_shares_m": "0"},
        {"disclosed_pre_tax_impact_m": "eleven"},
    ):
        kwargs = {
            "disclosed_basis": "100",
            "disclosed_pre_tax_impact_m": "-11",
            "scenario_shock": "25",
            "effective_tax_rate": "0.215",
            "diluted_shares_m": "645.0",
            **bad,
        }
        try:
            fsi_engine.scale_disclosed_sensitivity(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_supply_chain_three_hop_contagion_and_bigquery_gql() -> None:
    """Verify 1st -> 2nd -> 3rd-order contagion traversal and ISO GQL generation."""
    res = fsi_engine.analyze_book("next27_flagship", scenario_id="fed_up_50_oil_spike")
    graph = res["contagion_graph"]
    edges = {(e["src"], e["dst"]): e for e in graph["edges"]}

    # Hop 2: Boeing supplies aircraft to the shocked airlines. UAL's filing gives no share.
    assert ("BA", "UAL") in edges
    assert edges[("BA", "UAL")]["hop_order"] == 2
    assert edges[("BA", "UAL")]["provenance"]["verbatim_quote"]
    assert edges[("BA", "UAL")]["revenue_share_pct"] is None
    assert edges[("BA", "UAL")]["share_note"]

    # Hop 3: Hexcel sells to Boeing (13% of Hexcel's 2025 net sales, per its 10-K)
    assert ("HXL", "BA") in edges
    assert edges[("HXL", "BA")]["hop_order"] == 3
    assert edges[("HXL", "BA")]["revenue_share_pct"] == "13"
    assert "13%" in edges[("HXL", "BA")]["provenance"]["verbatim_quote"]

    # A share is a number only where the cited passage states one.
    for edge in graph["edges"]:
        if edge["revenue_share_pct"] is not None:
            assert f"{edge['revenue_share_pct']}%" in edge["provenance"]["verbatim_quote"], edge["edge_id"]
    assert graph["summary"] == {"hop1_count": 5, "hop2_count": 6, "hop3_count": 2, "total_edges": 10}

    tech_graph = fsi_engine.analyze_book("megacap_tech", scenario_id="fed_up_50_oil_spike")["contagion_graph"]
    assert tech_graph["summary"] == {"hop1_count": 2, "hop2_count": 3, "hop3_count": 1, "total_edges": 4}

    gql = graph["bigquery_gql"]
    assert "CREATE OR REPLACE PROPERTY GRAPH" in gql
    assert "GRAPH_TABLE" in gql
    assert "MATCH (hop3:Company)-[e2:DISCLOSED_LINK]->(hop2:Company)-[e1:DISCLOSED_LINK]->(shocked:Company)" in gql


def test_source_posture_and_excerpt_hashes_are_truthful() -> None:
    """The posture comes from the data, every hash can be recomputed, every source is recorded."""
    dataset = fsi_engine.load_sample_book()
    assert dataset["metadata"]["source_posture"] == fsi_engine.POSTURE_AUTHORITATIVE
    assert dataset["metadata"]["data_notice"]
    assert dataset["metadata"]["retrieval"]["builder"] == "scripts/build_sample_book.py"
    assert fsi_engine.verify_excerpt_hashes(dataset) == []

    res = fsi_engine.analyze_book("next27_flagship", scenario_id="fed_up_50_oil_spike")
    postures = {p["source_posture"] for h in res["holdings"] for p in h["provenance"]}
    postures |= {e["provenance"]["source_posture"] for e in res["contagion_graph"]["edges"]}
    postures.add(res["governance"]["source_posture"])
    assert postures == {fsi_engine.POSTURE_AUTHORITATIVE}

    dal = next(h for h in res["holdings"] if h["ticker"] == "DAL")
    assert dal["provenance"][0]["sha256"] == fsi_engine.compute_excerpt_sha256(dataset, "DAL")

    # A dataset may claim authoritative_filing only if each issuer names the filing it came from.
    for ticker, company in dataset["companies"].items():
        filing = company["filing"]
        assert filing["form"] == "10-K", ticker
        assert re.fullmatch(r"\d{10}-\d{2}-\d{6}", filing["accession_number"]), ticker
        assert filing["url"].startswith("https://www.sec.gov/Archives/edgar/data/"), ticker
        assert re.fullmatch(r"[0-9a-f]{64}", filing["document_sha256"]), ticker
        assert company["filing_doc_id"].startswith(f"{ticker}-10K-FY"), ticker
        assert company["xbrl_sources"]["accession_number"] == filing["accession_number"], ticker
        for metric in ("revenue_m", "effective_tax_rate", "diluted_shares_m", "diluted_eps", "long_term_debt_m"):
            assert company["xbrl_sources"][metric]["tag"].startswith("us-gaap:"), (ticker, metric)
            assert company["xbrl_sources"][metric]["period_end"] == filing["report_date"], (ticker, metric)

        for factor in ("rate", "oil"):
            info = company["sensitivities"][factor]
            passages = info.get("passages", [])
            assert info["status"] in fsi_engine.SENSITIVITY_STATUSES, (ticker, factor)
            if info["status"] == fsi_engine.STATUS_INCOME_STATEMENT:
                assert passages and "pre_tax_impact_m" in info, (ticker, factor)
            elif info["status"] == fsi_engine.STATUS_NONE_FOUND:
                assert not passages and info["coverage_note"], (ticker, factor)
            else:
                # A disclosure exists but is not converted: it must be quoted and explained.
                assert passages and info["non_comparable_flag"], (ticker, factor)
                assert "pre_tax_impact_m" not in info, (ticker, factor)

    # No invented scenario probabilities and no invented credit scores ride along with filing data.
    assert all("market_odds_pct" not in scenario for scenario in dataset["scenarios"])
    assert all("credit_rubric" not in company["yoy_delta"] for company in dataset["companies"].values())


def test_openxml_xlsx_contains_live_formulas_and_four_sheets() -> None:
    """Exported .xlsx must be a valid ZIP with dynamic <f> formulas referencing Scenario_Control."""
    xlsx_bytes, grid = model_exporter.export_workbook_bytes(
        "next27_flagship", scenario_id="fed_up_25_oil_flat"
    )
    assert len(xlsx_bytes) > 3000
    stats = model_exporter.inspect_xlsx_formulas(xlsx_bytes)
    assert stats["has_scenario_sheet_refs"] is True
    assert stats["formula_cell_count"] >= len(grid["rows"]) * 6

    sheets_payload = model_exporter.build_google_sheets_batch_payload(
        fsi_engine.analyze_book("defensive_consumer", rate_bps="25", oil_pct="10")
    )
    assert sheets_payload["valueInputOption"] == "USER_ENTERED"
    # A missing basis or amount is a blank cell, never a zero or a placeholder number.
    clorox = next(row for row in sheets_payload["data"][1]["values"] if row[0] == "CLX")
    assert clorox[2:6] == ["", "", "", ""] and clorox[15] == model_exporter.NON_COMPARABLE
    # An issuer with a convertible figure keeps its numbers.
    walmart = next(row for row in sheets_payload["data"][1]["values"] if row[0] == "WMT")
    assert walmart[2:6] == [100.0, -100.0, "", ""]


class _FormulaEvaluator:
    """Evaluate the workbook's formulas the way a spreadsheet does (IF is lazy, AND/OR are not).

    Small on purpose: numbers, strings, cell references, + - * /, comparisons and the
    functions the exporter emits. Dividing by a blank or zero cell raises, as #DIV/0! would.
    """

    _TOKEN = re.compile(
        r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>\"[^\"]*\")|(?P<ref>(?:[A-Za-z_]+!)?\$?[A-Z]+\$?\d+)"
        r"|(?P<name>[A-Z]+)(?=\()|(?P<op><>|>=|<=|[-+*/=<>(),]))"
    )

    def __init__(self, cells: dict[str, Any], formulas: dict[str, str]) -> None:
        self.cells = cells
        self.formulas = formulas

    def value(self, ref: str) -> Any:
        ref = ref.replace("$", "")
        if ref in self.formulas:
            return self.evaluate(self.formulas[ref])
        return self.cells[ref]

    def evaluate(self, formula: str) -> Any:
        self.tokens = []
        offset = 0
        while offset < len(formula):
            match = self._TOKEN.match(formula, offset)
            assert match and match.end() > offset, f"cannot read {formula[offset:]!r}"
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            offset = match.end()
        self.position = 0
        result = self._comparison(True)
        assert self.position == len(self.tokens), formula
        return result

    def _peek(self) -> tuple[str | None, str | None]:
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def _take(self, expected: str | None = None) -> str:
        kind, text = self.tokens[self.position]
        assert expected is None or text == expected, (expected, text)
        self.position += 1
        return text

    def _comparison(self, live: bool) -> Any:
        left = self._additive(live)
        if self._peek()[1] in ("=", "<>", ">", "<", ">=", "<="):
            operator = self._take()
            right = self._additive(live)
            if not live:
                return None
            if operator in ("=", "<>"):
                return (left == right) == (operator == "=")
            return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[operator]
        return left

    def _additive(self, live: bool) -> Any:
        total = self._term(live)
        while self._peek()[1] in ("+", "-"):
            operator = self._take()
            right = self._term(live)
            if live:
                total = total + right if operator == "+" else total - right
        return total

    def _term(self, live: bool) -> Any:
        product = self._unary(live)
        while self._peek()[1] in ("*", "/"):
            operator = self._take()
            right = self._unary(live)
            if not live:
                continue
            if operator == "/":
                if right in ("", 0):
                    raise ZeroDivisionError("#DIV/0!")
                product = product / right
            else:
                product = product * right
        return product

    def _unary(self, live: bool) -> Any:
        if self._peek()[1] == "-":
            self._take()
            operand = self._unary(live)
            return -operand if live else None
        return self._primary(live)

    def _primary(self, live: bool) -> Any:
        kind, text = self._peek()
        if kind == "num":
            return Decimal(self._take())
        if kind == "str":
            return self._take()[1:-1]
        if kind == "ref":
            self._take()
            if not live:
                return None
            saved = (self.tokens, self.position)
            result = self.value(text.split("!")[-1] if not text.startswith("Scenario_Control!") else text)
            self.tokens, self.position = saved
            return result
        if kind == "name":
            name = self._take()
            self._take("(")
            if name == "IF":
                condition = self._comparison(live)
                self._take(",")
                when_true = self._comparison(live and bool(condition))
                self._take(",")
                when_false = self._comparison(live and not condition)
                self._take(")")
                return (when_true if condition else when_false) if live else None
            arguments = [self._comparison(live)]
            while self._peek()[1] == ",":
                self._take()
                arguments.append(self._comparison(live))
            self._take(")")
            if not live:
                return None
            return {"AND": all, "OR": any, "ABS": lambda xs: abs(xs[0])}[name](arguments)
        assert text == "(", text
        self._take("(")
        inner = self._comparison(live)
        self._take(")")
        return inner


def _evaluate_grid(grid: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recompute every formula cell of the portfolio model from its inputs alone."""

    def number(value: Any) -> Any:
        return "" if value is None else Decimal(str(value))

    cells: dict[str, Any] = {
        "Scenario_Control!B2": Decimal(grid["scenario_cells"]["Scenario_Control!B2"]),
        "Scenario_Control!B3": Decimal(grid["scenario_cells"]["Scenario_Control!B3"]),
    }
    formulas: dict[str, str] = {}
    for row in grid["rows"]:
        for ref, value in row["inputs"].items():
            cells[ref] = value if ref.startswith("P") else number(value)
        formulas.update(row["formulas"])
    # FALSE and TRUE appear as bare words in the formulas.
    formulas = {ref: f.replace("FALSE", "(1=0)").replace("TRUE", "(1=1)") for ref, f in formulas.items()}
    evaluator = _FormulaEvaluator(cells, formulas)
    return {
        row["ticker"]: {column: evaluator.value(f"{column}{row['excel_row']}") for column in "GHIKMO"}
        for row in grid["rows"]
    }


def test_workbook_formulas_reproduce_the_engine_for_every_holding() -> None:
    """The formulas in the sheet, evaluated from the input cells alone, give the engine's numbers."""
    for book_id, rate, oil in (
        ("next27_flagship", "50", "25"),
        ("next27_flagship", "250", "30"),
        ("aero_transport", "-50", "-20"),
        ("defensive_consumer", "200", "0"),
        ("megacap_tech", "0", "0"),
    ):
        analysis = fsi_engine.analyze_book(book_id, rate_bps=rate, oil_pct=oil)
        grid = model_exporter.build_formula_grid(analysis)
        recomputed = _evaluate_grid(grid)
        for row in grid["rows"]:
            cells, expected = recomputed[row["ticker"]], row["evaluated"]
            where = (book_id, rate, oil, row["ticker"])
            assert fsi_engine.q2(cells["G"]) == expected["rate_scaled_m"], where
            assert fsi_engine.q2(cells["H"]) == expected["oil_scaled_m"], where
            assert fsi_engine.q2(cells["I"]) == expected["total_pre_tax_m"], where
            assert fsi_engine.q2(cells["K"]) == expected["after_tax_m"], where
            assert fsi_engine.q4(cells["M"]) == expected["eps_delta"], where
            assert cells["O"] == expected["guardrail_status"], where


def _saved_model_cells(xlsx_bytes: bytes) -> dict[str, tuple[str | None, str | None]]:
    """Map each cell of the model sheet to its (formula, saved value) as stored in the .xlsx file."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
    cells: dict[str, tuple[str | None, str | None]] = {}
    for cell in root.iterfind(".//s:c", ns):
        formula, saved = cell.find("s:f", ns), cell.find("s:v", ns)
        cells[cell.attrib["r"]] = (
            formula.text if formula is not None else None,
            saved.text if saved is not None else None,
        )
    return cells


def test_workbook_saved_totals_equal_the_rows_above_them() -> None:
    """A viewer that does not recalculate shows the saved values, so each saved total must match its column."""
    for book_id, rate, oil in (
        ("next27_flagship", "50", "25"),
        ("aero_transport", "-50", "-20"),
        ("defensive_consumer", "200", "0"),
        ("megacap_tech", "0", "0"),
    ):
        xlsx_bytes, grid = model_exporter.export_workbook_bytes(book_id, rate_bps=rate, oil_pct=oil)
        cells = _saved_model_cells(xlsx_bytes)
        first, last = 5, 4 + len(grid["rows"])
        for column in "GHIK":
            formula, saved = cells[f"{column}{last + 1}"]
            where = (book_id, rate, oil, column)
            assert formula == f"SUM({column}{first}:{column}{last})", where
            rows_above = sum(
                (Decimal(str(cells[f"{column}{row}"][1])) for row in range(first, last + 1)),
                Decimal("0"),
            )
            assert Decimal(str(saved)) == rows_above, where

    # The demo scenario has non-zero totals in both factor columns, so a saved 0 cannot pass.
    flagship, _ = model_exporter.export_workbook_bytes("next27_flagship", rate_bps="50", oil_pct="25")
    saved_cells = _saved_model_cells(flagship)
    assert (saved_cells["G17"][1], saved_cells["H17"][1], saved_cells["I17"][1]) == ("985.50", "-5271.92", "-4286.42")


def test_offline_scanned_pdf_posture_and_advisory_guardrail() -> None:
    """Scanned image-only PDFs return unassessable_offline; buy/sell/hold text is blocked."""
    scan_verdict = fsi_engine.inspect_pdf_posture("   ", filename="scan_deck.pdf", offline_mode=True)
    assert scan_verdict["status"] == "unassessable_offline"
    assert scan_verdict["source_posture"] == "customer_submitted_unverified"

    blocked = fsi_engine.enforce_non_advisory_guardrail("We recommend buying NVDA with a price target of $200.")
    assert blocked["allowed"] is False
    assert len(blocked["violations"]) >= 2

    clean = fsi_engine.enforce_non_advisory_guardrail(
        "A +100 bp shift increases net interest income by $85 million per Item 7A."
    )
    assert clean["allowed"] is True
    # The policy label names the control and no regulation.
    assert blocked["policy"] == clean["policy"] == "non_investment_advice"


def test_maker_checker_signoff_hash_chain_and_iap_link(tmp_path: Path) -> None:
    """Sign-off must reject self-approval, log refusals separately, and hash-chain valid approvals."""
    analysis = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="0")
    sha = analysis["governance"]["packet_sha256"]
    phrase = analysis["governance"]["approval_phrase"]

    # 1. Self-approval refused
    refused = record_decision.record_signoff(
        packet_sha256=sha,
        confirmation_text=phrase,
        maker_email="analyst@desk.example",
        signer_email="analyst@desk.example",
        reason_code="MACRO_DESK_RELEASE",
        dual_control=True,
        eval_evidence=PRACTICE_EVIDENCE,
        ledger_dir=tmp_path,
    )
    assert refused["status"] == "refused"
    assert "DUAL_CONTROL_VIOLATION" in refused["refusal_reason"]
    # The refusal states the control itself and cites no regulation.
    assert refused["refusal_reason"].endswith("A second person must approve.")
    assert "FINRA" not in refused["refusal_reason"]

    # 2. Wrong hash code refused
    bad_hash = record_decision.record_signoff(
        packet_sha256=sha,
        confirmation_text="APPROVE 000000",
        maker_email="analyst@desk.example",
        signer_email="principal@desk.example",
        reason_code="MACRO_DESK_RELEASE",
        dual_control=True,
        eval_evidence=PRACTICE_EVIDENCE,
        ledger_dir=tmp_path,
    )
    assert bad_hash["status"] == "refused"
    assert "HASH_CONFIRMATION_MISMATCH" in bad_hash["refusal_reason"]

    # 3. Two distinct approvals form a cryptographic hash chain
    first_ok = record_decision.record_signoff(
        packet_sha256=sha,
        confirmation_text=phrase,
        maker_email="analyst@desk.example",
        signer_email="principal@desk.example",
        reason_code="MACRO_DESK_RELEASE",
        dual_control=True,
        eval_evidence=PRACTICE_EVIDENCE,
        ledger_dir=tmp_path,
    )
    assert first_ok["status"] == "approved"
    assert first_ok["prev_record_hash"] == "GENESIS"

    second_ok = record_decision.record_signoff(
        packet_sha256=sha,
        confirmation_text=phrase,
        maker_email="analyst@desk.example",
        signer_email="chief.risk@desk.example",
        reason_code="RISK_COMMITTEE_PACKET",
        dual_control=True,
        eval_evidence=PRACTICE_EVIDENCE,
        ledger_dir=tmp_path,
    )
    assert second_ok["status"] == "approved"
    assert second_ok["prev_record_hash"] == first_ok["record_hash"]

    entries = record_decision.read_ledger_entries(tmp_path)
    assert len(entries["approvals"]) == 2
    assert len(entries["refusals"]) == 2

    safe_url = record_decision.format_iap_safe_url(
        "https://fsi-workstation.a.run.app/review", "reviewer@corp.example"
    )
    assert "authuser=reviewer%40corp.example" in safe_url


def test_cleanup_dry_run_default_and_audit_ledger_retention_guard() -> None:
    """cleanup.py must preserve audit tables by default and block unconfirmed --include-records."""
    default_plan = cleanup.build_cleanup_plan(
        project_id="fsi-prod-proj",
        audit_bucket="fsi-audit-worm",
    )
    assert default_plan["dry_run"] is True
    assert "fsi-prod-proj.fsi_capital_markets.decision_ledger" in default_plan["protected_preserved"]
    assert "gs://fsi-audit-worm" in default_plan["protected_preserved"]

    blocked = cleanup.build_cleanup_plan(
        project_id="fsi-prod-proj",
        include_records=True,
        confirm_project="wrong-proj",
    )
    assert blocked["status"] == "refused"

    confirmed = cleanup.build_cleanup_plan(
        project_id="fsi-prod-proj",
        dry_run=True,
        include_records=True,
        confirm_project="fsi-prod-proj",
    )
    assert confirmed["status"] == "planned"
    assert confirmed["protected_preserved"] == []


def test_watcher_lookup_setup_export_and_policy_layout(tmp_path: Path) -> None:
    """Watcher renders, setup DDL is complete, the bundle exports, and the layout obeys policy."""
    dataset = fsi_engine.load_sample_book()
    watcher = filing_compare.evaluate_book_deltas("next27_flagship")
    assert len(watcher["rows"]) == 12
    assert "the-watcher" in watcher["watcher_markdown"]
    assert f"`{fsi_engine.POSTURE_AUTHORITATIVE}`" in watcher["watcher_markdown"]
    rows = {row["ticker"]: row for row in watcher["rows"]}

    # A passage-based record quotes a bundled passage that states both periods.
    apple = rows["AAPL"]
    assert (apple["basis"], apple["prior_value"], apple["current_value"]) == (
        "filing_passage",
        "$129 million",
        "$95 million",
    )
    assert "$129" in apple["evidence_quote"] and "$95" in apple["evidence_quote"]
    bundled = {p["text"] for p in fsi_engine.canonical_excerpt_passages(dataset, "AAPL")}
    assert apple["provenance"]["verbatim_quote"] in bundled
    assert apple["provenance"]["sha256"] == fsi_engine.compute_excerpt_sha256(dataset, "AAPL")

    # An XBRL-based record names its tag and carries no quotation.
    delta = rows["DAL"]
    assert delta["basis"] == "xbrl" and delta["evidence_quote"] is None and "provenance" not in delta
    assert "us-gaap:LongTermDebtNoncurrent" in delta["value_source"]
    assert delta["current_value"] == "$11,936 million"

    # Filing-sourced data carries no rubric scores, and none are made up.
    assert {row["rubric_status"] for row in watcher["rows"]} == {"not_scored"}
    assert all(row["credit_scores"] is None for row in watcher["rows"])
    assert "Not scored" in watcher["watcher_markdown"]

    # A dataset that does carry analyst scores gets them back, with the composite computed here.
    scored_dataset = fsi_engine.load_sample_book()
    scored_dataset["companies"]["DAL"]["yoy_delta"]["credit_rubric"] = {
        "leverage_trajectory": 4,
        "liquidity_headroom": 4,
        "margin_resilience": 3,
        "covenant_risk": 4,
        "overall_stance": "Improving Credit Profile",
        "evidence_quote": "Analyst note supplied with the dataset.",
    }
    scored = filing_compare.evaluate_book_deltas("next27_flagship", dataset=scored_dataset)
    scored_row = next(row for row in scored["rows"] if row["ticker"] == "DAL")
    assert scored_row["rubric_status"] == "scored"
    assert scored_row["credit_scores"]["composite_score"] == "3.75"
    assert "`3.75/5.00`" in scored["watcher_markdown"]

    fact = edgar_xbrl_mcp.lookup_company_xbrl_and_item7a("NVDA")
    assert fact["source_posture"] == fsi_engine.POSTURE_AUTHORITATIVE
    assert fact["excerpt_sha256"] == fsi_engine.compute_excerpt_sha256(dataset, "NVDA")
    assert fact["filing"]["accession_number"] == "0001045810-26-000021"

    ddl = setup.run_setup(project_id="demo-proj", dry_run=True)["ddl_preview"]
    statements = setup.split_provisioning_statements(ddl)
    assert sum(s.startswith("CREATE TABLE IF NOT EXISTS") for s in statements) == 5
    # The property graph follows a SQL comment header; it must not be dropped with the comment.
    assert sum(s.startswith("CREATE OR REPLACE PROPERTY GRAPH") for s in statements) == 1
    assert not any(s.upper().startswith("SELECT") for s in statements)
    assert all(not line.strip().startswith("--") for s in statements for line in s.splitlines())

    bundle_dir = export_app.export_bundle(tmp_path / "bundle")
    assert (bundle_dir / "agents-cli-manifest.yaml").exists()
    assert (bundle_dir / "scripts" / "eval_runner.py").exists()
    # The image installs the locked dependencies, so the live evaluation can run in it.
    assert (bundle_dir / "uv.lock").exists()
    assert not list(bundle_dir.rglob("__pycache__"))
    dockerfile = (bundle_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen" in dockerfile
    # An unquoted ">=" in a RUN line is a shell redirect, not a version specifier.
    assert ">=" not in re.sub(r'"[^"]*"', "", dockerfile)
    deploy = (bundle_dir / "deploy_cloudrun.sh").read_text(encoding="utf-8")
    assert "--no-allow-unauthenticated" in deploy
    assert "--allow-unauthenticated" not in deploy.replace("--no-allow-unauthenticated", "")
    # The Gemini location is not tied to the Cloud Run region.
    assert "GOOGLE_CLOUD_LOCATION=${GEMINI_LOCATION}" in deploy
    assert "--service-account=" in deploy and "roles/aiplatform.user" in deploy

    # Verify policy: zero forbidden manifest.yaml / AGENTS.md inside plugins/fsi
    assert not list(PLUGIN_ROOT.rglob("manifest.yaml"))
    assert not list(PLUGIN_ROOT.rglob("AGENTS.md"))
    # A bare "design-spec.md" ignore rule would also drop the shipped template in assets/
    # from a checkout; only the spec a user generates at a skill's root is ignored.
    ignore_rules = (PLUGIN_ROOT / ".gitignore").read_text(encoding="utf-8").split("\n")
    assert "design-spec.md" not in ignore_rules
    assert "/skills/*/design-spec.md" in ignore_rules


_SQL_LITERAL = re.compile(r"NUMERIC '(?P<num>[^']*)'|'(?P<str>(?:[^'\\]|\\.)*)'|(?P<null>NULL)")
_AUDIT_TABLES = ("eval_runs", "decision_ledger", "signoff_refusals")


def _decode_rows(insert_sql: str) -> list[list[Any]]:
    """Read the VALUES rows of a generated INSERT back into Python values."""
    rows: list[list[Any]] = []
    for line in insert_sql.split("VALUES\n", 1)[1].split(",\n"):
        assert line.startswith("(") and line.endswith(")"), line
        values: list[Any] = []
        position = 1
        while position < len(line) - 1:
            match = _SQL_LITERAL.match(line, position)
            assert match, line[position : position + 60]
            if match.group("null"):
                values.append(None)
            elif match.group("num") is not None:
                values.append(Decimal(match.group("num")))
            else:
                values.append(re.sub(r"\\(.)", r"\1", match.group("str")))
            position = match.end()
            if position < len(line) - 1:
                assert line.startswith(", ", position), line[position : position + 10]
                position += 2
        rows.append(values)
    return rows


def test_sample_load_sql_round_trips_the_bundled_dataset() -> None:
    """The load replaces the two reference tables with exactly what the bundle holds."""
    dataset = fsi_engine.load_sample_book()
    statements = setup.generate_sample_load_statements("demo-proj", "fsi_capital_markets")
    fq = "`demo-proj.fsi_capital_markets`"

    # Links go out before the companies they reference and come back after them.
    assert statements[:2] == [
        f"DELETE FROM {fq}.supply_chain_edges WHERE TRUE",
        f"DELETE FROM {fq}.sp500_companies WHERE TRUE",
    ]
    assert statements[2].startswith(f"INSERT INTO {fq}.sp500_companies (ticker, ")
    assert statements[3].startswith(f"INSERT INTO {fq}.supply_chain_edges (edge_id, ")
    assert len(statements) == 4
    assert not any(table in statement for statement in statements for table in _AUDIT_TABLES)

    def number(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    assert _decode_rows(statements[2]) == [
        [
            ticker,
            company["name"],
            company["cik"],
            company["sector"],
            company["filing_doc_id"],
            company["filing"]["accession_number"],
            company["excerpt_sha256"],
            number(company["xbrl"]["revenue_m"]),
            number(company["xbrl"]["effective_tax_rate"]),
            number(company["xbrl"]["diluted_shares_m"]),
            number(company["xbrl"]["diluted_eps"]),
            number(company["xbrl"]["long_term_debt_m"]),
        ]
        for ticker, company in dataset["companies"].items()
    ]
    edge_rows = _decode_rows(statements[3])
    assert edge_rows == [
        [
            edge["edge_id"],
            edge["src"],
            edge["dst"],
            edge["relationship"],
            number(edge["revenue_share_pct"]),
            edge["share_note"],
            edge["doc_id"],
            edge["section"],
            edge["verbatim_quote"],
        ]
        for edge in dataset["supply_chain_edges"]
    ]
    assert len(_decode_rows(statements[2])) == 20 and len(edge_rows) == 12
    # A link whose filing states no share is loaded as NULL, never as zero.
    assert [row[0] for row in edge_rows if row[4] is None] == ["E02", "E03", "E04", "E05", "E06", "E11", "E12"]
    # The quotation with marks of its own survives the SQL escaping.
    assert '("Boeing")' in next(row[8] for row in edge_rows if row[0] == "E05")


def test_sql_literals_escape_and_reject() -> None:
    assert setup._sql_string(None) == "NULL"
    assert setup._sql_string("O'Neil \\ x\ny") == "'O\\'Neil \\\\ x y'"
    assert setup._sql_numeric(None) == "NULL"
    assert setup._sql_numeric("13") == "NUMERIC '13'"
    assert setup._sql_numeric("0.191") == "NUMERIC '0.191'"
    assert setup._sql_numeric("1E+3") == "NUMERIC '1000'"
    with pytest.raises(InvalidOperation):
        setup._sql_numeric("13 OR 1=1")
    for not_finite in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="Not a finite number"):
            setup._sql_numeric(not_finite)


class _RecordingBigQueryClient:
    """Stands in for ``google.cloud.bigquery.Client``; it records SQL and runs nothing."""

    executed: list[str] = []
    projects: list[str] = []

    def __init__(self, project: str, credentials: Any) -> None:
        type(self).projects.append(project)

    def query(self, sql: str) -> Any:
        type(self).executed.append(sql)
        return types.SimpleNamespace(result=lambda: None)


def test_run_setup_dry_run_and_execute_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry run only previews. Execute runs the DDL, then the load, and never writes an audit table.

    The execute path is driven against a recording stand-in, so this checks which statements
    are sent and in what order. It does not show that BigQuery accepts them.
    """
    expected_load = setup.generate_sample_load_statements("demo-proj", "fsi_capital_markets")
    preview = setup.run_setup(project_id="demo-proj", dry_run=True, load_sample=True)
    assert preview["status"] == "dry_run_ready"
    assert preview["sample_load_preview"] == expected_load
    assert setup.run_setup(project_id="demo-proj", dry_run=True)["sample_load_preview"] == []

    import google.auth
    import google.cloud

    fake_bigquery = types.ModuleType("google.cloud.bigquery")
    fake_bigquery.Client = _RecordingBigQueryClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)
    monkeypatch.setattr(google.cloud, "bigquery", fake_bigquery, raising=False)
    monkeypatch.setattr(google.auth, "default", lambda *args, **kwargs: (object(), "detected-proj"))
    monkeypatch.setattr(_RecordingBigQueryClient, "executed", [])
    monkeypatch.setattr(_RecordingBigQueryClient, "projects", [])

    ddl = setup.split_provisioning_statements(preview["ddl_preview"])
    assert len(ddl) == 7  # schema, five tables, property graph

    result = setup.run_setup(project_id="demo-proj", dry_run=False, load_sample=True)
    assert result["status"] == "provisioned" and result["sample_rows_loaded"] is True
    assert _RecordingBigQueryClient.projects == ["demo-proj"]
    assert _RecordingBigQueryClient.executed == ddl + expected_load
    writes = [sql for sql in _RecordingBigQueryClient.executed if sql.startswith(("DELETE", "INSERT"))]
    assert writes == expected_load
    assert not any(table in sql for sql in writes for table in _AUDIT_TABLES)

    monkeypatch.setattr(_RecordingBigQueryClient, "executed", [])
    result = setup.run_setup(project_id="demo-proj", dry_run=False)
    assert result["sample_rows_loaded"] is False
    assert _RecordingBigQueryClient.executed == ddl


def test_sensitivity_lookup_is_bundle_only() -> None:
    """An uncovered ticker is refused, and the module that holds the lookup cannot reach the network."""
    with pytest.raises(KeyError, match="not looked up at run time"):
        edgar_xbrl_mcp.lookup_company_xbrl_and_item7a("TSLA")
    source = Path(edgar_xbrl_mcp.__file__).read_text(encoding="utf-8")
    assert "urllib" not in source and "urlopen" not in source and "requests" not in source


def test_docs_state_what_the_bundled_dataset_holds() -> None:
    """The docs quote counts, tickers and a retrieval date. They must equal what the bundle holds."""
    from collections import Counter

    dataset = fsi_engine.load_sample_book()
    companies = dataset["companies"]
    links = dataset["supply_chain_edges"]
    statuses = {
        factor: Counter(company["sensitivities"][factor]["status"] for company in companies.values())
        for factor in ("rate", "oil")
    }
    not_converted = ("fair_value_only", "not_quantified", "not_convertible")
    retrieved_on = dataset["metadata"]["retrieval"]["retrieved_to_utc"][:10]

    readme = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")

    def table_row(label: str) -> tuple[int, int]:
        line = next(line for line in readme.splitlines() if line.startswith(f"| {label}"))
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        return int(cells[1].split()[0]), int(cells[2].split()[0])

    assert table_row("Annual income-statement sensitivity") == (
        statuses["rate"]["income_statement"],
        statuses["oil"]["income_statement"],
    )
    assert table_row("Disclosure quoted but not converted") == (
        sum(statuses["rate"][status] for status in not_converted),
        sum(statuses["oil"][status] for status in not_converted),
    )
    assert table_row("Market-risk section incorporated by reference") == (
        statuses["rate"]["incorporated_by_reference"],
        statuses["oil"]["incorporated_by_reference"],
    )
    assert table_row("No sentence found") == (statuses["rate"]["none_found"], statuses["oil"]["none_found"])
    assert (
        f"holds {len(companies)} issuers, {len(dataset['books'])} books, {len(dataset['scenarios'])} scenario "
        f"presets and {len(links)} disclosed"
    ) in readme
    with_share = sum(link["revenue_share_pct"] is not None for link in links)
    assert f"{with_share} of the {len(links)} links state a revenue share" in readme
    assert f"`EVAL.yaml` holds {len(eval_runner.load_cases())} cases" in readme
    assert f"retrieved from SEC EDGAR on {retrieved_on}" in readme
    derived = sum("derivation" in company["sensitivities"][factor] for company in companies.values() for factor in ("rate", "oil"))
    assert derived == 3 and "Three figures are computed from quoted ones" in readme

    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert f"retrieved from SEC EDGAR on {retrieved_on}" in skill
    assert (
        f"{statuses['rate']['income_statement']} of the {len(companies)} issuers state an annual income-statement "
        f"sensitivity to interest rates and {statuses['oil']['income_statement']} state one to fuel prices"
    ) in skill
    # Neither file may still call the data illustrative.
    for text in (readme, skill):
        assert "is illustrative" not in text and "labelled `illustrative_sample`" not in text

    playbook = (SKILL_ROOT / "references" / "item7a_sensitivity_rules.md").read_text(encoding="utf-8")
    for status in ("income_statement", "fair_value_only", "not_quantified", "not_convertible", "incorporated_by_reference"):
        line = next(line.strip() for line in playbook.splitlines() if line.strip().startswith(f"| `{status}`"))
        listed = set(re.findall(r"`([A-Z]{2,5})`", line.split("|")[3]))
        expected = {ticker for ticker, company in companies.items() if company["sensitivities"]["rate"]["status"] == status}
        assert listed == expected, status

    watcher_doc = (SKILL_ROOT / "references" / "filing_delta_audit.md").read_text(encoding="utf-8")
    bases = Counter(company["yoy_delta"]["basis"] for company in companies.values())
    for basis in ("filing_passage", "xbrl"):
        line = next(line for line in watcher_doc.splitlines() if line.startswith(f"| `{basis}`"))
        assert line.rstrip(" |").endswith(f"{bases[basis]} issuers"), basis


def test_after_tax_uses_a_usable_tax_rate_and_says_which_rate_it_used() -> None:
    """Intel's XBRL effective tax rate is 98.3% (pre-tax income 1,557, tax 1,531). It is not a marginal rate.

    Applied as one, a -48.50 pre-tax impact became -0.82 after tax. Outside 0% to 50% the engine now
    applies the US federal statutory rate and says so on the holding.
    """
    statutory, as_filed = fsi_engine.TAX_BASIS_STATUTORY, fsi_engine.TAX_BASIS_XBRL_EFFECTIVE
    analysis = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="25")
    holdings = {holding["ticker"]: holding for holding in analysis["holdings"]}
    intc, aapl = holdings["INTC"], holdings["AAPL"]

    assert intc["xbrl"]["effective_tax_rate"] == "0.983"  # the filed rate is still reported as filed
    impact = intc["scaled_impact"]
    assert (impact["total_pre_tax_m"], impact["tax_rate_applied"], impact["tax_rate_basis"]) == ("-48.50", "0.21", statutory)
    assert impact["after_tax_m"] == fsi_engine.q2(Decimal("-48.50") * (Decimal("1") - Decimal("0.21"))) == "-38.32"
    assert impact["eps_delta"] == fsi_engine.q4(Decimal("-38.315") / Decimal("4530")) == "-0.0085"
    # Reported diluted EPS is -0.06: a percentage of a negative base is not reported.
    assert impact["eps_pct_of_fy"] is None
    assert [note.split(":")[0] for note in intc["conversion_notes"]] == ["Tax", "EPS"]
    tax_note = intc["conversion_notes"][0]
    assert "statutory rate of 21%" in tax_note and "98.3%" in tax_note and "0% to 50%" in tax_note and "assumption of this tool" in tax_note

    # An ordinary rate is used as filed and carries no note.
    assert (aapl["scaled_impact"]["tax_rate_applied"], aapl["scaled_impact"]["tax_rate_basis"]) == ("0.17", as_filed)
    assert aapl["conversion_notes"] == [] and aapl["scaled_impact"]["eps_pct_of_fy"] == "-0.03"
    totals = analysis["portfolio_totals"]
    assert (totals["aggregate_pre_tax_impact_m"], totals["aggregate_after_tax_impact_m"]) == ("-96.00", "-77.75")

    # The notes are facts of the issuer: they are there whatever the scenario, also when nothing is shocked.
    quiet = {h["ticker"]: h for h in fsi_engine.analyze_book("megacap_tech", rate_bps="0", oil_pct="0")["holdings"]}
    assert quiet["INTC"]["conversion_notes"] == intc["conversion_notes"]
    # A holding with nothing to convert carries none, whatever its tax rate.
    assert holdings["MSFT"]["conversion_notes"] == []

    for rate, basis in (("0", as_filed), ("0.244", as_filed), ("0.5", as_filed), ("-0.01", statutory), ("0.501", statutory), ("0.983", statutory)):
        applied, found = fsi_engine.conversion_tax_rate(Decimal(rate))
        assert found == basis and applied == (Decimal(rate) if basis == as_filed else fsi_engine.US_FEDERAL_STATUTORY_TAX_RATE), rate
    # Of the bundled issuers only Intel is outside the range. A rebuilt bundle that moves another one out shows up here.
    companies = fsi_engine.load_sample_book()["companies"]
    outside = sorted(t for t, c in companies.items() if fsi_engine.conversion_tax_rate(Decimal(c["xbrl"]["effective_tax_rate"]))[1] == statutory)
    assert outside == ["INTC"]

    # The workbook: column J holds the rate that entered the formula, and the citation column says which rate it is.
    grid = model_exporter.build_formula_grid(analysis)
    row = next(row for row in grid["rows"] if row["ticker"] == "INTC")
    assert row["inputs"][f"J{row['excel_row']}"] == "0.21" and row["tax_rate_basis"] == statutory
    assert row["formulas"][f"K{row['excel_row']}"] == f"I{row['excel_row']}*(1-J{row['excel_row']})" and row["evaluated"]["after_tax_m"] == "-38.32"
    workbook, _ = model_exporter.export_workbook_bytes("megacap_tech", rate_bps="50", oil_pct="25")
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        sheets = b"".join(archive.read(name) for name in archive.namelist() if name.startswith("xl/worksheets/"))
    assert b"US federal statutory rate of 21%" in sheets and b"Tax Rate Applied" in sheets

    # The pasted-passage tool keeps the rate its caller gives and adds a note above the usable range.
    scaled = fsi_engine.scale_disclosed_sensitivity(
        disclosed_basis="100", disclosed_pre_tax_impact_m="-97", scenario_shock="50", effective_tax_rate="0.983", diluted_shares_m="4530"
    )
    assert scaled["after_tax_m"] == "-0.82" and any("98.3%" in note and "above 50%" in note for note in scaled["notes"])
    ordinary = fsi_engine.scale_disclosed_sensitivity(
        disclosed_basis="100", disclosed_pre_tax_impact_m="-97", scenario_shock="50", effective_tax_rate="0.21", diluted_shares_m="4530"
    )
    assert ordinary["notes"] == []
