"""SEC EDGAR retrieval and filing-text normalisation used to build the bundled data.

``scripts/build_sample_book.py`` and ``scripts/reported_financials.py`` use it to fetch
filings, turn their HTML into comparable text, and read XBRL facts. At agent runtime only
``declared_user_agent``, the request interval and ``decimal_text`` are used, by
``scripts/sec_lookup.py`` and only when live lookups are switched on.

Network access needs ``SEC_EDGAR_USER_AGENT`` (SEC fair-access policy) and stays under
10 requests per second. Every response is cached on disk, so a rebuild from an existing
cache makes no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.request import Request, urlopen

SEC_MIN_INTERVAL_SECONDS = 0.15  # under the SEC limit of 10 requests per second
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
# Addresses that come from a template and identify nobody.
PLACEHOLDER_CONTACT_MARKERS = ("@example.", "your-domain", "yourdomain")

NORMALISATION_NOTE = (
    "Filing HTML is reduced to text by removing tags, scripts, styles and the inline-XBRL "
    "header, decoding character references, and collapsing each run of whitespace to one "
    "space. A sentence passage is an exact substring of that text. A table-row passage is the "
    "row's non-empty cell texts joined with ' | '; a currency symbol, closing parenthesis or "
    "percent sign that the filing puts in its own cell is attached to the adjacent number."
)

_SKIP_TAGS = frozenset({"script", "style", "ix:header"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "table", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}
)
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"))


@dataclass
class FilingText:
    """Normalised text of one filing document plus its table rows."""

    text: str
    rows: list[dict[str, Any]] = field(default_factory=list)


class _FilingParser(HTMLParser):
    """Build whitespace-normalised text and table rows in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._buffer: list[str] = []
        self._length = 0
        self._ends_with_space = True
        self._row_stack: list[dict[str, Any]] = []
        self._cell: list[str] | None = None
        self.rows: list[dict[str, Any]] = []

    def _emit(self, chunk: str) -> None:
        for piece in re.split(r"(\s+)", chunk.translate(_ZERO_WIDTH)):
            if not piece:
                continue
            if piece.isspace():
                if not self._ends_with_space:
                    self._buffer.append(" ")
                    self._length += 1
                    self._ends_with_space = True
            else:
                self._buffer.append(piece)
                self._length += len(piece)
                self._ends_with_space = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._emit(" ")
            if self._cell is not None and tag not in ("td", "th"):
                self._cell.append(" ")
        if tag == "tr":
            self._row_stack.append({"offset": self._length, "cells": []})
        elif tag in ("td", "th") and self._row_stack:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("td", "th") and self._row_stack and self._cell is not None:
            cell_text = " ".join("".join(self._cell).translate(_ZERO_WIDTH).split())
            if cell_text:
                self._row_stack[-1]["cells"].append(cell_text)
            self._cell = None
        elif tag == "tr" and self._row_stack:
            row = self._row_stack.pop()
            cells = merge_split_cells(row["cells"])
            if cells:
                self.rows.append({"offset": row["offset"], "cells": cells})
        elif tag in _BLOCK_TAGS and self._cell is not None:
            self._cell.append(" ")
        if tag in _BLOCK_TAGS:
            self._emit(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._emit(data)
        if self._cell is not None:
            self._cell.append(data)

    def result(self) -> FilingText:
        return FilingText(text="".join(self._buffer).strip(), rows=self.rows)


def merge_split_cells(cells: list[str]) -> list[str]:
    """Attach a lone currency symbol, closing parenthesis or percent sign to its number."""
    merged: list[str] = []
    pending_prefix = ""
    for cell in cells:
        if cell in ("$", "€", "£"):
            pending_prefix = cell
            continue
        if cell in (")", "%", ")%") and merged:
            merged[-1] += cell
            continue
        text = pending_prefix + cell
        pending_prefix = ""
        merged.append(re.sub(r"^([$€£])\s+", r"\1", text))
    if pending_prefix:
        merged.append(pending_prefix)
    return merged


def parse_filing_html(html: str) -> FilingText:
    """Return the normalised text and table rows of a filing's primary HTML document."""
    parser = _FilingParser()
    parser.feed(html)
    parser.close()
    return parser.result()


def row_text(cells: list[str]) -> str:
    """Render a table row as one passage."""
    return " | ".join(cells)


def find_rows(filing: FilingText, cells_include: list[str], *, after_offset: int = 0) -> list[dict[str, Any]]:
    """Rows that contain every given cell text, in order, at or after ``after_offset``."""
    found: list[dict[str, Any]] = []
    for row in filing.rows:
        if row["offset"] < after_offset:
            continue
        position = 0
        for wanted in cells_include:
            try:
                position = row["cells"].index(wanted, position) + 1
            except ValueError:
                break
        else:
            found.append(row)
    return found


def declared_user_agent() -> str:
    """The contact declared in ``SEC_EDGAR_USER_AGENT``, as SEC's fair-access policy asks.

    Raises:
        RuntimeError: the variable is empty, has no address, or holds a template address.
    """
    user_agent = os.environ.get(USER_AGENT_ENV, "").strip()
    placeholder = any(marker in user_agent.lower() for marker in PLACEHOLDER_CONTACT_MARKERS)
    if not user_agent or "@" not in user_agent or placeholder:
        raise RuntimeError(
            f"Set {USER_AGENT_ENV}='Your Organisation contact@your-domain', with your own organisation "
            "and an address you read, before fetching from sec.gov. SEC rejects requests that do not "
            "declare a contact, and a placeholder address declares nobody."
        )
    return user_agent


def sha256_bytes(payload: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(payload).hexdigest()


def utc_iso(timestamp: float) -> str:
    """Format a POSIX timestamp as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EdgarCache:
    """Disk cache in front of sec.gov. Reads the cache first and fetches only what is missing."""

    def __init__(self, cache_dir: Path, *, offline: bool = False) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self._last_request = 0.0
        self.network_requests = 0

    def fetch(self, url: str, relative_path: str) -> tuple[bytes, str]:
        """Return ``(bytes, retrieved_at_utc)`` for a URL, from the cache when present."""
        target = self.cache_dir / relative_path
        if not target.exists():
            if self.offline:
                raise FileNotFoundError(f"{relative_path} is not cached and --offline was given.")
            wait = SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            request = Request(url, headers={"User-Agent": declared_user_agent(), "Accept-Encoding": "identity"})
            with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed sec.gov URLs
                payload = response.read()
            self._last_request = time.monotonic()
            self.network_requests += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return target.read_bytes(), utc_iso(target.stat().st_mtime)

    def submissions(self, ticker: str, cik: str) -> dict[str, Any]:
        payload, _ = self.fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", f"{ticker}_sub.json")
        return json.loads(payload)

    def company_facts(self, ticker: str, cik: str) -> tuple[dict[str, Any], str]:
        """Return ``(companyfacts, retrieved_at_utc)``. Numbers are parsed as exact decimals."""
        payload, retrieved = self.fetch(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", f"{ticker}_facts.json"
        )
        return json.loads(payload, parse_float=Decimal), retrieved

    def primary_document(self, ticker: str, cik: str, accession: str, document: str) -> tuple[bytes, str, str]:
        """Return ``(bytes, url, retrieved_at_utc)`` for a filing's primary document."""
        url = filing_document_url(cik, accession, document)
        payload, retrieved = self.fetch(url, f"filings/{ticker}_{accession}.htm")
        return payload, url, retrieved

    def xbrl_instance(self, ticker: str, cik: str, accession: str, document: str) -> tuple[bytes, str, str]:
        """Return ``(bytes, url, retrieved_at_utc)`` for the XBRL instance EDGAR extracts from a filing.

        EDGAR names it after the primary document: ``name.htm`` becomes ``name_htm.xml``.
        """
        url = filing_document_url(cik, accession, re.sub(r"\.htm$", "_htm.xml", document))
        payload, retrieved = self.fetch(url, f"filings/{ticker}_{accession}_instance.xml")
        return payload, url, retrieved


def filing_document_url(cik: str, accession: str, document: str) -> str:
    """Archive URL of one document inside a filing."""
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"


def filing_record(submissions: dict[str, Any], accession: str) -> dict[str, str]:
    """Form, dates and primary document of one accession from the submissions index."""
    recent = submissions["filings"]["recent"]
    try:
        index = recent["accessionNumber"].index(accession)
    except ValueError as exc:
        raise KeyError(f"Accession {accession} is not in the recent filings index.") from exc
    return {
        "form": recent["form"][index],
        "accession_number": accession,
        "filing_date": recent["filingDate"][index],
        "report_date": recent["reportDate"][index],
        "primary_document": recent["primaryDocument"][index],
    }


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def select_fact(
    company_facts: dict[str, Any],
    tags: list[str],
    unit: str,
    *,
    accession: str,
    period_end: str,
    annual_duration: bool,
) -> dict[str, Any] | None:
    """First tag in ``tags`` that the given accession reports for ``period_end``.

    A duration fact must span one fiscal year (350 to 380 days). Returns the tag, the raw
    value and its period, or None when the accession does not report any of the tags.
    """
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        rows = us_gaap.get(tag, {}).get("units", {}).get(unit, [])
        matches = [
            row
            for row in rows
            if row.get("accn") == accession
            and row.get("end") == period_end
            and (
                ("start" not in row)
                if not annual_duration
                else ("start" in row and 350 <= _days(row["start"], row["end"]) <= 380)
            )
        ]
        values = {str(row["val"]) for row in matches}
        if len(values) > 1:
            raise ValueError(f"{tag}: accession {accession} reports {sorted(values)} for {period_end}.")
        if matches:
            row = matches[0]
            return {
                "tag": f"us-gaap:{tag}",
                "unit": unit,
                "value": str(row["val"]),
                "period_start": row.get("start"),
                "period_end": row["end"],
                "fiscal_year": row.get("fy"),
                "accession_number": accession,
            }
    return None


def prior_instant_end(company_facts: dict[str, Any], tag: str, unit: str, *, accession: str, period_end: str) -> str | None:
    """Latest balance-sheet date before ``period_end`` that the accession reports for a tag."""
    rows = company_facts.get("facts", {}).get("us-gaap", {}).get(tag.removeprefix("us-gaap:"), {})
    ends = sorted(
        {
            row["end"]
            for row in rows.get("units", {}).get(unit, [])
            if row.get("accn") == accession and "start" not in row and row["end"] < period_end
        }
    )
    return ends[-1] if ends else None


def decimal_text(value: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def to_millions(raw_value: str) -> str:
    """Convert a raw XBRL amount (USD or shares) to millions, exactly."""
    return decimal_text(Decimal(raw_value) / Decimal("1000000"))


def has_accession_facts(company_facts: dict[str, Any], accession: str) -> bool:
    """True when companyfacts carries at least one us-gaap fact reported by the accession."""
    for node in company_facts.get("facts", {}).get("us-gaap", {}).values():
        for rows in node.get("units", {}).values():
            if any(row.get("accn") == accession for row in rows):
                return True
    return False


_XBRLI = "{http://www.xbrl.org/2003/instance}"


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def instance_to_company_facts(instance_xml: bytes, accession: str) -> dict[str, Any]:
    """Convert a filing's XBRL instance into the companyfacts shape that ``select_fact`` reads.

    Only entity-wide facts (contexts without a dimensional segment) in the us-gaap
    namespace are kept. The fiscal year comes from ``dei:DocumentFiscalYearFocus``.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(instance_xml)
    contexts: dict[str, dict[str, str]] = {}
    for context in root.findall(f"{_XBRLI}context"):
        entity = context.find(f"{_XBRLI}entity")
        if entity is None or entity.find(f"{_XBRLI}segment") is not None:
            continue
        period = context.find(f"{_XBRLI}period")
        if period is None:
            continue
        instant = period.findtext(f"{_XBRLI}instant")
        if instant:
            contexts[context.get("id", "")] = {"end": instant.strip()}
        else:
            start, end = period.findtext(f"{_XBRLI}startDate"), period.findtext(f"{_XBRLI}endDate")
            if start and end:
                contexts[context.get("id", "")] = {"start": start.strip(), "end": end.strip()}

    units: dict[str, str] = {}
    for unit in root.findall(f"{_XBRLI}unit"):
        divide = unit.find(f"{_XBRLI}divide")
        if divide is not None:
            measures = [_local(m.text or "") for m in divide.iter(f"{_XBRLI}measure")]
            units[unit.get("id", "")] = "/".join(measures)
        else:
            units[unit.get("id", "")] = _local(unit.findtext(f"{_XBRLI}measure") or "")

    fiscal_year: int | None = None
    us_gaap: dict[str, dict[str, Any]] = {}
    for element in root:
        if "}" not in element.tag:
            continue
        namespace, local = element.tag[1:].split("}", 1)
        text = (element.text or "").strip()
        if local == "DocumentFiscalYearFocus" and text.isdigit():
            fiscal_year = int(text)
        context = contexts.get(element.get("contextRef", ""))
        if "fasb.org/us-gaap" not in namespace or context is None or not text:
            continue
        try:
            value = Decimal(text)
        except ArithmeticError:
            continue
        unit_name = units.get(element.get("unitRef", ""), "")
        row = {**context, "val": value, "accn": accession}
        us_gaap.setdefault(local, {"units": {}})["units"].setdefault(unit_name, []).append(row)

    for node in us_gaap.values():
        for rows in node["units"].values():
            for row in rows:
                row["fy"] = fiscal_year
    return {"facts": {"us-gaap": us_gaap}}
