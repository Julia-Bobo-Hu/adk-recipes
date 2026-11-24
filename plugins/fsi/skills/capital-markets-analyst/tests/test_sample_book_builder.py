"""Offline tests for the sample-book builder and its SEC EDGAR source layer.

No test here touches the network. The parser, the XBRL selection and the passage checks
run on small synthetic documents; the bundled dataset is compared with the builder's
specs so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import build_sample_book, edgar_source, fsi_engine  # noqa: E402

FILING_HTML = """<html><head><style>p {color: red}</style></head><body>
<ix:header><ix:hidden>HIDDEN FACT 999</ix:hidden></ix:header>
<div>ITEM 1. BUSINESS</div>
<p>We sell widgets&nbsp;to   Acme&#8217;s stores.</p>
<div>ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK</div>
<p>A 100 basis point <span>incr</span><span>ease</span> in rates would raise annual interest expense by $12 million.</p>
<table>
 <tr><th>Instrument</th><th>June 30,<br/>2026</th><th>Impact</th></tr>
 <tr><td>Term debt</td><td>$</td><td>95</td><td>(1,201</td><td>)</td><td>17</td><td>%</td></tr>
 <tr><td>Term debt</td><td>$</td><td>40</td></tr>
 <tr><td></td><td>&nbsp;</td></tr>
</table>
<div>ITEM 8. FINANCIAL STATEMENTS</div>
<p>A one cent increase in the cost of jet fuel per gallon would result in approximately $40 million of additional annual fuel expense.</p>
<p>Fuel prices were volatile and our costs increased during the year.</p>
</body></html>"""


def test_filing_html_becomes_comparable_text_and_table_rows() -> None:
    filing = edgar_source.parse_filing_html(FILING_HTML)

    # Tags, styles and the inline-XBRL header are gone; entities are decoded; whitespace is single.
    assert "HIDDEN FACT" not in filing.text and "color: red" not in filing.text
    assert "We sell widgets to Acme\u2019s stores." in filing.text
    # Inline tags inside a word do not split it.
    assert "A 100 basis point increase in rates would raise annual interest expense by $12 million." in filing.text
    assert "  " not in filing.text

    rows = [row["cells"] for row in filing.rows]
    assert rows == [
        ["Instrument", "June 30, 2026", "Impact"],
        ["Term debt", "$95", "(1,201)", "17%"],
        ["Term debt", "$40"],
    ]
    assert edgar_source.row_text(rows[1]) == "Term debt | $95 | (1,201) | 17%"
    offsets = [row["offset"] for row in filing.rows]
    assert offsets == sorted(offsets)
    assert filing.text.index("ITEM 7A.") < offsets[0] < filing.text.index("ITEM 8.")

    assert [row["cells"] for row in edgar_source.find_rows(filing, ["Term debt"])] == rows[1:]
    assert [row["cells"] for row in edgar_source.find_rows(filing, ["Term debt", "$40"])] == [rows[2]]
    # Cells must appear in the given order.
    assert edgar_source.find_rows(filing, ["$95", "Term debt"]) == []
    assert edgar_source.find_rows(filing, ["Term debt"], after_offset=offsets[2]) == [filing.rows[2]]
    assert edgar_source.merge_split_cells(["$", "(13,653", ")", "$ 5"]) == ["$(13,653)", "$5"]


def _facts() -> dict[str, Any]:
    def row(accn: str, end: str, val: Any, start: str | None = None) -> dict[str, Any]:
        out = {"accn": accn, "end": end, "val": val, "fy": 2025}
        if start:
            out["start"] = start
        return out

    return {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            row("A-2", "2025-12-31", 1000, "2025-01-01"),
                            row("A-2", "2024-12-31", 900, "2024-01-01"),  # comparative column
                            row("A-2", "2025-12-31", 260, "2025-10-01"),  # a quarter, not the year
                            row("A-1", "2025-12-31", 1111, "2025-01-01"),  # another filing
                        ]
                    }
                },
                "SalesRevenueNet": {"units": {"USD": [row("A-2", "2025-12-31", 990, "2025-01-01")]}},
                "LongTermDebtNoncurrent": {
                    "units": {
                        "USD": [
                            row("A-2", "2025-12-31", 500),
                            row("A-2", "2024-12-31", 650),
                            row("A-2", "2023-12-31", 700),
                        ]
                    }
                },
                "Conflicted": {
                    "units": {"USD": [row("A-2", "2025-12-31", 1, "2025-01-01"), row("A-2", "2025-12-31", 2, "2025-01-01")]}
                },
            }
        }
    }


def test_xbrl_facts_are_selected_for_one_accession_and_period_only() -> None:
    facts = _facts()
    select = edgar_source.select_fact

    revenue = select(facts, ["Missing", "Revenues", "SalesRevenueNet"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=True)
    assert revenue == {
        "tag": "us-gaap:Revenues",
        "unit": "USD",
        "value": "1000",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "fiscal_year": 2025,
        "accession_number": "A-2",
    }
    # Tag order is the priority order.
    assert select(facts, ["SalesRevenueNet", "Revenues"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=True)["value"] == "990"
    # A value from another filing, another year or another unit is never substituted.
    assert select(facts, ["Revenues"], "USD", accession="A-3", period_end="2025-12-31", annual_duration=True) is None
    assert select(facts, ["Revenues"], "USD", accession="A-2", period_end="2026-12-31", annual_duration=True) is None
    assert select(facts, ["Revenues"], "shares", accession="A-2", period_end="2025-12-31", annual_duration=True) is None
    # A duration fact is not a balance-sheet fact and the reverse.
    assert select(facts, ["Revenues"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=False) is None
    assert select(facts, ["LongTermDebtNoncurrent"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=True) is None
    debt = select(facts, ["LongTermDebtNoncurrent"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=False)
    assert debt["value"] == "500" and debt["period_start"] is None
    assert (
        edgar_source.prior_instant_end(facts, "us-gaap:LongTermDebtNoncurrent", "USD", accession="A-2", period_end="2025-12-31")
        == "2024-12-31"
    )
    with pytest.raises(ValueError, match="reports"):
        select(facts, ["Conflicted"], "USD", accession="A-2", period_end="2025-12-31", annual_duration=True)

    assert edgar_source.has_accession_facts(facts, "A-2") and not edgar_source.has_accession_facts(facts, "A-9")
    assert edgar_source.to_millions("63364000000") == "63364"
    assert edgar_source.to_millions("122132000") == "122.132"
    assert edgar_source.decimal_text(Decimal("0.240")) == "0.24"
    assert edgar_source.decimal_text(Decimal("-0.0")) == "0"


INSTANCE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2026"
      xmlns:dei="http://xbrl.sec.gov/dei/2026" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
  <context id="fy"><entity><identifier scheme="x">1</identifier></entity>
    <period><startDate>2025-07-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="segment"><entity><identifier scheme="x">1</identifier>
    <segment><xbrldi:explicitMember dimension="d">m</xbrldi:explicitMember></segment></entity>
    <period><startDate>2025-07-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="now"><entity><identifier scheme="x">1</identifier></entity><period><instant>2026-06-30</instant></period></context>
  <unit id="usd"><measure>iso4217:USD</measure></unit>
  <unit id="shares"><measure>shares</measure></unit>
  <unit id="eps"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator>
    <unitDenominator><measure>shares</measure></unitDenominator></divide></unit>
  <unit id="number"><measure>pure</measure></unit>
  <dei:DocumentFiscalYearFocus contextRef="fy">2026</dei:DocumentFiscalYearFocus>
  <us-gaap:Revenues contextRef="fy" unitRef="usd" decimals="-6">6720000000</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="segment" unitRef="usd" decimals="-6">2000000000</us-gaap:Revenues>
  <us-gaap:EarningsPerShareDiluted contextRef="fy" unitRef="eps" decimals="2">4.81</us-gaap:EarningsPerShareDiluted>
  <us-gaap:EffectiveIncomeTaxRateContinuingOperations contextRef="fy" unitRef="number" decimals="3">0.240</us-gaap:EffectiveIncomeTaxRateContinuingOperations>
  <us-gaap:LongTermDebtNoncurrent contextRef="now" unitRef="usd" decimals="-6">3981000000</us-gaap:LongTermDebtNoncurrent>
</xbrl>"""


def test_xbrl_instance_is_read_like_companyfacts() -> None:
    facts = edgar_source.instance_to_company_facts(INSTANCE_XML, "ACC-1")
    kwargs = {"accession": "ACC-1", "period_end": "2026-06-30"}

    revenue = edgar_source.select_fact(facts, ["Revenues"], "USD", annual_duration=True, **kwargs)
    # The segment-level value is ignored; only the entity-wide fact counts.
    assert (revenue["value"], revenue["fiscal_year"]) == ("6720000000", 2026)
    assert edgar_source.select_fact(facts, ["EarningsPerShareDiluted"], "USD/shares", annual_duration=True, **kwargs)["value"] == "4.81"
    rate = edgar_source.select_fact(facts, ["EffectiveIncomeTaxRateContinuingOperations"], "pure", annual_duration=True, **kwargs)
    assert edgar_source.decimal_text(Decimal(rate["value"])) == "0.24"
    assert edgar_source.select_fact(facts, ["LongTermDebtNoncurrent"], "USD", annual_duration=False, **kwargs)["value"] == "3981000000"
    assert edgar_source.has_accession_facts(facts, "ACC-1")


def test_cache_never_fetches_when_offline_or_without_a_declared_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the network must not be used in this test")

    monkeypatch.setattr(edgar_source, "urlopen", no_network)
    (tmp_path / "filings").mkdir()
    (tmp_path / "filings" / "ABC_0000000000-26-000001.htm").write_bytes(b"<p>cached</p>")

    offline = edgar_source.EdgarCache(tmp_path, offline=True)
    payload, url, retrieved = offline.primary_document("ABC", "0000001234", "0000000000-26-000001", "abc.htm")
    assert payload == b"<p>cached</p>"
    assert url == "https://www.sec.gov/Archives/edgar/data/1234/000000000026000001/abc.htm"
    assert retrieved.endswith("Z") and offline.network_requests == 0
    with pytest.raises(FileNotFoundError, match="not cached"):
        offline.submissions("ABC", "0000001234")

    online = edgar_source.EdgarCache(tmp_path)
    for value in (
        "",
        "no contact address",
        "ExampleInstitution ResearchOps@example.com",
        "Your Organisation contact@your-domain",
    ):
        monkeypatch.setenv(edgar_source.USER_AGENT_ENV, value)
        with pytest.raises(RuntimeError, match=edgar_source.USER_AGENT_ENV):
            online.submissions("ABC", "0000001234")
    assert online.network_requests == 0


def test_passage_specs_are_rejected_unless_the_filing_really_says_so() -> None:
    filing = edgar_source.parse_filing_html(FILING_HTML)
    heading = "ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK"
    sentence = "A 100 basis point increase in rates would raise annual interest expense by $12 million."
    sent, row, resolve = build_sample_book.sent, build_sample_book.row, build_sample_book.resolve_passage

    assert resolve("ABC", filing, sent(sentence, "Item 7A", (heading, 200))) == {
        "kind": "sentence",
        "section": "Item 7A",
        "text": sentence,
    }
    assert resolve("ABC", filing, row(["Term debt", "$95"], "Item 7A", (heading, 400)))["text"] == (
        "Term debt | $95 | (1,201) | 17%"
    )

    failures = {
        "is not in the filing text": sent(sentence.replace("$12", "$13"), "Item 7A", (heading, 200)),
        "does not precede": sent(sentence, "Item 9", ("ITEM 9. CONTROLS", 200)),
        "characters after": sent(sentence, "Item 1", ("ITEM 1. BUSINESS", 20)),
        "occurs between": sent(sentence, "Item 1", ("ITEM 1. BUSINESS", 5000), not_between=("ITEM 7A.",)),
        "2 table rows match": row(["Term debt"], "Item 7A", (heading, 400)),
        "0 table rows match": row(["Term debt", "$96"], "Item 7A", (heading, 400)),
        "has no section anchor": sent(sentence, "Item 7A"),
    }
    for message, spec in failures.items():
        with pytest.raises(build_sample_book.BuildError, match=message):
            resolve("ABC", filing, spec)


def test_fuel_screen_and_change_text() -> None:
    filing = edgar_source.parse_filing_html(FILING_HTML)
    sentence = (
        "A one cent increase in the cost of jet fuel per gallon would result in approximately $40 million of "
        "additional annual fuel expense."
    )
    # One hit: the quantified sentence (with the heading in front of it, which has no full
    # stop). The sentence that only says fuel costs rose is not a sensitivity.
    hits = build_sample_book.fuel_sensitivity_sentences(filing.text)
    assert len(hits) == 1 and hits[0].endswith(sentence)
    build_sample_book.check_fuel_screen("ABC", filing, {"status": "income_statement", "passages": [{"text": sentence}]})
    for claimed in ({"status": "none_found"}, {"status": "income_statement", "passages": [{"text": "something else"}]}):
        with pytest.raises(build_sample_book.BuildError, match="fuel screen"):
            build_sample_book.check_fuel_screen("ABC", filing, claimed)

    change = build_sample_book._change_text
    assert change(Decimal("129"), Decimal("95"), "usd_m") == "Down $34 million (-26.4%)."
    assert change(Decimal("600"), Decimal("694"), "usd_m") == "Up $94 million (+15.7%)."
    assert change(Decimal("15"), Decimal("13"), "pct_share") == "Down 2 percentage points."
    assert change(Decimal("27"), Decimal("26"), "pct_share") == "Down 1 percentage point."
    assert change(Decimal("16"), Decimal("16"), "pct_share") == "Unchanged."


def test_bundled_dataset_matches_the_builder_specs() -> None:
    """The shipped JSON holds exactly the issuers, passages and links the builder specifies."""
    dataset = fsi_engine.load_sample_book()
    assert dataset["metadata"]["dataset_id"] == build_sample_book.DATASET_ID
    assert dataset["books"] == build_sample_book.BOOKS
    assert dataset["scenarios"] == build_sample_book.SCENARIOS
    assert list(dataset["companies"]) == list(build_sample_book.ISSUERS)

    for ticker, issuer in build_sample_book.ISSUERS.items():
        company = dataset["companies"][ticker]
        assert company["filing"]["accession_number"] == issuer["accession"], ticker
        assert company["cik"] == issuer["cik"], ticker
        for factor in ("rate", "oil"):
            spec = build_sample_book.SENSITIVITIES[ticker][factor]
            record = company["sensitivities"][factor]
            assert record["status"] == spec["status"], (ticker, factor)
            assert [p["kind"] for p in record.get("passages", [])] == [p["kind"] for p in spec.get("passages", [])]
            for stored, wanted in zip(record.get("passages", []), spec.get("passages", [])):
                if wanted["kind"] == "sentence":
                    assert stored["text"] == wanted["text"], (ticker, factor)
                else:
                    cells = stored["text"].split(" | ")
                    position = 0
                    for cell in wanted["cells"]:
                        position = cells.index(cell, position) + 1
            for needle in spec.get("expect", []):
                assert any(needle in p["text"] for p in record["passages"]) or needle in "\n".join(
                    p["text"] for p in record["passages"]
                ), (ticker, factor, needle)
            if "components" in record:
                assert sum(Decimal(c["amount_m"]) for c in record["components"]) == Decimal(record["pre_tax_impact_m"])

    edges = {edge["edge_id"]: edge for edge in dataset["supply_chain_edges"]}
    assert list(edges) == [spec["edge_id"] for spec in build_sample_book.EDGES]
    for spec in build_sample_book.EDGES:
        edge = edges[spec["edge_id"]]
        assert edge["verbatim_quote"] == spec["passage"]["text"]
        assert (edge["src"], edge["dst"], edge["revenue_share_pct"]) == (spec["src"], spec["dst"], spec["revenue_share_pct"])
        assert edge["doc_id"] == dataset["companies"][spec["cited_in"]]["filing_doc_id"]

    # Timestamps are the only thing two builds of the same filings may differ in.
    later = json.loads(json.dumps(dataset))
    later["metadata"]["as_of_utc"] = "2030-01-01T00:00:00Z"
    later["companies"]["DAL"]["filing"]["retrieved_at_utc"] = "2030-01-01T00:00:00Z"
    strip = build_sample_book.without_retrieval_times
    assert strip(later) == strip(dataset)
    later["companies"]["DAL"]["xbrl"]["revenue_m"] = "1"
    assert strip(later) != strip(dataset)
