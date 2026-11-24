"""Look up a company's reported figures on SEC EDGAR, for a company outside the bundle.

Off by default: a lookup happens only when ``FSI_SEC_LIVE_LOOKUP`` is true and
``SEC_EDGAR_USER_AGENT`` names an organisation and a contact address (SEC fair-access
policy; requests stay under 10 per second). Two fixed SEC URLs are read: the ticker list
and one company's XBRL company facts. The response is reduced to a
``reported_financials`` record at once, and only that record is kept, under
``FSI_COMPANY_STORE_DIR`` (default ``_output/company_store``).

Fetch and keep companies ahead of time, so the agent can answer about them with lookups off::

    SEC_EDGAR_USER_AGENT="Your Organisation contact@your-domain" python scripts/sec_lookup.py TSLA NFLX
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import edgar_source, reported_financials  # noqa: E402

LIVE_LOOKUP_ENV = "FSI_SEC_LIVE_LOOKUP"
STORE_DIR_ENV = "FSI_COMPANY_STORE_DIR"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKER_LIST_FILE = "_sec_company_tickers.json"

# A stored record is reused for a day, and SEC's ticker list for a week, before SEC is asked again.
RECORD_MAX_AGE_HOURS = 24.0
TICKER_LIST_MAX_AGE_HOURS = 24.0 * 7
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30

ORIGIN_BUNDLED = "bundled"
ORIGIN_STORE = "local_store"
ORIGIN_LIVE = "sec_live"

REASON_INVALID_TICKER = "invalid_ticker"
REASON_LIVE_OFF = "live_lookup_disabled"
REASON_NO_CONTACT = "sec_contact_missing"
REASON_UNKNOWN_TICKER = "unknown_ticker"
REASON_NO_FACTS = "no_form_10k_us_gaap_facts"
REASON_SEC_UNAVAILABLE = "sec_unavailable"
REASON_BAD_RECORD = "stored_record_failed_hash_check"

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:-[A-Z]{1,2})?$")
_TRUE = ("1", "true", "yes", "on")

Fetch = Callable[[str], bytes]

_request_lock = threading.Lock()
_last_request = 0.0


class LookupUnavailable(Exception):
    """A company's figures cannot be returned; ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def live_lookup_enabled() -> bool:
    """True when the environment allows reading SEC EDGAR at run time."""
    return os.environ.get(LIVE_LOOKUP_ENV, "").strip().lower() in _TRUE


def store_dir() -> Path:
    """Directory that keeps the records of looked-up companies."""
    configured = os.environ.get(STORE_DIR_ENV, "").strip()
    return Path(configured) if configured else SKILL_ROOT / "_output" / "company_store"


def normalise_ticker(raw: str) -> str:
    """Upper-case a ticker and write a share class the way SEC does (BRK.B becomes BRK-B).

    Raises:
        LookupUnavailable: the text is not a ticker (it is also used as a file name).
    """
    ticker = str(raw).strip().upper().replace(".", "-")
    if not _TICKER_RE.match(ticker):
        raise LookupUnavailable(REASON_INVALID_TICKER, f"{str(raw)[:20]!r} is not a stock ticker.")
    return ticker


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(retrieved_at_utc: str, now: datetime) -> float:
    retrieved = datetime.strptime(retrieved_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now - retrieved).total_seconds() / 3600.0


def http_get(url: str) -> bytes:
    """GET one sec.gov URL with the declared contact, no faster than SEC allows.

    Raises:
        RuntimeError: ``SEC_EDGAR_USER_AGENT`` does not declare a contact.
        OSError: the request failed (``urllib`` errors are ``OSError`` subclasses).
    """
    global _last_request
    user_agent = edgar_source.declared_user_agent()
    with _request_lock:
        wait = edgar_source.SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed sec.gov URLs
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                encoding = response.headers.get("Content-Encoding", "")
        finally:
            _last_request = time.monotonic()
    if len(payload) > MAX_RESPONSE_BYTES:
        raise OSError(f"{url} returned more than {MAX_RESPONSE_BYTES} bytes.")
    return gzip.decompress(payload) if encoding.lower() == "gzip" else payload


def _get(fetch: Fetch, url: str, what: str) -> bytes:
    """Run one fetch and turn its failures into ``LookupUnavailable``."""
    try:
        return fetch(url)
    except RuntimeError as exc:
        raise LookupUnavailable(REASON_NO_CONTACT, str(exc)) from exc
    except HTTPError as exc:
        if exc.code == 404:
            raise LookupUnavailable(REASON_NO_FACTS, f"SEC EDGAR has no {what}.") from exc
        raise LookupUnavailable(REASON_SEC_UNAVAILABLE, f"SEC EDGAR answered HTTP {exc.code} for {what}.") from exc
    except (URLError, OSError) as exc:
        raise LookupUnavailable(REASON_SEC_UNAVAILABLE, f"SEC EDGAR could not be reached for {what}: {exc}") from exc


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    """Write through a temporary file, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ticker_directory(fetch: Fetch, now: datetime) -> dict[str, dict[str, str]]:
    """SEC's ticker list as ``{ticker: {"cik": ten digits, "name": ...}}``, kept for a week."""
    path = store_dir() / TICKER_LIST_FILE
    stored = _read_json(path)
    if stored and _age_hours(stored["retrieved_at_utc"], now) < TICKER_LIST_MAX_AGE_HOURS:
        return stored["tickers"]
    listing = json.loads(_get(fetch, TICKERS_URL, "list of company tickers"))
    tickers = {
        str(entry["ticker"]).upper(): {"cik": str(entry["cik_str"]).zfill(10), "name": str(entry["title"])}
        for entry in listing.values()
    }
    _write_json(path, {"source_url": TICKERS_URL, "retrieved_at_utc": _iso(now), "tickers": tickers})
    return tickers


def stored_record(ticker: str) -> dict[str, Any] | None:
    """The stored record of a looked-up company, hash-checked, or None when there is none.

    Raises:
        LookupUnavailable: the stored file does not match its own hash.
    """
    record = _read_json(store_dir() / f"{ticker}.json")
    if record is None:
        return None
    try:
        return reported_financials.checked_record(record, f"stored record {ticker}")
    except (reported_financials.RecordIntegrityError, KeyError, TypeError) as exc:
        raise LookupUnavailable(REASON_BAD_RECORD, f"The stored record for {ticker} is damaged: {exc}") from exc


def fetch_record(ticker: str, fetch: Fetch = http_get, now: datetime | None = None) -> dict[str, Any]:
    """Read one company from SEC EDGAR, reduce it to a record and store it.

    Raises:
        LookupUnavailable: SEC does not list the ticker, has no Form 10-K facts under
            US GAAP for it, could not be reached, or no contact is declared.
    """
    moment = now or utc_now()
    symbol = normalise_ticker(ticker)
    entry = ticker_directory(fetch, moment).get(symbol)
    if entry is None:
        raise LookupUnavailable(REASON_UNKNOWN_TICKER, f"{symbol} is not in SEC's list of company tickers.")
    url = reported_financials.COMPANYFACTS_URL.format(cik=entry["cik"])
    facts = json.loads(_get(fetch, url, f"XBRL company facts for {symbol}"), parse_float=Decimal)
    try:
        record = reported_financials.build_record(
            facts,
            ticker=symbol,
            # The name SEC lists against the ticker; entityName in company facts can be a co-registrant.
            name=entry["name"],
            cik=entry["cik"],
            source="SEC EDGAR company facts API",
            retrieved_at_utc=_iso(moment),
        )
    except reported_financials.NoAnnualFacts as exc:
        raise LookupUnavailable(
            REASON_NO_FACTS,
            f"{symbol} has no Form 10-K figures under US GAAP in SEC company facts "
            "(for example a foreign issuer that files Form 20-F, or a fund).",
        ) from exc
    _write_json(store_dir() / f"{symbol}.json", record)
    return record


def lookup(
    ticker: str,
    *,
    fetch: Fetch = http_get,
    live: bool | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Return ``(record, origin)`` for one ticker: bundled, stored, or read from SEC EDGAR.

    Args:
        ticker: Stock ticker in any case.
        fetch: Replaces the HTTP GET, for tests.
        live: Whether SEC EDGAR may be read; defaults to ``live_lookup_enabled()``.
        now: The current time, for tests.

    Raises:
        LookupUnavailable: with one of the ``REASON_*`` codes.
    """
    moment = now or utc_now()
    symbol = normalise_ticker(ticker)
    bundled = reported_financials.bundled_record(symbol)
    if bundled is not None:
        return bundled, ORIGIN_BUNDLED
    allowed = live_lookup_enabled() if live is None else live
    stored = stored_record(symbol)
    if stored is not None and (not allowed or _age_hours(stored["retrieved_at_utc"], moment) < RECORD_MAX_AGE_HOURS):
        return stored, ORIGIN_STORE
    if not allowed:
        raise LookupUnavailable(
            REASON_LIVE_OFF,
            f"{symbol} is not among the bundled companies and SEC EDGAR lookups are off. Set {LIVE_LOOKUP_ENV}=true "
            f"and {edgar_source.USER_AGENT_ENV}, or fetch it once with scripts/sec_lookup.py.",
        )
    try:
        return fetch_record(symbol, fetch, moment), ORIGIN_LIVE
    except LookupUnavailable as exc:
        # An older stored copy, with its retrieval time, is better than nothing when SEC is unreachable.
        if stored is not None and exc.reason == REASON_SEC_UNAVAILABLE:
            return stored, ORIGIN_STORE
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch companies from SEC EDGAR and keep their reported figures.")
    parser.add_argument("tickers", nargs="*", help="Stock tickers to fetch, e.g. TSLA NFLX")
    parser.add_argument("--list", action="store_true", help="List the companies already stored")
    args = parser.parse_args()

    if args.list or not args.tickers:
        stored = sorted(path.stem for path in store_dir().glob("*.json") if not path.name.startswith("_"))
        print(f"{len(stored)} stored in {store_dir()}: {', '.join(stored) or 'none'}")
        return 0
    failures = 0
    for raw in args.tickers:
        try:
            record = fetch_record(raw)
        except LookupUnavailable as exc:
            failures += 1
            print(f"{raw}: not stored ({exc.reason}): {exc.message}", file=sys.stderr)
            continue
        years = ", ".join(str(year["fiscal_year"]) for year in record["fiscal_years"])
        print(f"{record['ticker']}: {record['name']}, fiscal years {years}, {len(record['items'])} line items stored.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
