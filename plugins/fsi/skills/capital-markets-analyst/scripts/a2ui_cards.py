"""A2UI cards for the Capital Markets Analyst skill.

A2UI is a declarative format: the agent sends JSON messages that name components from
a catalog, and the client renders them with its own widgets. This module builds two
cards as A2UI v0.9 server-to-client messages, using only the basic catalog:

* a holding card: one issuer under a scenario, with the filing passage, document id
  and SHA-256 behind each figure;
* a book card: the book totals, the holdings with and without a convertible figure,
  and the disclosed links.

What this is and is not:

* Python builds the cards from the deterministic analysis. No model writes the JSON,
  so every number and passage on a card is the tool's.
* The module returns the message list. It does not send it: the skill has no A2A
  server yet, and the ADK agent does not call this module. Over A2A the list travels
  as one data part whose metadata sets ``mimeType`` to ``A2UI_MIME_TYPE``.
* Version: messages carry ``"version": "v0.9"`` and the v0.9 basic catalog id, which
  the v0.9.1 schemas also accept. The basic catalog has no table, chart or graph
  component, so links are listed as rows; the node-link graph is the MCP App.
* A client renders ``Text`` through a Markdown parser, so every string is escaped to
  display literally (see ``markdown_literal``). Filing passages stay exact that way.

Run ``python scripts/a2ui_cards.py --card holding --ticker UAL`` to print a card as
JSON Lines.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import fsi_engine, ripple_view  # noqa: E402

A2UI_VERSION = "v0.9"
BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
A2UI_MIME_TYPE = "application/a2ui+json"
AGENT_DISPLAY_NAME = "FSI Capital Markets Analyst"

CARD_HOLDING = "holding"
CARD_BOOK = "book"
CARD_KINDS = (CARD_HOLDING, CARD_BOOK)

# The same wording as the workstation and the MCP App view (a test keeps the three equal).
STATUS_LABELS: dict[str, str] = {
    fsi_engine.STATUS_INCOME_STATEMENT: "Annual income sensitivity",
    fsi_engine.STATUS_FAIR_VALUE_ONLY: "Fair-value sensitivity only",
    fsi_engine.STATUS_NOT_CONVERTIBLE: "Quantified, not convertible",
    fsi_engine.STATUS_NOT_QUANTIFIED: "No amount disclosed",
    fsi_engine.STATUS_INCORPORATED_BY_REFERENCE: "Incorporated by reference",
    fsi_engine.STATUS_NONE_FOUND: "No passage located",
}

NO_FIGURE_NOTE = (
    "The filing gives no figure that converts to annual earnings, so no impact is computed for this holding. "
    "That is not a statement that it has no exposure."
)
SCALING_NOTE = (
    "Figures are linear scalings of each issuer's disclosed sensitivity, computed by the tool in decimal arithmetic."
)

# Components this module emits. All are in the v0.9 basic catalog.
_USED_COMPONENTS = frozenset({"Card", "Column", "Row", "Text", "Divider"})

_MD_INLINE = re.compile(r"([\\`*_\[\]<~$])")
_MD_ENTITY = re.compile(r"&(?=#?[0-9A-Za-z]+;)")
_MD_LINE_START = re.compile(r"^(#{1,6}(?=\s|$)|>|[-+](?=\s|$)|-{3,}$|\d{1,9}(?=[.)](?:\s|$)))")
_MD_UNESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


def markdown_literal(text: str) -> str:
    """Escape ``text`` so a CommonMark renderer displays it character for character.

    The basic catalog's ``Text`` is rendered through a Markdown parser. A filing passage
    such as ``2025 | 4,269 | $9,819 | $2.30`` must not be restyled (a renderer with a math
    extension would read the two dollar signs as a formula), so characters that start an
    inline construct are backslash-escaped, and so is a block marker at the start of the
    text. ``markdown_unescape`` is the inverse.

    Raises:
        ValueError: for text Markdown cannot show literally: empty text, a line break or a
            tab, or leading or trailing whitespace (a renderer strips it).
    """
    if not text or text != text.strip():
        raise ValueError("Empty text, or text with leading or trailing whitespace, cannot be shown literally.")
    if "\n" in text or "\r" in text or "\t" in text:
        raise ValueError("Text with a line break or a tab cannot be shown literally in one A2UI Text component.")
    escaped = _MD_INLINE.sub(r"\\\1", text)
    escaped = _MD_ENTITY.sub(r"\\&", escaped)
    match = _MD_LINE_START.match(escaped)
    if match:
        # An ordered-list marker is escaped at its delimiter ("1\."); every other marker at its first character.
        split = match.end() if escaped[0].isdigit() else 0
        escaped = f"{escaped[:split]}\\{escaped[split:]}"
    return escaped


def markdown_unescape(text: str) -> str:
    """Undo ``markdown_literal``: the characters a CommonMark renderer would display."""
    return _MD_UNESCAPE.sub(r"\1", text)


def _usd_m(value: str) -> str:
    """``'-2933.42'`` -> ``'-$2,933.42M'`` (USD millions), with an explicit sign unless zero."""
    number = Decimal(value)
    sign = "" if number == 0 else ("-" if number < 0 else "+")
    return f"{sign}${ripple_view.format_decimal(str(abs(number)))}M"


def _usd_per_share(value: str) -> str:
    number = Decimal(value)
    sign = "" if number == 0 else ("-" if number < 0 else "+")
    return f"{sign}${ripple_view.format_decimal(str(abs(number)), places=4)} per share"


def _scenario_text(analysis: dict[str, Any]) -> str:
    scenario = analysis["scenario"]
    return (
        f"Rates {ripple_view.format_signed(scenario['rate_bps'])} bp · "
        f"Oil {ripple_view.format_signed(scenario['oil_pct'])}%"
    )


def _notice(governance: dict[str, Any]) -> str:
    """The dataset's own notice (when it has one) followed by the scaling statement."""
    return " ".join(part for part in (governance.get("data_notice", "").strip(), SCALING_NOTE) if part)


def _relation_words(relationship: str) -> str:
    return relationship.lower().replace("_", " ")


def _share_text(edge: dict[str, Any]) -> str:
    share = edge["revenue_share_pct"]
    return f"{share}% disclosed share" if share is not None else "no share disclosed"


class _Surface:
    """Collects the components of one surface as a flat list with unique ids."""

    def __init__(self, surface_id: str) -> None:
        self.surface_id = surface_id
        self.components: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    def add(self, component_id: str, component: str, **properties: Any) -> str:
        if component_id in self._ids:
            raise ValueError(f"Duplicate component id {component_id!r}.")
        self._ids.add(component_id)
        self.components.append({"id": component_id, "component": component, **properties})
        return component_id

    def text(self, component_id: str, value: str, variant: str = "body", *, weight: int | None = None) -> str:
        properties: dict[str, Any] = {"text": markdown_literal(value), "variant": variant}
        if weight is not None:
            properties["weight"] = weight
        return self.add(component_id, "Text", **properties)

    def quote(self, component_id: str, passage: str) -> str:
        """A filing passage, shown inside quotation marks. Nothing else on a card is quoted."""
        return self.text(component_id, f"“{passage}”")

    def column(self, component_id: str, children: list[str], *, weight: int | None = None) -> str:
        properties: dict[str, Any] = {"children": children}
        if weight is not None:
            properties["weight"] = weight
        return self.add(component_id, "Column", **properties)

    def row(self, component_id: str, children: list[str]) -> str:
        return self.add(component_id, "Row", children=children)

    def divider(self, component_id: str) -> str:
        return self.add(component_id, "Divider")

    def metric(self, component_id: str, label: str, value: str, note: str | None = None) -> str:
        """A label over a value (and an optional note), sized to share a row equally with its siblings."""
        children = [
            self.text(f"{component_id}-label", label, "caption"),
            self.text(f"{component_id}-value", value, "h4"),
        ]
        if note:
            children.append(self.text(f"{component_id}-note", note, "caption"))
        return self.column(component_id, children, weight=1)

    def messages(self, body: list[str]) -> list[dict[str, Any]]:
        """Wrap ``body`` in a card and return the create and update messages for the surface."""
        self.add("root", "Card", child=self.column("body", body))
        messages = [
            {
                "version": A2UI_VERSION,
                "createSurface": {
                    "surfaceId": self.surface_id,
                    "catalogId": BASIC_CATALOG_ID,
                    "theme": {"agentDisplayName": AGENT_DISPLAY_NAME},
                },
            },
            {
                "version": A2UI_VERSION,
                "updateComponents": {"surfaceId": self.surface_id, "components": self.components},
            },
        ]
        problems = check_messages(messages)
        if problems:
            raise ValueError("The card is not a well-formed A2UI surface: " + "; ".join(problems))
        return messages


def _factor_section(
    surface: _Surface,
    prefix: str,
    title: str,
    note_prefix: str,
    citation: dict[str, Any],
    disclosed_m: str | None,
    multiplier: str,
    scaled_m: str,
    holding: dict[str, Any],
) -> list[str]:
    """Components for one factor: status, scaling, the passage with its section, supporting passages, notes."""
    status = citation["disclosure_status"]
    children = [surface.text(f"{prefix}-title", f"{title}: {STATUS_LABELS.get(status, status)}", "h5")]
    if status == fsi_engine.STATUS_INCOME_STATEMENT and disclosed_m is not None:
        children.append(
            surface.text(
                f"{prefix}-scaling",
                f"Disclosed {_usd_m(disclosed_m)}, scaled by {multiplier} (rounded) to {_usd_m(scaled_m)}",
                "caption",
            )
        )
    children.append(surface.text(f"{prefix}-section", citation["filing_section"], "caption"))
    if citation.get("verbatim_quote"):
        children.append(surface.quote(f"{prefix}-quote", citation["verbatim_quote"]))
    else:
        children.append(surface.text(f"{prefix}-quote", "No passage is bundled for this factor."))
    for index, passage in enumerate(citation.get("supporting_passages", [])):
        children.append(surface.text(f"{prefix}-support-{index}-section", passage["section"], "caption"))
        children.append(surface.quote(f"{prefix}-support-{index}-quote", passage["text"]))
    notes = [
        note[len(note_prefix) + 1 :].strip()
        for note in [*holding.get("coverage_notes", []), *holding.get("basis_notes", [])]
        if note.startswith(f"{note_prefix}:")
    ]
    for index, note in enumerate(notes):
        # The tool's own words: shown without quotation marks.
        children.append(surface.text(f"{prefix}-note-{index}", f"Tool note: {note}"))
    return children


def build_holding_card(analysis: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    """Return the A2UI messages for one holding of an analysed book.

    Args:
        analysis: result of ``fsi_engine.analyze_book`` (or ``ripple_view.build_ripple_view``).
        ticker: a holding of that book.

    Raises:
        ValueError: if the ticker is not a holding of the analysed book.
    """
    wanted = ticker.strip().upper()
    holding = next((item for item in analysis["holdings"] if item["ticker"] == wanted), None)
    if holding is None:
        held = ", ".join(item["ticker"] for item in analysis["holdings"])
        raise ValueError(f"Ticker '{wanted}' is not a holding of book '{analysis['book_id']}'. Holdings: {held}.")

    governance = analysis["governance"]
    impact = holding["scaled_impact"]
    basis = holding["disclosed_basis"]
    citations = {entry["factor"]: entry for entry in holding["provenance"]}
    rate, oil = citations["interest_rate"], citations["commodity_oil"]
    surface = _Surface(f"fsi-holding-{wanted}-{governance['packet_sha256'][:12]}")

    body = [
        surface.text("title", f"{holding['name']} ({holding['ticker']})", "h3"),
        surface.text(
            "subtitle",
            f"{holding['sector']} · {_scenario_text(analysis)} · Source: {governance['source_posture']}",
            "caption",
        ),
    ]
    if ripple_view.has_convertible_figure(holding):
        body.append(
            surface.row(
                "metrics",
                [
                    surface.metric("pretax", "Pre-tax impact", _usd_m(impact["total_pre_tax_m"])),
                    surface.metric("aftertax", "After-tax impact", _usd_m(impact["after_tax_m"])),
                    surface.metric(
                        "eps",
                        "Diluted EPS change",
                        _usd_per_share(impact["eps_delta"]),
                        (
                            f"{ripple_view.format_signed(impact['eps_pct_of_fy'])}% of FY EPS"
                            if impact["eps_pct_of_fy"] is not None
                            else "% of FY EPS not meaningful"
                        ),
                    ),
                ],
            )
        )
    else:
        body.append(surface.text("no-figure", NO_FIGURE_NOTE))
    for index, flag in enumerate(
        [*holding["extrapolation_flags"], *holding["non_comparable_flags"], *holding.get("conversion_notes", [])]
    ):
        body.append(surface.text(f"flag-{index}", f"Warning: {flag}"))

    body.append(surface.divider("rate-divider"))
    body.extend(
        _factor_section(
            surface, "rate", "Interest rates", "Rate", rate, basis["rate_pre_tax_m"],
            impact["rate_multiplier"], impact["rate_pre_tax_m"], holding,
        )
    )
    body.append(surface.divider("oil-divider"))
    body.extend(
        _factor_section(
            surface, "oil", "Oil / jet fuel", "Oil", oil, basis["oil_pre_tax_m"],
            impact["oil_multiplier"], impact["oil_pre_tax_m"], holding,
        )
    )
    body.append(surface.divider("footer-divider"))
    body.append(surface.text("document", f"{rate['doc_id']} · excerpt SHA-256 {rate['sha256']}", "caption"))
    body.append(surface.text("notice", _notice(governance), "caption"))
    return surface.messages(body)


def build_book_card(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the A2UI messages for the book summary: totals, holdings and disclosed links."""
    governance = analysis["governance"]
    totals = analysis["portfolio_totals"]
    holdings = analysis["holdings"]
    edges = analysis["contagion_graph"]["edges"]
    converted = [holding for holding in holdings if ripple_view.has_convertible_figure(holding)]
    unconverted = [holding for holding in holdings if not ripple_view.has_convertible_figure(holding)]
    surface = _Surface(f"fsi-book-{analysis['book_id']}-{governance['packet_sha256'][:12]}")

    body = [
        surface.text("title", analysis["book_label"], "h3"),
        surface.text(
            "subtitle",
            f"{_scenario_text(analysis)} · Source: {governance['source_posture']} · Data as of {governance['as_of_utc']}",
            "caption",
        ),
        surface.row(
            "metrics",
            [
                surface.metric("pretax", "Pre-tax impact, book total", _usd_m(totals["aggregate_pre_tax_impact_m"])),
                surface.metric("aftertax", "After-tax impact, book total", _usd_m(totals["aggregate_after_tax_impact_m"])),
                surface.metric(
                    "holdings", "Holdings", str(totals["holdings_count"]), f"{len(converted)} with a convertible figure"
                ),
            ],
        ),
        surface.divider("converted-divider"),
        surface.text("converted-title", f"Holdings with a convertible figure ({len(converted)})", "h5"),
    ]
    for holding in converted:
        ticker = holding["ticker"]
        impact = holding["scaled_impact"]
        flag = " (beyond 2x the disclosed basis)" if holding["extrapolation_warning"] else ""
        body.append(
            surface.row(
                f"holding-{ticker}",
                [
                    surface.text(f"holding-{ticker}-name", f"{ticker} · {holding['name']}{flag}", weight=2),
                    surface.text(f"holding-{ticker}-pretax", f"{_usd_m(impact['total_pre_tax_m'])} pre-tax", weight=1),
                    surface.text(f"holding-{ticker}-eps", _usd_per_share(impact["eps_delta"]), weight=1),
                ],
            )
        )
    body.append(surface.divider("unconverted-divider"))
    body.append(surface.text("unconverted-title", f"No convertible figure in the filing ({len(unconverted)})", "h5"))
    for holding in unconverted:
        basis = holding["disclosed_basis"]
        body.append(
            surface.text(
                f"holding-{holding['ticker']}-status",
                f"{holding['ticker']} · {holding['name']}: rates, {STATUS_LABELS[basis['rate_status']].lower()}; "
                f"oil, {STATUS_LABELS[basis['oil_status']].lower()}",
            )
        )
    if unconverted:
        body.append(
            surface.text(
                "unconverted-note",
                "No impact is computed for these holdings because the filing gives nothing to convert, "
                "not because the issuer has no exposure.",
                "caption",
            )
        )
    body.append(surface.divider("links-divider"))
    body.append(surface.text("links-title", f"Disclosed customer and supplier links ({len(edges)})", "h5"))
    for edge in edges:
        body.append(
            surface.text(
                f"link-{edge['edge_id']}",
                f"{edge['src']} {_relation_words(edge['relationship'])} {edge['dst']} · {_share_text(edge)} · "
                f"{edge['provenance']['doc_id']}",
            )
        )
    if not edges:
        body.append(surface.text("links-none", "No disclosed link in the bundled dataset reaches this book's shocked holdings."))
    body.append(surface.divider("footer-divider"))
    body.append(surface.text("notice", _notice(governance), "caption"))
    return surface.messages(body)


def build_card(analysis: dict[str, Any], card: str, ticker: str | None = None) -> list[dict[str, Any]]:
    """Build the named card (``"holding"`` needs a ticker; ``"book"`` does not).

    Raises:
        ValueError: for an unknown card kind, a missing ticker, or a ticker outside the book.
    """
    if card == CARD_BOOK:
        return build_book_card(analysis)
    if card == CARD_HOLDING:
        if not ticker or not ticker.strip():
            raise ValueError("A holding card needs a ticker.")
        return build_holding_card(analysis, ticker)
    raise ValueError(f"Unknown card '{card}'. Use one of: {', '.join(CARD_KINDS)}.")


def delete_surface(surface_id: str) -> dict[str, Any]:
    """Message that removes a surface. A surface id may not be created twice without it."""
    return {"version": A2UI_VERSION, "deleteSurface": {"surfaceId": surface_id}}


def check_messages(messages: list[dict[str, Any]]) -> list[str]:
    """Return the structural problems of a create-then-update message list (empty when sound).

    This is the builder's own check and needs no schema library: the surface is created
    before it is updated, ids are unique, there is exactly one ``root``, every child
    reference resolves, every component hangs off the root, and ``weight`` appears only on
    a direct child of a Row or Column. The tests additionally validate the same messages
    against the published A2UI JSON schemas.
    """
    problems: list[str] = []
    if len(messages) != 2 or "createSurface" not in messages[0] or "updateComponents" not in messages[1]:
        return ["expected one createSurface message followed by one updateComponents message"]
    created, updated = messages[0]["createSurface"], messages[1]["updateComponents"]
    if any(message.get("version") != A2UI_VERSION for message in messages):
        problems.append(f"every message must carry version {A2UI_VERSION}")
    if created.get("surfaceId") != updated.get("surfaceId"):
        problems.append("createSurface and updateComponents name different surfaces")

    components = updated.get("components", [])
    by_id: dict[str, dict[str, Any]] = {}
    for component in components:
        component_id = component.get("id")
        if component_id in by_id:
            problems.append(f"duplicate component id {component_id!r}")
        by_id[component_id] = component
        if component.get("component") not in _USED_COMPONENTS:
            problems.append(f"component {component_id!r} has unexpected type {component.get('component')!r}")
        if component.get("component") == "Text" and not (isinstance(component.get("text"), str) and component["text"]):
            problems.append(f"Text {component_id!r} has no text")
    if "root" not in by_id:
        problems.append("no component has the id 'root'")
        return problems

    def children_of(component: dict[str, Any]) -> list[str]:
        if "child" in component:
            return [component["child"]]
        return list(component.get("children", []))

    reachable: set[str] = set()
    weighted_ok: set[str] = set()
    stack = ["root"]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        component = by_id[current]
        for child in children_of(component):
            if child not in by_id:
                problems.append(f"{current!r} references missing component {child!r}")
                continue
            if component["component"] in ("Row", "Column"):
                weighted_ok.add(child)
            stack.append(child)
    for component_id, component in by_id.items():
        if component_id not in reachable:
            problems.append(f"component {component_id!r} is not reachable from the root")
        if "weight" in component and component_id not in weighted_ok:
            problems.append(f"component {component_id!r} sets weight but is not a direct child of a Row or Column")
    return problems


def main() -> None:
    """Print a card as JSON Lines, one A2UI message per line."""
    parser = argparse.ArgumentParser(description="Print an A2UI v0.9 card for a covered book and scenario")
    parser.add_argument("--card", choices=CARD_KINDS, default=CARD_HOLDING)
    parser.add_argument("--book", default=ripple_view.DEFAULT_BOOK_ID, help="Covered book id")
    parser.add_argument("--ticker", default="UAL", help="Holding to show (holding card only)")
    parser.add_argument("--rate-bps", default=ripple_view.DEFAULT_RATE_BPS, help="Rate shock in basis points")
    parser.add_argument("--oil-pct", default=ripple_view.DEFAULT_OIL_PCT, help="Oil shock in percent")
    args = parser.parse_args()
    try:
        analysis = ripple_view.build_ripple_view(args.book, args.rate_bps, args.oil_pct)
        messages = build_card(analysis, args.card, args.ticker)
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")
    for message in messages:
        print(json.dumps(message, ensure_ascii=False))


if __name__ == "__main__":
    main()
