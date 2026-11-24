"""Reported annual figures: the bundled records, the SEC company-facts reader, the lookup and the tool.

No test here touches the network. The reader is exercised on small hand-written company
facts whose expected values are worked out in the comments; the lookup gets a stand-in
for the HTTP GET.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import edgar_source, fsi_engine, reported_financials as rf, sec_lookup, tools  # noqa: E402

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
CONTACT = "Real Organisation analyst@real-organisation.org"


def _row(end: str, val: Any, accn: str, filed: str, *, start: str | None = None, form: str = "10-K", fy: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": fy or int(filed[:4]) - 1}
    if start:
        row["start"] = start
    return row


def _facts(tags: dict[str, tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    return {"entityName": "Sample Co", "facts": {"us-gaap": {tag: {"units": {unit: rows}} for tag, (unit, rows) in tags.items()}}}


def _year(year: int) -> tuple[str, str]:
    return f"{year}-01-01", f"{year}-12-31"


def sample_company_facts() -> dict[str, Any]:
    """Two Form 10-K filings. The later one restates 2024 revenue and switches the revenue tag.

    A-25 (filed 2025-02-10) reports 2024 and 2023; A-26 (filed 2026-02-10) reports 2025 and 2024.
    """
    (s23, e23), (s24, e24), (s25, e25) = _year(2023), _year(2024), _year(2025)
    a25, a26 = ("0000000001-25-000001", "2025-02-10"), ("0000000001-26-000001", "2026-02-10")
    return _facts(
        {
            # Old tag, used by the earlier filing only.
            "SalesRevenueNet": ("USD", [_row(e23, 800_000_000, *a25, start=s23), _row(e24, 1_000_000_000, *a25, start=s24)]),
            # New tag: 2024 restated from 1,000 to 900; a quarter and a 10-Q row must be ignored.
            "RevenueFromContractWithCustomerExcludingAssessedTax": (
                "USD",
                [
                    _row(e24, 900_000_000, *a26, start=s24),
                    _row(e25, 1_200_000_000, *a26, start=s25),
                    _row(e25, 310_000_000, *a26, start="2025-10-01"),
                    _row(e25, 9_999_000_000, "0000000001-26-000009", "2026-04-30", start=s25, form="10-Q"),
                ],
            ),
            "NetIncomeLoss": (
                "USD",
                [
                    _row(e23, -50_000_000, *a25, start=s23),
                    _row(e24, 100_000_000, *a25, start=s24),
                    _row(e24, 100_000_000, *a26, start=s24),
                    _row(e25, 150_000_000, *a26, start=s25),
                ],
            ),
            "OperatingIncomeLoss": ("USD", [_row(e24, 180_000_000, *a26, start=s24), _row(e25, 300_000_000, *a26, start=s25)]),
            "EarningsPerShareDiluted": ("USD/shares", [_row(e24, Decimal("1.0"), *a26, start=s24), _row(e25, Decimal("1.5"), *a26, start=s25)]),
            "NetCashProvidedByUsedInOperatingActivities": ("USD", [_row(e25, 400_000_000, *a26, start=s25)]),
            "PaymentsToAcquirePropertyPlantAndEquipment": ("USD", [_row(e25, 150_000_000, *a26, start=s25)]),
            "Assets": ("USD", [_row(e24, 2_000_000_000, *a25), _row(e24, 2_000_000_000, *a26), _row(e25, 2_500_000_000, *a26)]),
            # The latest filing gives two different values for one date: the fact is left out, not guessed.
            "Liabilities": ("USD", [_row(e25, 1_000_000_000, *a26), _row(e25, 1_100_000_000, *a26)]),
            "EffectiveIncomeTaxRateContinuingOperations": ("pure", [_row(e25, Decimal("0.215"), *a26, start=s25)]),
        }
    )


def sample_record(**overrides: Any) -> dict[str, Any]:
    arguments = {"ticker": "SMPL", "name": "Sample Co", "cik": "0000000001", "source": "test", "retrieved_at_utc": "2026-11-22T00:00:00Z"}
    return rf.build_record(sample_company_facts(), **{**arguments, **overrides})


# --------------------------------------------------------------------------- the bundled records


def test_bundle_matches_the_sample_book_and_its_own_hashes() -> None:
    bundle, book = rf.load_bundle(), fsi_engine.load_sample_book()
    assert sorted(bundle["companies"]) == sorted(book["companies"])
    assert bundle["metadata"]["source_posture"] == fsi_engine.POSTURE_AUTHORITATIVE
    # The file is in the canonical layout, so a rebuilt figure is a one-line diff.
    assert rf.BUNDLE_PATH.read_text(encoding="utf-8") == rf.dumps_bundle(bundle)

    for ticker, record in bundle["companies"].items():
        assert rf.checked_record(record, ticker) is record
        ends = [year["period_end"] for year in record["fiscal_years"]]
        assert len(ends) == rf.KEEP_FISCAL_YEARS and ends == sorted(ends, reverse=True), ticker
        # Same value and same XBRL tag as the five figures the sample book already holds.
        rf._same_as_sample_book(ticker, record, book["companies"][ticker])
        for key, entry in record["items"].items():
            assert key in rf.ITEMS_BY_KEY and entry["tag"].startswith("us-gaap:"), (ticker, key)
            for end, value in entry["values"].items():
                assert end in ends and value["accession_number"] in record["filings"], (ticker, key, end)
                Decimal(value["value"])

    tampered = json.loads(json.dumps(bundle["companies"]["AAPL"]))
    tampered["items"]["revenue"]["values"][tampered["fiscal_years"][0]["period_end"]]["value"] = "1"
    with pytest.raises(rf.RecordIntegrityError):
        rf.checked_record(tampered, "AAPL")


# --------------------------------------------------------------------------- reading company facts


def test_reader_takes_the_latest_filing_marks_restatements_and_ignores_other_periods() -> None:
    record = sample_record()
    assert [(year["fiscal_year"], year["period_end"]) for year in record["fiscal_years"]] == [
        (2025, "2025-12-31"),
        (2024, "2024-12-31"),
        (2023, "2023-12-31"),
    ]
    assert record["fiscal_years"][0]["first_reported_in"] == "0000000001-26-000001"
    assert record["fiscal_years"][1]["first_reported_in"] == "0000000001-25-000001"

    revenue = record["items"]["revenue"]
    assert revenue["tag"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    # 2025: the full-year fact of the 10-K; the quarter and the 10-Q row are ignored.
    assert revenue["values"]["2025-12-31"] == {"value": "1200", "accession_number": "0000000001-26-000001"}
    # 2024: the later filing's 900 under the tag of the latest year. The earlier 1,000 was under another tag,
    # so it is not a restatement of the same fact.
    assert revenue["values"]["2024-12-31"] == {"value": "900", "accession_number": "0000000001-26-000001"}
    # 2023: only the old tag reports it, and the value says so.
    assert revenue["values"]["2023-12-31"] == {
        "value": "800",
        "accession_number": "0000000001-25-000001",
        "tag": "us-gaap:SalesRevenueNet",
    }
    # Net income for 2024 is reported twice with the same value: latest filing, no restatement mark.
    assert record["items"]["net_income"]["values"]["2024-12-31"] == {"value": "100", "accession_number": "0000000001-26-000001"}
    # Per-share amounts keep two decimals; a rate stays a fraction; shares and USD are in millions.
    assert record["items"]["diluted_eps"]["values"]["2025-12-31"]["value"] == "1.50"
    assert record["items"]["effective_tax_rate"]["values"]["2025-12-31"]["value"] == "0.215"
    # Two values for one date in the latest filing: left out.
    assert "total_liabilities" not in record["items"]
    assert record["filings"] == {
        "0000000001-25-000001": {"form": "10-K", "filed": "2025-02-10"},
        "0000000001-26-000001": {"form": "10-K", "filed": "2026-02-10"},
    }
    assert record["facts_sha256"] == rf.record_sha256(record) and record["source_posture"] == "authoritative_filing"

    with pytest.raises(rf.NoAnnualFacts):
        rf.build_record({"facts": {"ifrs-full": {}}}, ticker="X", name="X", cik="1", source="t", retrieved_at_utc="t")


def test_restated_value_and_fiscal_year_label_come_from_the_filings() -> None:
    (s24, e24), (s25, e25) = _year(2024), _year(2025)
    facts = _facts(
        {
            "Revenues": (
                "USD",
                [
                    _row(e24, 1_000_000_000, "A-25", "2025-02-10", start=s24),
                    _row(e24, 950_000_000, "A-26", "2026-02-10", start=s24),
                    _row(e25, 1_100_000_000, "A-26", "2026-02-10", start=s25),
                ],
            )
        }
    )
    record = rf.build_record(facts, ticker="R", name="R", cik="2", source="t", retrieved_at_utc="t")
    assert record["items"]["revenue"]["values"][e24] == {"value": "950", "accession_number": "A-26", "restated_from": "1000"}

    # A retailer whose year ends 2026-01-31 calls it fiscal 2025; the filing's own number is kept.
    retailer = _facts({"Revenues": ("USD", [_row("2026-01-31", 5_000_000, "T-26", "2026-03-11", start="2025-02-02", fy=2025)])})
    year = rf.build_record(retailer, ticker="T", name="T", cik="3", source="t", retrieved_at_utc="t")["fiscal_years"][0]
    assert (year["fiscal_year"], year["period_start"], year["period_end"]) == (2025, "2025-02-02", "2026-01-31")


def test_computed_metrics_are_exact_and_say_why_one_is_missing() -> None:
    record = sample_record()
    values, reasons = rf.computed_metrics(record, "2025-12-31", "2024-12-31")
    # revenue 1,200 vs 900; operating income 300 vs 180; net income 150 vs 100; EPS 1.50 vs 1.00.
    assert values["revenue_change"] == "300" and values["revenue_growth_pct"] == "33.33"
    assert values["operating_income_growth_pct"] == "66.67"
    assert values["net_income_change"] == "50" and values["net_income_growth_pct"] == "50.00"
    assert values["diluted_eps_growth_pct"] == "50.00"
    assert values["operating_margin_pct"] == "25.00" and values["net_margin_pct"] == "12.50"
    assert values["effective_tax_rate_pct"] == "21.50"
    # 400 - 150 = 250; 250 / 1,200 = 20.83%.
    assert values["free_cash_flow"] == "250" and values["free_cash_flow_margin_pct"] == "20.83"
    assert reasons["gross_margin_pct"] == "gross_profit is not tagged"
    assert set(values) | set(reasons) == {metric.key for metric in rf.COMPUTED_METRICS} and not set(values) & set(reasons)

    # 2024 against 2023: revenue changes tag, and net income was negative the year before.
    values, reasons = rf.computed_metrics(record, "2024-12-31", "2023-12-31")
    assert "revenue_growth_pct" not in values and "different XBRL tags" in reasons["revenue_growth_pct"]
    assert values["net_income_change"] == "150" and "not meaningful" in reasons["net_income_growth_pct"]
    assert reasons["free_cash_flow"] == "operating_cash_flow is not tagged"

    # The oldest year has no prior year in the record.
    _, reasons = rf.computed_metrics(record, "2023-12-31", None)
    assert reasons["revenue_growth_pct"] == "the record holds no prior fiscal year"


def test_a_ratio_is_not_computed_across_a_restatement() -> None:
    """Inputs from different filings, one of them restated, may be on different bases."""
    (s23, e23), (s24, e24), (s25, e25) = _year(2023), _year(2024), _year(2025)
    facts = _facts(
        {
            "Revenues": (
                "USD",
                [
                    _row(e23, 500_000_000, "A-24", "2024-02-10", start=s23),
                    _row(e23, 400_000_000, "A-25", "2025-02-10", start=s23),  # 2023 recast in the next filing
                    _row(e24, 600_000_000, "A-25", "2025-02-10", start=s24),
                    _row(e24, 600_000_000, "A-26", "2026-02-10", start=s24),
                    _row(e25, 660_000_000, "A-26", "2026-02-10", start=s25),
                ],
            )
        }
    )
    record = rf.build_record(facts, ticker="G", name="G", cik="4", source="t", retrieved_at_utc="t")
    # 2025 and 2024 both come from A-26: computed.
    assert rf.computed_metrics(record, e25, e24)[0]["revenue_growth_pct"] == "10.00"
    # 2024 comes from A-26, 2023 from A-25 where it was restated: not computed.
    values, reasons = rf.computed_metrics(record, e24, e23)
    assert "revenue_growth_pct" not in values and reasons["revenue_growth_pct"] == rf.MIXED_BASIS_REASON


def test_a_filing_missing_from_company_facts_can_be_merged_in() -> None:
    facts = sample_company_facts()
    (s26, e26) = _year(2026)
    instance = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [{"start": s26, "end": e26, "val": Decimal("175000000"), "accn": "A-27", "fy": 2026}]}}}}}
    merged = rf.merge_filing_facts(facts, instance, form="10-K", filed="2027-02-10")
    record = rf.build_record(merged, ticker="M", name="M", cik="5", source="t", retrieved_at_utc="t")
    assert record["fiscal_years"][0]["period_end"] == e26
    assert record["items"]["net_income"]["values"][e26] == {"value": "175", "accession_number": "A-27"}
    assert record["filings"]["A-27"] == {"form": "10-K", "filed": "2027-02-10"}
    # The original facts are not modified.
    assert all(row["accn"] != "A-27" for row in facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"])


# --------------------------------------------------------------------------- views and the tool


@pytest.fixture()
def sealed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Lookups off and an empty store, so only the bundle answers."""
    monkeypatch.setenv(sec_lookup.LIVE_LOOKUP_ENV, "false")
    monkeypatch.setenv(sec_lookup.STORE_DIR_ENV, str(tmp_path / "store"))
    return tmp_path / "store"


def test_tool_returns_figures_sources_comparison_and_structured_refusals(sealed: Path) -> None:
    single = tools.call_tool("get_reported_financials", {"tickers": ["aapl"]})
    assert single["status"] == "ok" and "comparison" not in single and single["not_available"] == []
    company = single["companies"][0]
    assert (company["ticker"], company["data_origin"], company["source_posture"]) == ("AAPL", "bundled", "authoritative_filing")
    assert len(company["fiscal_years"]) == 3
    latest = company["fiscal_years"][0]
    book = fsi_engine.load_sample_book()["companies"]["AAPL"]
    assert latest["period_end"] == book["filing"]["report_date"]
    assert Decimal(latest["reported"]["revenue"]["value"]) == Decimal(book["xbrl"]["revenue_m"])
    assert latest["reported"]["revenue"]["accession_number"] == book["filing"]["accession_number"]
    assert company["tags"]["revenue"] == book["xbrl_sources"]["revenue_m"]["tag"]
    # Every cited filing is described, every item is either reported or listed as not tagged.
    assert {cell["accession_number"] for year in company["fiscal_years"] for cell in year["reported"].values()} == set(company["filings"])
    assert set(latest["reported"]) | set(latest["not_tagged"]) == set(rf.ITEMS_BY_KEY)
    assert set(single["line_items"]) == set(rf.ITEMS_BY_KEY)
    assert set(single["computed_metrics"]) == {metric.key for metric in rf.COMPUTED_METRICS}
    # What goes back to the model stays small.
    assert len(json.dumps(single)) < 16_000

    peers = tools.call_tool("get_reported_financials", {"tickers": ["DAL", " ual ", "dal", "TSLA", "../etc"], "fiscal_years": 1})
    assert [company["ticker"] for company in peers["companies"]] == ["DAL", "UAL"]
    assert [row["ticker"] for row in peers["comparison"]["rows"]] == ["DAL", "UAL"]
    row = peers["comparison"]["rows"][0]
    assert row["gross_margin_pct"] is None and Decimal(row["operating_margin_pct"]) > 0  # an airline tags no gross profit
    assert {entry["ticker"]: entry["reason"] for entry in peers["not_available"]} == {
        "TSLA": sec_lookup.REASON_LIVE_OFF,
        "../ETC": sec_lookup.REASON_INVALID_TICKER,
    }
    six = tools.call_tool("get_reported_financials", {"tickers": ["AAPL", "MSFT", "NVDA", "AMZN", "JPM", "GE"]})
    assert len(six["companies"]) == 6 and len(json.dumps(six)) < 64_000

    for bad, code in (
        ({"tickers": []}, "invalid_input"),
        ({"tickers": ["A", "B", "C", "D", "E", "F", "G"]}, "invalid_input"),
        ({"tickers": ["AAPL"], "fiscal_years": 0}, "invalid_input"),
        ({"tickers": ["AAPL"], "fiscal_years": 4}, "invalid_input"),
        ({"tickers": ["TSLA"]}, "not_available"),
    ):
        error = tools.call_tool("get_reported_financials", bad)
        assert (error["status"], error["error_code"]) == ("error", code), bad
    assert not sealed.exists()  # nothing was written while lookups were off


# --------------------------------------------------------------------------- the SEC lookup


class FakeSec:
    """Stand-in for the HTTP GET: serves a ticker list and company facts, and counts requests."""

    def __init__(self, facts: dict[str, Any] | Exception) -> None:
        self.facts = facts
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if url == sec_lookup.TICKERS_URL:
            return json.dumps({"0": {"cik_str": 1, "ticker": "SMPL", "title": "SAMPLE CO /DE/"}}).encode()
        if isinstance(self.facts, Exception):
            raise self.facts
        return json.dumps(self.facts, default=str).encode()


def test_lookup_reads_sec_only_when_allowed_and_keeps_the_reduced_record(sealed: Path) -> None:
    sec = FakeSec(sample_company_facts())
    with pytest.raises(sec_lookup.LookupUnavailable) as refused:
        sec_lookup.lookup("smpl", fetch=sec, now=NOW)
    assert refused.value.reason == sec_lookup.REASON_LIVE_OFF and sec.urls == []

    record, origin = sec_lookup.lookup("smpl", fetch=sec, live=True, now=NOW)
    assert origin == sec_lookup.ORIGIN_LIVE and record["name"] == "SAMPLE CO /DE/" and record["cik"] == "0000000001"
    assert sec.urls == [sec_lookup.TICKERS_URL, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"]
    assert record["retrieved_at_utc"] == "2026-11-22T12:00:00Z"
    stored = json.loads((sealed / "SMPL.json").read_text(encoding="utf-8"))
    assert stored == record and "facts" not in stored  # the reduced record, not SEC's payload

    # Within a day the stored record answers, with or without live lookups; no request is made.
    for live in (True, False):
        again, origin = sec_lookup.lookup("SMPL", fetch=sec, live=live, now=NOW + timedelta(hours=23))
        assert origin == sec_lookup.ORIGIN_STORE and again == record
    assert len(sec.urls) == 2
    # After a day, with lookups on, SEC is asked again (the ticker list is kept for a week).
    _, origin = sec_lookup.lookup("SMPL", fetch=sec, live=True, now=NOW + timedelta(hours=25))
    assert origin == sec_lookup.ORIGIN_LIVE and len(sec.urls) == 3
    # With lookups off an old record still answers, and shows when it was retrieved.
    old, origin = sec_lookup.lookup("SMPL", fetch=sec, live=False, now=NOW + timedelta(days=90))
    assert origin == sec_lookup.ORIGIN_STORE and old["retrieved_at_utc"] == "2026-11-23T13:00:00Z"

    # A bundled company never reaches SEC.
    bundled, origin = sec_lookup.lookup("aapl", fetch=sec, live=True, now=NOW)
    assert origin == sec_lookup.ORIGIN_BUNDLED and bundled["ticker"] == "AAPL" and len(sec.urls) == 3


def test_lookup_failures_have_a_reason_and_a_damaged_record_is_refused(sealed: Path) -> None:
    def reason(ticker: str, sec: FakeSec, **kwargs: Any) -> str:
        with pytest.raises(sec_lookup.LookupUnavailable) as refused:
            sec_lookup.lookup(ticker, fetch=sec, live=True, now=NOW, **kwargs)
        return refused.value.reason

    for text in ("", "../x", "TOOLONGTICKER", "1ABC", "A B", "A/B"):
        assert reason(text, FakeSec(sample_company_facts())) == sec_lookup.REASON_INVALID_TICKER
    assert reason("NOPE", FakeSec(sample_company_facts())) == sec_lookup.REASON_UNKNOWN_TICKER
    assert reason("SMPL", FakeSec({"facts": {"ifrs-full": {}}})) == sec_lookup.REASON_NO_FACTS
    not_found = HTTPError("https://data.sec.gov/x", 404, "Not Found", None, io.BytesIO())  # type: ignore[arg-type]
    assert reason("SMPL", FakeSec(not_found)) == sec_lookup.REASON_NO_FACTS
    assert reason("SMPL", FakeSec(URLError("timed out"))) == sec_lookup.REASON_SEC_UNAVAILABLE
    assert reason("SMPL", FakeSec(RuntimeError("Set SEC_EDGAR_USER_AGENT"))) == sec_lookup.REASON_NO_CONTACT
    assert not (sealed / "SMPL.json").exists()

    # When SEC cannot be reached, an older stored record is returned rather than nothing.
    record, _ = sec_lookup.lookup("SMPL", fetch=FakeSec(sample_company_facts()), live=True, now=NOW)
    later = NOW + timedelta(days=3)
    stale, origin = sec_lookup.lookup("SMPL", fetch=FakeSec(URLError("down")), live=True, now=later)
    assert origin == sec_lookup.ORIGIN_STORE and stale == record

    # A stored record that no longer matches its hash is refused, live or not.
    path = sealed / "SMPL.json"
    damaged = json.loads(path.read_text(encoding="utf-8"))
    damaged["items"]["revenue"]["values"]["2025-12-31"]["value"] = "999999"
    path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(sec_lookup.LookupUnavailable) as refused:
        sec_lookup.lookup("SMPL", fetch=FakeSec(sample_company_facts()), live=False, now=NOW)
    assert refused.value.reason == sec_lookup.REASON_BAD_RECORD


def test_no_request_leaves_without_a_declared_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the network must not be used in this test")

    monkeypatch.setattr(sec_lookup, "urlopen", no_network)
    for contact in ("", "no contact address", "ExampleInstitution ResearchOps@example.com"):
        monkeypatch.setenv(edgar_source.USER_AGENT_ENV, contact)
        with pytest.raises(RuntimeError, match=edgar_source.USER_AGENT_ENV):
            sec_lookup.http_get(sec_lookup.TICKERS_URL)
    monkeypatch.setenv(edgar_source.USER_AGENT_ENV, CONTACT)
    assert edgar_source.declared_user_agent() == CONTACT
    for value, expected in (("", False), ("0", False), ("false", False), ("1", True), ("TRUE", True), ("on", True)):
        monkeypatch.setenv(sec_lookup.LIVE_LOOKUP_ENV, value)
        assert sec_lookup.live_lookup_enabled() is expected


def test_a_growth_rate_is_not_reported_across_a_sign_change_or_on_a_base_too_small() -> None:
    """Intel's fiscal 2024 operating income, 93 to -11,678 USD million, used to read -12,656.99%."""
    now, before = "2025-12-31", "2024-12-31"

    def growth(prior: str, current: str) -> tuple[str | None, str | None]:
        cells = {before: {"value": prior, "accession_number": "A-26"}, now: {"value": current, "accession_number": "A-26"}}
        record = {"items": {"operating_income": {"tag": "us-gaap:OperatingIncomeLoss", "values": cells}}}
        values, reasons = rf.computed_metrics(record, now, before)
        return values.get("operating_income_growth_pct"), reasons.get("operating_income_growth_pct")

    assert growth("180", "300") == ("66.67", None)
    assert growth("100", "10") == ("-90.00", None) and growth("100", "0") == ("-100.00", None)
    # Profit to loss: the percentage has no reading, however small or large it is.
    for prior, current in (("93", "-11678"), ("100", "-50")):
        value, reason = growth(prior, current)
        assert value is None and reason == "operating_income is negative after a positive prior year, so a growth rate is not meaningful"
    # A change of more than ten times the prior-year figure; exactly ten times is still reported.
    assert growth("100", "1100") == ("1000.00", None)
    value, reason = growth("5", "2000")
    assert value is None and reason.startswith("the change is more than ten times prior-year operating_income")
    assert growth("100", "1101")[0] is None
    # The rule that was there before is unchanged.
    assert growth("-5", "40") == (None, "prior-year operating_income is zero or negative, so a growth rate is not meaningful")

    # In the bundled records it removes five figures and leaves real growth alone.
    def bundled(ticker: str, fiscal_year: int) -> dict[str, Any]:
        view = rf.company_view(rf.bundled_record(ticker), years=rf.MAX_VIEW_YEARS, origin="bundled")
        return next(year for year in view["fiscal_years"] if year["fiscal_year"] == fiscal_year)

    intel = bundled("INTC", 2024)
    for metric in ("operating_income_growth_pct", "net_income_growth_pct", "diluted_eps_growth_pct"):
        assert metric not in intel["computed"] and "not meaningful" in intel["not_computed"][metric]
    assert intel["computed"]["revenue_growth_pct"] == "-2.08" and intel["computed"]["net_income_change"] == "-20445"
    assert bundled("NVDA", 2024)["computed"]["operating_income_growth_pct"] == "680.59"
