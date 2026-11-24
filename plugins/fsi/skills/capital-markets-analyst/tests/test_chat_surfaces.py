"""Chat-surface tests: the MCP App server and view, the A2UI cards, and the workstation preview.

Nothing here needs a network, a model or an MCP Apps host. What a real host or A2UI
client does with these payloads is outside what these tests can show.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re
import sys
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource

SKILL_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = SKILL_ROOT.parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import a2ui_cards, fsi_engine, mcp_server, ripple_view, server, tools  # noqa: E402

A2UI_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a2ui_v0_9"
APP_HTML = SKILL_ROOT / "assets" / "mcp-app" / "ripple.html"
STRESS = ("250", "30")


def _wire(result: Any) -> dict[str, Any]:
    """An SDK result as it appears on the wire, without the field newer protocol revisions add."""
    dumped = result.model_dump(by_alias=True, exclude_none=True, mode="json")
    dumped.pop("resultType", None)
    return dumped


def _texts(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Component id -> the text a Markdown renderer displays, for every Text of a card."""
    return {
        component["id"]: a2ui_cards.markdown_unescape(component["text"])
        for component in messages[1]["updateComponents"]["components"]
        if component["component"] == "Text"
    }


def _all_cards(rate_bps: str = "50", oil_pct: str = "25") -> list[tuple[str, str | None, list[dict[str, Any]]]]:
    """Every card of every book under one scenario: (book id, ticker or None, messages)."""
    cards: list[tuple[str, str | None, list[dict[str, Any]]]] = []
    for book_id, book in fsi_engine.load_sample_book()["books"].items():
        analysis = fsi_engine.analyze_book(book_id, rate_bps=rate_bps, oil_pct=oil_pct)
        cards.append((book_id, None, a2ui_cards.build_book_card(analysis)))
        for ticker in book["tickers"]:
            cards.append((book_id, ticker, a2ui_cards.build_holding_card(analysis, ticker)))
    return cards


# --------------------------------------------------------------------------- A2UI cards


@pytest.fixture(scope="module")
def a2ui_validator() -> jsonschema.Draft202012Validator:
    """Validator for one A2UI server-to-client message, built from the published v0.9.1 schemas."""
    envelope = json.loads((A2UI_FIXTURES / "server_to_client.json").read_text(encoding="utf-8"))
    common = json.loads((A2UI_FIXTURES / "common_types.json").read_text(encoding="utf-8"))
    catalog = json.loads((A2UI_FIXTURES / "basic_catalog.json").read_text(encoding="utf-8"))
    catalog_resource = Resource.from_contents(catalog)
    registry = Registry().with_resources(
        [
            (envelope["$id"], Resource.from_contents(envelope)),
            (common["$id"], Resource.from_contents(common)),
            # The envelope names the catalog by a placeholder file name; the spec says to map it to the catalog in use.
            ("https://a2ui.org/specification/v0_9/catalog.json", catalog_resource),
            (catalog["$id"], catalog_resource),
        ]
    )
    jsonschema.Draft202012Validator.check_schema(envelope)
    return jsonschema.Draft202012Validator(envelope, registry=registry)


def test_a2ui_cards_validate_against_the_published_schemas(a2ui_validator: jsonschema.Draft202012Validator) -> None:
    """Every message of every card is valid A2UI v0.9 for the basic catalog."""
    envelope = json.loads((A2UI_FIXTURES / "server_to_client.json").read_text(encoding="utf-8"))
    catalog = json.loads((A2UI_FIXTURES / "basic_catalog.json").read_text(encoding="utf-8"))
    assert a2ui_cards.BASIC_CATALOG_ID == catalog["catalogId"] == catalog["$id"]
    assert a2ui_cards.A2UI_VERSION in envelope["$defs"]["CreateSurfaceMessage"]["properties"]["version"]["enum"]

    cards = _all_cards() + [card for card in _all_cards(*STRESS) if card[1] is None]
    assert len(cards) == 28 + 4 + 4  # 4 book cards and 28 holding cards, plus the 4 book cards under stress
    for book_id, ticker, messages in cards:
        for message in messages:
            errors = [error.message for error in a2ui_validator.iter_errors(message)]
            assert not errors, (book_id, ticker, errors[:2])
        assert a2ui_validator.is_valid(a2ui_cards.delete_surface(messages[0]["createSurface"]["surfaceId"]))


def test_a2ui_schema_check_rejects_broken_messages(a2ui_validator: jsonschema.Draft202012Validator) -> None:
    """The schema check has teeth: each of these one-line mistakes makes a message invalid."""
    good = a2ui_cards.build_holding_card(fsi_engine.analyze_book("next27_flagship", rate_bps="50", oil_pct="25"), "UAL")

    def first_text(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return next(c for c in messages[1]["updateComponents"]["components"] if c["component"] == "Text")

    mistakes = {
        "unknown version": lambda m: m[0].__setitem__("version", "v9"),
        "extra envelope key": lambda m: m[0].__setitem__("note", "x"),
        "missing catalog id": lambda m: m[0]["createSurface"].pop("catalogId"),
        "unknown component": lambda m: first_text(m).__setitem__("component", "Table"),
        "unknown property": lambda m: first_text(m).__setitem__("colour", "red"),
        "unknown variant": lambda m: first_text(m).__setitem__("variant", "huge"),
        "text is not a string": lambda m: first_text(m).__setitem__("text", 7),
    }
    for name, mutate in mistakes.items():
        broken = copy.deepcopy(good)
        mutate(broken)
        assert any(not a2ui_validator.is_valid(message) for message in broken), name


def test_a2ui_cards_are_sound_deterministic_and_bounded() -> None:
    """The builder's own structure check passes, output is repeatable, and the check catches broken trees."""
    analysis = fsi_engine.analyze_book("next27_flagship", rate_bps="50", oil_pct="25")
    holding = a2ui_cards.build_holding_card(analysis, "ual")
    book = a2ui_cards.build_book_card(analysis)
    assert holding == a2ui_cards.build_card(analysis, "holding", "UAL") == a2ui_cards.build_holding_card(analysis, "UAL")
    assert book == a2ui_cards.build_card(analysis, "book")
    for messages in (holding, book):
        assert a2ui_cards.check_messages(messages) == []
        assert [next(iter(set(m) - {"version"})) for m in messages] == ["createSurface", "updateComponents"]
        assert messages[0]["createSurface"]["theme"] == {"agentDisplayName": a2ui_cards.AGENT_DISPLAY_NAME}
        components = messages[1]["updateComponents"]["components"]
        assert {c["component"] for c in components} <= {"Card", "Column", "Row", "Text", "Divider"}
        assert len(json.dumps(messages)) < 32_000

    # A different scenario is a different surface, so a client never sees one id created twice.
    stressed = a2ui_cards.build_holding_card(fsi_engine.analyze_book("next27_flagship", rate_bps="250", oil_pct="30"), "UAL")
    assert stressed[0]["createSurface"]["surfaceId"] != holding[0]["createSurface"]["surfaceId"]

    def broken(mutate: Any) -> list[str]:
        messages = copy.deepcopy(holding)
        mutate(messages[1]["updateComponents"]["components"])
        return a2ui_cards.check_messages(messages)

    assert any("duplicate" in p for p in broken(lambda cs: cs.append(dict(cs[0]))))
    assert any("missing component" in p for p in broken(lambda cs: next(c for c in cs if c["id"] == "body")["children"].append("ghost")))
    assert any("not reachable" in p for p in broken(lambda cs: cs.append({"id": "orphan", "component": "Divider"})))
    assert any("no component has the id 'root'" in p for p in broken(lambda cs: cs.pop()))
    assert any("weight" in p for p in broken(lambda cs: next(c for c in cs if c["id"] == "root").__setitem__("weight", 1)))
    assert a2ui_cards.check_messages(holding[:1]) != []

    with pytest.raises(ValueError, match="not a holding of book"):
        a2ui_cards.build_holding_card(analysis, "NVDA")
    with pytest.raises(ValueError, match="needs a ticker"):
        a2ui_cards.build_card(analysis, "holding")
    with pytest.raises(ValueError, match="Unknown card"):
        a2ui_cards.build_card(analysis, "pie")


def test_a2ui_quotes_are_exact_filing_passages_and_nothing_else_is_quoted() -> None:
    """Quotation marks on a card enclose a bundled passage, character for character, and only that."""
    dataset = fsi_engine.load_sample_book()
    for book_id, ticker, messages in _all_cards(*STRESS):
        shown = _texts(messages)
        quoted = {key: text for key, text in shown.items() if text.startswith("“") and text.endswith("”")}
        for key, text in shown.items():
            if key not in quoted:
                assert not any(mark in text for mark in "“”\""), (book_id, ticker, key, text)
        if ticker is None:
            assert not quoted, (book_id, quoted)
            continue
        bundled = {passage["text"] for passage in fsi_engine.canonical_excerpt_passages(dataset, ticker)}
        assert {text[1:-1] for text in quoted.values()} <= bundled, (book_id, ticker)
        # Every passage behind this issuer's two sensitivities is on its card.
        expected = {
            passage["text"]
            for factor in ("rate", "oil")
            for passage in fsi_engine.sensitivity_passages(dataset["companies"][ticker]["sensitivities"][factor])
        }
        assert {text[1:-1] for text in quoted.values()} == expected, (book_id, ticker)
        company = dataset["companies"][ticker]
        assert shown["document"] == f"{company['filing_doc_id']} · excerpt SHA-256 {fsi_engine.compute_excerpt_sha256(dataset, ticker)}"


def test_markdown_literal_displays_text_character_for_character() -> None:
    """A2UI Text is rendered as Markdown; every string is escaped so it displays unchanged."""
    cases = {
        "2025 | 4,269 | $9,819 | $2.30 | 17%": "2025 | 4,269 | \\$9,819 | \\$2.30 | 17%",
        "*bold* and _em_": "\\*bold\\* and \\_em\\_",
        "# heading": "\\# heading",
        "1. item": "1\\. item",
        "12) item": "12\\) item",
        "- item": "\\- item",
        "+ item": "\\+ item",
        "> quote": "\\> quote",
        "---": "\\---",
        "a\\*b": "a\\\\\\*b",
        "AT&amp;T": "AT\\&amp;T",
        "<b>x</b>": "\\<b>x\\</b>",
        "[t](u)": "\\[t\\](u)",
        "`code` ~~s~~": "\\`code\\` \\~\\~s\\~\\~",
        # Not Markdown: left alone.
        "+100 bps shift in rates | $2.1": "+100 bps shift in rates | \\$2.1",
        "-100 bps": "-100 bps",
        "1.5 million, P&G & Co": "1.5 million, P&G & Co",
    }
    for text, literal in cases.items():
        assert a2ui_cards.markdown_literal(text) == literal
        assert a2ui_cards.markdown_unescape(literal) == text
    for unshowable in ("", " leading", "trailing ", "two\nlines", "tab\there"):
        with pytest.raises(ValueError):
            a2ui_cards.markdown_literal(unshowable)

    # In every card, no Markdown-active character is left unescaped, and the escaping is exactly markdown_literal's.
    active = re.compile(r"(?<!\\)(?:\\\\)*[`*_\[\]<~$]")
    for _, _, messages in _all_cards(*STRESS):
        for component in messages[1]["updateComponents"]["components"]:
            if component["component"] == "Text":
                assert not active.search(component["text"]), component
                assert a2ui_cards.markdown_literal(a2ui_cards.markdown_unescape(component["text"])) == component["text"]


def test_a2ui_cards_show_the_engine_numbers_and_no_advice() -> None:
    """Card figures are the engine's, formatted; a holding with no figure says so; no advisory wording."""
    analysis = fsi_engine.analyze_book("next27_flagship", rate_bps="50", oil_pct="25")
    by_ticker = {holding["ticker"]: holding for holding in analysis["holdings"]}

    ual = _texts(a2ui_cards.build_holding_card(analysis, "UAL"))
    impact = by_ticker["UAL"]["scaled_impact"]
    assert (impact["total_pre_tax_m"], impact["after_tax_m"], impact["eps_delta"]) == ("-2933.42", "-2285.13", "-6.9563")
    assert (ual["pretax-value"], ual["aftertax-value"], ual["eps-value"]) == ("-$2,933.42M", "-$2,285.13M", "-$6.9563 per share")
    assert ual["eps-note"] == "-68.20% of FY EPS"
    assert ual["rate-scaling"] == "Disclosed +$77.00M, scaled by 0.50 (rounded) to +$38.50M"
    assert ual["subtitle"] == "Airlines · Rates +50 bp · Oil +25% · Source: authoritative_filing"
    assert ual["flag-0"].startswith("Warning: Oil shock (+25%) exceeds 2x")
    assert ual["rate-note-0"].startswith("Tool note: Derived net figure.")
    assert ual["notice"].endswith(a2ui_cards.SCALING_NOTE) and "Not investment advice." in ual["notice"]

    boeing = _texts(a2ui_cards.build_holding_card(analysis, "BA"))
    assert boeing["no-figure"] == a2ui_cards.NO_FIGURE_NOTE and "pretax-value" not in boeing
    assert boeing["oil-quote"] == "No passage is bundled for this factor."

    book = _texts(a2ui_cards.build_book_card(analysis))
    totals = analysis["portfolio_totals"]
    assert (totals["aggregate_pre_tax_impact_m"], totals["aggregate_after_tax_impact_m"]) == ("-4286.42", "-3402.21")
    assert (book["pretax-value"], book["aftertax-value"]) == ("-$4,286.42M", "-$3,402.21M")
    assert (book["holdings-value"], book["holdings-note"]) == ("12", "5 with a convertible figure")
    assert book["converted-title"].endswith("(5)") and book["unconverted-title"].endswith("(7)")
    assert book["holding-JPM-pretax"] == "+$1,050.00M pre-tax" and book["holding-JPM-eps"] == "+$0.2967 per share"
    assert book["holding-DAL-name"] == "DAL · Delta Air Lines, Inc. (beyond 2x the disclosed basis)"
    assert book["holding-BA-status"] == "BA · The Boeing Company: rates, no amount disclosed; oil, no passage located"
    assert book["link-E07"] == "CLX sells through retailer WMT · 26% disclosed share · CLX-10K-FY2026"
    assert book["link-E04"] == "BA sells aircraft to DAL · no share disclosed · DAL-10K-FY2025"
    assert sum(key.startswith("link-E") for key in book) == len(analysis["contagion_graph"]["edges"]) == 10

    megacap_analysis = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="25")
    megacap = _texts(a2ui_cards.build_book_card(megacap_analysis))
    assert sum(key.startswith("link-E") for key in megacap) == 4
    assert "QRVO" in megacap["link-E09"] and "50% disclosed share" in megacap["link-E09"]

    # A book whose shocked holdings no disclosed link reaches says so instead of listing nothing.
    no_links = copy.deepcopy(megacap_analysis)
    no_links["contagion_graph"]["edges"] = []
    empty_card = _texts(a2ui_cards.build_book_card(no_links))
    assert empty_card["links-none"].startswith("No disclosed link") and empty_card["links-title"].endswith("(0)")

    for _, _, messages in _all_cards(*STRESS):
        for text in _texts(messages).values():
            assert fsi_engine.enforce_non_advisory_guardrail(text)["allowed"], text


def test_status_labels_are_the_same_on_every_surface() -> None:
    """The workstation, the MCP App view and the A2UI cards name a disclosure status identically."""
    assert tuple(a2ui_cards.STATUS_LABELS) == fsi_engine.SENSITIVITY_STATUSES

    def js_labels(path: Path) -> dict[str, str]:
        block = re.search(r"const STATUS_LABEL = \{(.*?)\};", path.read_text(encoding="utf-8"), re.DOTALL)
        assert block, path
        return dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))

    assert js_labels(SKILL_ROOT / "assets" / "ui" / "app.js") == a2ui_cards.STATUS_LABELS
    assert js_labels(APP_HTML) == a2ui_cards.STATUS_LABELS


# --------------------------------------------------------------------------- MCP App server


def test_mcp_tools_follow_the_tool_definition_standard() -> None:
    """Name, description, described inputs, output schema, annotations, and the MCP Apps metadata."""
    listed = {tool.name: _wire(tool) for tool in asyncio.run(mcp_server.server.list_tools())}
    assert list(listed) == [ripple_view.SHOW_TOOL, ripple_view.RECOMPUTE_TOOL]

    for name, tool in listed.items():
        assert len(tool["description"]) > 150 and "Returns an error" in tool["description"], name
        schema = tool["inputSchema"]
        assert set(schema["properties"]) == {"book_id", "book_name", "rate_bps", "oil_pct"}
        # A host opens the view from its menus with no arguments, so nothing may be required.
        assert not schema.get("required")
        for prop_name, prop in schema["properties"].items():
            assert prop["type"] == "string" and len(prop["description"]) > 20 and "default" in prop, (name, prop_name)
        assert set(tool["outputSchema"]["required"]) == set(ripple_view.RippleView.model_fields)
        assert all(prop.get("description") for prop in tool["outputSchema"]["properties"].values())
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }

    show, recompute = listed[ripple_view.SHOW_TOOL], listed[ripple_view.RECOMPUTE_TOOL]
    assert "Do not use it for" in show["description"]
    assert show["_meta"]["ui"] == {"resourceUri": ripple_view.APP_RESOURCE_URI}
    assert ripple_view.APP_RESOURCE_URI.startswith("ui://")
    assert {entry["type"] for entry in show["_meta"]["com.google.antigravity/ui"]["entrypoints"]} == {"aux_pane", "global"}
    # Only the view may call the second tool, and it declares no view of its own.
    assert recompute["_meta"] == {"ui": {"visibility": ["app"]}}
    # The shock descriptions are the agent tool's, so the two cannot drift. The book description is built
    # from the dataset: it names every covered book by id and by display name, so a model can pick either.
    for shock in ("rate_bps", "oil_pct"):
        assert show["inputSchema"]["properties"][shock]["description"] == tools.BookShockParams.model_fields[shock].description
    for book_id, book in fsi_engine.load_sample_book()["books"].items():
        assert f"{book_id} ({book['label']})" in show["inputSchema"]["properties"]["book_id"]["description"]


def test_mcp_result_is_the_agent_tools_analysis_and_fits_its_schema() -> None:
    """The structured result carries the evaluated tool's numbers; the text is a summary without passages."""
    listed = {tool.name: _wire(tool) for tool in asyncio.run(mcp_server.server.list_tools())}
    result = _wire(asyncio.run(mcp_server.server.call_tool(ripple_view.SHOW_TOOL, {})))
    assert result["isError"] is False and [block["type"] for block in result["content"]] == ["text"]
    view = result["structuredContent"]
    jsonschema.Draft202012Validator(listed[ripple_view.SHOW_TOOL]["outputSchema"]).validate(view)

    agent_tool = tools.run_portfolio_shock_analysis(
        tools.BookShockParams(book_id="next27_flagship", rate_bps=ripple_view.DEFAULT_RATE_BPS, oil_pct=ripple_view.DEFAULT_OIL_PCT)
    )
    for key in ("book_id", "book_label", "scenario", "portfolio_totals", "holdings", "contagion_graph"):
        assert view[key] == agent_tool[key], key
    # The view is read-only: the typed sign-off confirmation is not sent to a chat surface.
    assert set(view["governance"]) == {"packet_sha256", "source_posture", "data_notice", "as_of_utc"}
    assert view["governance"]["packet_sha256"] == agent_tool["governance"]["packet_sha256"]
    assert view["governance"]["source_posture"] == fsi_engine.load_sample_book()["metadata"]["source_posture"]
    assert [book["id"] for book in view["books"]] == list(fsi_engine.load_sample_book()["books"])
    assert [preset["id"] for preset in view["scenario_presets"]] == [s["id"] for s in fsi_engine.load_sample_book()["scenarios"]]
    assert all(isinstance(preset["rate_bps"], str) and isinstance(preset["oil_pct"], str) for preset in view["scenario_presets"])

    text = result["content"][0]["text"]
    assert text == ripple_view.summarize_for_model(view)
    assert "pre-tax -4,286.42 USD million, after-tax -3,402.21 USD million" in text
    assert "- UAL (United Airlines Holdings, Inc.): pre-tax -2,933.42 USD million" in text
    assert "Holdings with no convertible figure (7)" in text and "- CLX -> WMT: SELLS_THROUGH_RETAILER, 26% revenue share" in text
    assert "do not quote filing text from memory" in text and "Not investment advice." in text
    # No filing passage travels in the text the model reads.
    passages = {p["text"] for t in fsi_engine.load_sample_book()["companies"] for p in fsi_engine.canonical_excerpt_passages(fsi_engine.load_sample_book(), t)}
    assert not any(passage in text for passage in passages) and "“" not in text
    assert fsi_engine.enforce_non_advisory_guardrail(text)["allowed"]
    # Hosts store one tool result up to a documented size; this one is far below it.
    assert len(json.dumps(result).encode("utf-8")) < ripple_view.MAX_TOOL_RESULT_BYTES // 10

    # Same call, same result (the tool is declared idempotent).
    assert _wire(asyncio.run(mcp_server.server.call_tool(ripple_view.RECOMPUTE_TOOL, {}))) == result


def test_mcp_tool_errors_and_numeric_arguments() -> None:
    """Expected failures come back as error results the model can read; JSON numbers are accepted exactly."""
    call = lambda args: _wire(asyncio.run(mcp_server.server.call_tool(ripple_view.RECOMPUTE_TOOL, args)))  # noqa: E731

    unknown = call({"book_id": "not_a_book"})
    assert unknown["isError"] is True and "structuredContent" not in unknown
    assert unknown["content"][0]["text"].startswith("unknown_book:") and "next27_flagship" in unknown["content"][0]["text"]
    for bad in ("abc", "", "NaN", "Infinity"):
        result = call({"rate_bps": bad})
        assert result["isError"] is True and result["content"][0]["text"].startswith("invalid_input:"), bad

    # A model may send 25 where "25" is declared. The number is read back as decimal text.
    numbers = call({"book_id": "aero_transport", "rate_bps": 25, "oil_pct": -20.5})
    strings = call({"book_id": "aero_transport", "rate_bps": "25", "oil_pct": "-20.5"})
    assert numbers["isError"] is False and numbers == strings
    assert (numbers["structuredContent"]["scenario"]["rate_bps"], numbers["structuredContent"]["scenario"]["oil_pct"]) == ("25", "-20.5")

    # A boolean is not a number: the SDK refuses it before the tool runs.
    with pytest.raises(Exception, match="not a boolean"):
        asyncio.run(mcp_server.server.call_tool(ripple_view.SHOW_TOOL, {"rate_bps": True}))
    assert ripple_view.call_tool_result(rate_bps=True)["content"][0]["text"].startswith("invalid_input:")  # type: ignore[arg-type]


def test_mcp_view_is_one_self_contained_document() -> None:
    """The ui:// resource is the HTML file, with the MCP Apps MIME type, and it loads nothing from outside."""
    resources = [_wire(resource) for resource in asyncio.run(mcp_server.server.list_resources())]
    assert [(r["uri"], r["mimeType"], r["_meta"]) for r in resources] == [
        (ripple_view.APP_RESOURCE_URI, "text/html;profile=mcp-app", {"ui": {"prefersBorder": True}})
    ]
    contents = list(asyncio.run(mcp_server.server.read_resource(ripple_view.APP_RESOURCE_URI)))
    assert len(contents) == 1 and contents[0].mime_type == ripple_view.APP_MIME_TYPE
    html = contents[0].content
    assert html == APP_HTML.read_text(encoding="utf-8") == ripple_view.load_app_html()
    assert html.lower().startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert len(html.encode("utf-8")) < ripple_view.MAX_APP_HTML_BYTES // 100

    # The handshake and data messages of the MCP Apps protocol, and the one tool the view may call.
    for needed in (
        '"ui/initialize"',
        '"ui/notifications/initialized"',
        '"ui/notifications/tool-input"',
        '"ui/notifications/tool-result"',
        '"ui/notifications/tool-cancelled"',
        '"ui/notifications/host-context-changed"',
        '"ui/notifications/size-changed"',
        '"ui/resource-teardown"',
        '"tools/call"',
        "ping:",
        f'RECOMPUTE_TOOL = "{ripple_view.RECOMPUTE_TOOL}"',
    ):
        assert needed in html, needed
    assert ripple_view.SHOW_TOOL not in html  # the view never calls the model's tool

    # Nothing is fetched: the host's default content security policy allows no network at all.
    assert set(re.findall(r"https?://[^\s\"'<>)]+", html)) == {"http://www.w3.org/2000/svg"}
    for forbidden in ("<script src", "<link", "<img", "<iframe", "<form", "fetch(", "XMLHttpRequest", "WebSocket", "import(", "@import", "url(http"):
        assert forbidden not in html, forbidden
    # Tool data reaches the page as text nodes only, and nothing is kept in browser storage.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "sessionStorage", "document.cookie"):
        assert forbidden not in html, forbidden
    assert html.count("<script>") == 1 and html.count("<style>") == 1

    with pytest.raises(FileNotFoundError):
        ripple_view.load_app_html(SKILL_ROOT / "assets" / "mcp-app" / "missing.html")


def test_mcp_server_over_stdio() -> None:
    """Start the server as a host would and run the whole exchange through the SDK's stdio client."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exchange() -> dict[str, Any]:
        params = StdioServerParameters(command=sys.executable, args=[str(SKILL_ROOT / "scripts" / "mcp_server.py")], cwd="/")
        ui_extension = {"io.modelcontextprotocol/ui": {"mimeTypes": [ripple_view.APP_MIME_TYPE]}}
        async with stdio_client(params) as (read, write), ClientSession(read, write, extensions=ui_extension) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            default = await session.call_tool(ripple_view.SHOW_TOOL, {})
            other = await session.call_tool(ripple_view.RECOMPUTE_TOOL, {"book_id": "defensive_consumer", "rate_bps": "25", "oil_pct": "0"})
            unknown = await session.call_tool(ripple_view.SHOW_TOOL, {"book_id": "not_a_book"})
            by_name = await session.call_tool(ripple_view.SHOW_TOOL, {"book_name": "Mega-Cap Tech & AI Supply Chain"})
            stray = await session.call_tool(ripple_view.SHOW_TOOL, {"book": "megacap_tech"})
            wrong_type = await session.call_tool(ripple_view.SHOW_TOOL, {"rate_bps": True})
            document = await session.read_resource(ripple_view.APP_RESOURCE_URI)
            return {
                "capabilities": _wire(initialized.capabilities),
                "server": _wire(initialized.server_info),
                "tools": [_wire(tool) for tool in listed.tools],
                "default": _wire(default),
                "other": _wire(other),
                "unknown": _wire(unknown),
                "by_name": _wire(by_name),
                "stray": _wire(stray),
                "wrong_type": _wire(wrong_type),
                "document": document.contents[0],
            }

    got = asyncio.run(asyncio.wait_for(exchange(), timeout=60))
    assert got["server"]["name"] == mcp_server.SERVER_NAME
    # A host refuses to render the view unless the server declares resources.
    assert "tools" in got["capabilities"] and "resources" in got["capabilities"]
    assert [tool["name"] for tool in got["tools"]] == [ripple_view.SHOW_TOOL, ripple_view.RECOMPUTE_TOOL]
    assert got["tools"][0]["_meta"]["ui"]["resourceUri"] == ripple_view.APP_RESOURCE_URI
    # Over the wire the result is what the in-process call produces.
    assert got["default"] == ripple_view.call_tool_result()
    assert got["other"]["structuredContent"]["book_label"] == "Defensive Consumer & Staples"
    assert got["unknown"]["isError"] is True and got["unknown"]["content"][0]["text"].startswith("unknown_book:")
    # The book named by its display name is the book shown; an undeclared argument is refused over the wire too.
    assert got["by_name"]["isError"] is False and got["by_name"]["structuredContent"]["book_id"] == "megacap_tech"
    assert got["stray"]["isError"] is True and "has no argument named book" in got["stray"]["content"][0]["text"]
    assert "megacap_tech (Mega-Cap Tech & AI Supply Chain)" in got["stray"]["content"][0]["text"]
    assert got["wrong_type"]["isError"] is True and "not a boolean" in got["wrong_type"]["content"][0]["text"]
    assert got["document"].mime_type == ripple_view.APP_MIME_TYPE
    assert got["document"].text == APP_HTML.read_text(encoding="utf-8")
    assert _wire(got["document"])["_meta"] == {"ui": {"prefersBorder": True}}


def test_mcp_server_over_streamable_http() -> None:
    """Streamable HTTP (/mcp) serves both minimal and full-suite modes and leaves the stdio server untouched."""
    from starlette.testclient import TestClient

    with TestClient(mcp_server.build_streamable_http_app(full_suite=True)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["full_suite"] is True
        # Clients such as Discovery Engine may omit Accept: text/event-stream.
        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert ripple_view.SHOW_TOOL in names and "push_scenario_from_widget" in names
        assert "lookup_issuer_10k_facts" in names and "audit_book_10k_deltas" in names
        facts = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lookup_issuer_10k_facts", "arguments": {"ticker": "SWKS"}}},
        ).json()
        assert facts["result"]["isError"] is False
        assert facts["result"]["structuredContent"]["filing_doc_id"] == "SWKS-10K-FY2026"

    # Building the full-suite HTTP app never mutates the default stdio server's two-tool surface.
    assert [t.name for t in asyncio.run(mcp_server.server.list_tools())] == [
        ripple_view.SHOW_TOOL,
        ripple_view.RECOMPUTE_TOOL,
    ]


# --------------------------------------------------------------------------- workstation preview and packaging


def test_workstation_preview_returns_what_the_mcp_server_returns() -> None:
    """The preview page stands in for a host, so its endpoints must return the server's own payloads."""
    for name, args in (
        (ripple_view.SHOW_TOOL, {}),
        (ripple_view.RECOMPUTE_TOOL, {"book_id": "megacap_tech", "rate_bps": "-50", "oil_pct": "-20"}),
        (ripple_view.SHOW_TOOL, {"book_id": "not_a_book"}),
        (ripple_view.RECOMPUTE_TOOL, {"oil_pct": "abc"}),
    ):
        from_server = _wire(asyncio.run(mcp_server.server.call_tool(name, args)))
        assert server.handle_mcp_app_call({"name": name, "arguments": args}) == from_server, (name, args)
    refused = server.handle_mcp_app_call({"name": "export_formula_workbook", "arguments": {}})
    assert refused["isError"] is True and "Unknown tool" in refused["content"][0]["text"]
    assert server.handle_mcp_app_call({"name": ripple_view.SHOW_TOOL})["isError"] is False
    # The brief the view draws is served by the preview too, and is what the full MCP server returns.
    suite = mcp_server._build_full_suite_server()
    for args in ({"book_id": "megacap_tech", "tickers_csv": "INTC"}, {"book_id": "next27_flagship", "tickers_csv": "HXL,BA"}, {"tickers_csv": ""}):
        from_suite = _wire(asyncio.run(suite.call_tool(ripple_view.BRIEF_TOOL, args)))
        assert server.handle_mcp_app_call({"name": ripple_view.BRIEF_TOOL, "arguments": args}) == from_suite, args
    assert server.handle_mcp_app_call({"name": ripple_view.BRIEF_TOOL, "arguments": {"tickers_csv": ""}})["isError"] is True
    by_name = server.handle_mcp_app_call({"name": ripple_view.SHOW_TOOL, "arguments": {"book_name": "Mega-Cap Tech"}})
    assert by_name["structuredContent"]["book_id"] == "megacap_tech"

    document = server.handle_mcp_app_resource()
    assert document == {
        "uri": ripple_view.APP_RESOURCE_URI,
        "mimeType": ripple_view.APP_MIME_TYPE,
        "text": APP_HTML.read_text(encoding="utf-8"),
    }

    body = {"card": "holding", "ticker": "DAL", "book_id": "next27_flagship", "rate_bps": "50", "oil_pct": "25"}
    payload = server.handle_a2ui_request(body)
    analysis = fsi_engine.analyze_book("next27_flagship", rate_bps="50", oil_pct="25")
    assert payload["messages"] == a2ui_cards.build_holding_card(analysis, "DAL")
    assert (payload["version"], payload["catalog_id"], payload["mime_type"]) == ("v0.9", a2ui_cards.BASIC_CATALOG_ID, "application/a2ui+json")
    assert payload["surface_id"] == payload["messages"][0]["createSurface"]["surfaceId"]
    assert payload["holdings"] == fsi_engine.load_sample_book()["books"]["next27_flagship"]["tickers"]
    assert server.handle_a2ui_request({**body, "card": "book"})["messages"] == a2ui_cards.build_book_card(analysis)
    for bad in ({**body, "ticker": "NVDA"}, {**body, "ticker": None}, {**body, "card": "pie"}, {**body, "book_id": "not_a_book"}):
        with pytest.raises(ValueError):
            server.handle_a2ui_request(bad)

    # The preview page is static and ships with the workstation; it loads nothing from outside either.
    page = (SKILL_ROOT / "assets" / "ui" / "chat-surfaces.html").read_text(encoding="utf-8")
    script = (SKILL_ROOT / "assets" / "ui" / "chat-surfaces.js").read_text(encoding="utf-8")
    assert 'sandbox="allow-scripts"' in page and 'src="chat-surfaces.js"' in page
    assert "default-src 'none'" in script and "connect-src 'none'" in script
    assert not re.findall(r"https?://", page + script)
    for claim in ("not an MCP Apps host", "not the official A2UI renderer"):
        assert claim in page, claim


def test_preview_page_reads_as_a_chat_and_keeps_the_wire_details_closed() -> None:
    """The preview is laid out as a conversation; what goes over the wire sits in a section that starts closed."""
    page = (SKILL_ROOT / "assets" / "ui" / "chat-surfaces.html").read_text(encoding="utf-8")
    script = (SKILL_ROOT / "assets" / "ui" / "chat-surfaces.js").read_text(encoding="utf-8")

    # One note, always on screen, says what the page is not. The claims are not repeated around the page.
    note = re.search(r'<p class="preview-note" id="preview-note">(.*?)</p>', page, re.DOTALL)
    assert note is not None
    for claim in ("not an MCP Apps host", "not the official A2UI renderer", "no agent runs here"):
        assert claim in note.group(1) and page.count(claim) == 1, claim

    # The chat comes first, in reading order: a request, the view in its frame, a request, the card.
    # The developer section has no "open" attribute, so it starts closed.
    chat, developer = page.split('<details class="dev-panel" id="dev-panel">')
    places = [chat.index(f'id="{name}"') for name in ("mcp-prompt", "app-frame", "view-calls", "a2ui-prompt", "card-picker", "a2ui-mount")]
    assert places == sorted(places)
    for name in ("mcp-text", "rpc-log", "a2ui-json", "a2ui-facts", "mcp-call", "mcp-theme", "a2ui-build"):
        assert f'id="{name}"' not in chat, name
        assert developer.index(f'id="{name}"') < developer.index("</details>"), name

    # The frame is capped as an inline view is. Its border belongs to the host box around it,
    # so the frame is exactly as tall as the view reports.
    assert "const INLINE_MAX_HEIGHT = 800;" in script
    assert "containerDimensions: { maxHeight: INLINE_MAX_HEIGHT }" in script
    assert re.search(r"#app-frame \{[^}]*\bborder: 0;", page)

    # As in a chat, only the model's call writes the request above the view. A call the view
    # makes itself is counted on the line under the view instead.
    writes = [match.start() for match in re.finditer(re.escape('$("mcp-prompt").textContent ='), script)]
    assert len(writes) == 1
    assert script.index("async function modelCall(") < writes[0] < script.index('window.addEventListener("message"')
    assert "state.viewCalls += 1;" in script and '$("view-calls").textContent' in script


def test_plugin_mcp_json_declares_the_server() -> None:
    """plugins/fsi/mcp.json follows the Agent Plugins 1.0.0 shape and points at a script that exists."""
    config = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))
    assert set(config) == {"$schema", "mcpServers"}
    assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert list(config["mcpServers"]) == ["capital-markets-ripple"]
    entry = config["mcpServers"]["capital-markets-ripple"]
    assert set(entry) == {"type", "command", "args", "cwd"} and entry["type"] == "stdio"
    assert entry["command"] == "uv" and entry["args"][:3] == ["run", "--extra", "mcp"]
    # The working directory is plugin-relative, as the schema requires, and the script is found from it.
    assert re.match(r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))", entry["cwd"])
    working_dir = (PLUGIN_ROOT / entry["cwd"]).resolve()
    assert working_dir == SKILL_ROOT and (working_dir / entry["args"][-1]).is_file()
    assert entry["args"][-1] == "scripts/mcp_server.py"

    pyproject = (SKILL_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^mcp = \[\s*"mcp>=2\.0\.0,<3\.0\.0",\s*\]', pyproject, re.MULTILINE)
    lock = (SKILL_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "mcp"' in lock and 'name = "jsonschema"' in lock


def test_the_agent_and_the_workstation_do_not_depend_on_the_chat_surfaces() -> None:
    """The evaluated agent is untouched by these modules, and the workstation runs without the MCP SDK."""
    scripts = SKILL_ROOT / "scripts"
    for evaluated in ("agent.py", "tools.py", "fsi_engine.py", "eval_runner.py", "model_exporter.py", "filing_compare.py"):
        source = (scripts / evaluated).read_text(encoding="utf-8")
        for module in ("mcp_server", "ripple_view", "a2ui_cards"):
            assert module not in source, (evaluated, module)
    for sdk_free in ("server.py", "ripple_view.py", "a2ui_cards.py"):
        source = (scripts / sdk_free).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:from|import)\s+(?:mcp|google\.adk)\b", source, re.MULTILINE), sdk_free
    # The agent still has exactly its ten evaluated tools; the view tool is not one of them.
    assert len(tools.AGENT_TOOLS) == 10 and ripple_view.SHOW_TOOL not in tools.PARAM_MODELS

    # Vendored A2UI schemas are attributed and unmodified in identity.
    note = (A2UI_FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "Apache-2.0" in note and "a2ui.org/specification/v0.9.1" in note


# --------------------------------------------------------------------------- found in a recorded run, 2026-11-24


def test_mcp_never_shows_a_book_other_than_the_one_named() -> None:
    """A host's model sent ``book_name``, which the tool did not declare, and was shown the default book.

    The book may now be named by id or by display name, a name that fits no book is an error, and an
    argument the tool does not declare is refused: the SDK alone drops it and runs with the defaults.
    """
    call = lambda args: _wire(asyncio.run(mcp_server.server.call_tool(ripple_view.SHOW_TOOL, args)))  # noqa: E731
    label = "Mega-Cap Tech & AI Supply Chain"
    for args in ({"book_name": label}, {"book_id": label}, {"book_name": "Mega-Cap Tech"}, {"book_id": "megacap_tech"}):
        result = call(args)
        assert result["isError"] is False and result["structuredContent"]["book_id"] == "megacap_tech", args
        assert result["content"][0]["text"].startswith(f"Ripple-graph view: {label} (book_id megacap_tech)"), args

    for args in ({"book_name": "Emerging Markets"}, {"book_name": "a"}, {"book_id": "next27_flagship", "book_name": label}):
        result = call(args)
        assert result["isError"] is True and "structuredContent" not in result, args
        text = result["content"][0]["text"]
        assert text.startswith("unknown_book:") and f"megacap_tech ({label})" in text, args

    for stray in ({"book": "megacap_tech"}, {"portfolio": label, "rate_bps": "50"}):
        with pytest.raises(Exception, match="has no argument named"):
            asyncio.run(mcp_server.server.call_tool(ripple_view.SHOW_TOOL, stray))
    suite = mcp_server._build_full_suite_server()
    with pytest.raises(Exception, match="has no argument named tickers"):
        asyncio.run(suite.call_tool("lookup_3yr_reported_financials", {"tickers": "UAL"}))

    # With no book at all the default book is shown: a host opens the view from its menus with {}.
    assert call({})["structuredContent"]["book_id"] == ripple_view.DEFAULT_BOOK_ID


def test_full_suite_text_is_short_enough_to_be_read_whole() -> None:
    """A host showed its model the first 200 lines of a 494-line JSON result, and the model filled the rest in.

    The text of the two large tools is now a table that holds every value; the JSON stays in the structured content.
    """
    suite = mcp_server._build_full_suite_server()
    reported = _wire(asyncio.run(suite.call_tool("lookup_3yr_reported_financials", {"tickers_csv": "UAL"})))
    text, payload = reported["content"][0]["text"], reported["structuredContent"]
    assert reported["isError"] is False and len(text) < 5000 < len(json.dumps(payload, indent=2))
    assert "Revenue [USD millions] | 59070 | 57063 | 53717" in text
    company = payload["companies"][0]
    for key, item in payload["line_items"].items():
        values = [(year["reported"].get(key) or {}).get("value") for year in company["fiscal_years"]]
        if any(value is not None for value in values):
            cells = " | ".join("not tagged" if value is None else value for value in values)
            assert f"{item['label']} [{item['unit']}] | {cells}" in text, key
    for key, metric in payload["computed_metrics"].items():
        values = [year["computed"].get(key) for year in company["fiscal_years"]]
        if any(value is not None for value in values):
            cells = " | ".join("not computed" if value is None else value for value in values)
            assert f"{metric['label']} [{metric.get('unit', '')}] | {cells}" in text, key
    assert "$" not in text and "\\" not in text and fsi_engine.enforce_non_advisory_guardrail(text)["allowed"]

    deltas = _wire(asyncio.run(suite.call_tool("audit_book_10k_deltas", {"book_id": "next27_flagship"})))
    text, payload = deltas["content"][0]["text"], deltas["structuredContent"]
    assert len(text) < 6000 < len(json.dumps(payload, indent=2))
    for row in payload["rows"]:
        assert f"- {row['ticker']} ({row['name']}): {row['metric']}." in text and row["change_summary"] in text

    unknown = _wire(asyncio.run(suite.call_tool("audit_book_10k_deltas", {"book_id": "nope"})))
    assert unknown["isError"] is True and unknown["content"][0]["text"].startswith("unknown_book:")

    # Every tool tells the host's model the same thing about copying figures.
    for rules in (mcp_server.server.instructions, suite.instructions, reported["content"][0]["text"]):
        assert "copy it exactly" in rules and "without LaTeX" in rules and "Do not recommend an action" in rules


def test_the_view_draws_its_brief_itself_and_sends_no_prompt_to_the_chat() -> None:
    """The brief button asked the host's model to summarise; the summary contradicted the view. It now draws the tool's figures."""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "ui/message" not in html and '"brief_selection"' in html
    suite = mcp_server._build_full_suite_server()
    listed = {tool.name: _wire(tool) for tool in asyncio.run(suite.list_tools())}
    assert listed["brief_selection"]["_meta"] == {"ui": {"visibility": ["app"]}}
    brief = _wire(asyncio.run(suite.call_tool("brief_selection", {"book_id": "megacap_tech", "tickers_csv": "INTC"})))
    years = brief["structuredContent"]["summaries"][0]["years"]
    # Intel as reported: fiscal 2024 revenue 53,101, operating loss 11,678, net loss 18,756 (USD millions).
    assert [(y["fy"], y["revenue_m"], y["op_inc_m"], y["net_inc_m"]) for y in years] == [
        (2025, "52853", "-2214", "-267"),
        (2024, "53101", "-11678", "-18756"),
        (2023, "54228", "93", "1689"),
    ]
    # The context a host attaches to the user's next message names the figures of the book by their real keys.
    assert "aggregate_pre_tax_impact_m" in html and "portfolio_totals.total_pre_tax_m" not in html


def test_tool_names_fit_the_prefix_a_host_adds() -> None:
    """One host names a plugin tool ``mcp_<plugin>_<server>_<tool>`` and disables any name longer than 64 characters."""
    plugin = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))["name"]
    (server_key,) = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    names = [tool.name for tool in asyncio.run(mcp_server._build_full_suite_server().list_tools())]
    too_long = [name for name in names if len(f"mcp_{plugin}_{server_key}_{name}") > 64]
    assert len(names) == 11 and not too_long, too_long


def test_a_holding_taxed_at_the_statutory_rate_says_so_on_every_chat_surface() -> None:
    analysis = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="25")
    card = " ".join(_texts(a2ui_cards.build_holding_card(analysis, "INTC")).values())
    assert "statutory rate of 21" in card and "of FY EPS not meaningful" in card
    text = ripple_view.call_tool_result("megacap_tech", "50", "25")["content"][0]["text"]
    assert "after-tax -77.75 USD million" in text and "- INTC (Intel Corporation): pre-tax -48.50 USD million, after-tax -38.32 USD million" in text
    assert "Note on INTC: Tax: after-tax impact and EPS use the US federal statutory rate of 21%" in text
    html = APP_HTML.read_text(encoding="utf-8")
    assert "holding.conversion_notes" in html and "% of FY EPS not meaningful" in html


def test_every_get_route_of_the_workstation_answers_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler functions were tested, the routes were not: a NameError in the download route dropped the socket."""
    from http.server import ThreadingHTTPServer
    import io
    import threading
    import urllib.error
    import urllib.request
    import zipfile

    monkeypatch.setattr(server, "_SHEET_SETTINGS", ("", ""))
    monkeypatch.setattr(server, "_LATEST_EVAL", {"run_id": "test-run", "mode": "offline", "verdict": "deterministic_only"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.WorkstationRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str) -> tuple[int, str, str, bytes]:
        try:
            with urllib.request.urlopen(base + path, timeout=30) as response:  # noqa: S310 - loopback test server
                headers = response.headers
                return response.status, headers.get("Content-Type", ""), headers.get("Content-Disposition", ""), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), "", error.read()

    try:
        for path in ("/api/bootstrap", "/api/eval/status", "/api/ledger", "/api/sheet", "/api/mcp_app/resource"):
            status, content_type, _, raw = get(path)
            assert status == 200 and content_type.startswith("application/json") and isinstance(json.loads(raw), dict), path
        for path in ("/", "/index.html", "/app.js", "/chat-surfaces.html"):
            status, _, _, raw = get(path)
            assert status == 200 and raw, path

        status, content_type, disposition, raw = get("/api/export.xlsx?book_id=megacap_tech&rate_bps=50&oil_pct=25")
        assert status == 200 and content_type.endswith("spreadsheetml.sheet")
        assert disposition == 'attachment; filename="fsi_megacap_tech_rate50_oil25.xlsx"'
        assert len(zipfile.ZipFile(io.BytesIO(raw)).namelist()) > 4
        # A file name is built from the query string, so only letters, digits and . _ + - reach the header.
        _, _, disposition, _ = get("/api/export.xlsx?book_id=megacap_tech&rate_bps=-25&oil_pct=10.5")
        assert disposition == 'attachment; filename="fsi_megacap_tech_rate-25_oil10.5.xlsx"'

        status, content_type, _, raw = get("/api/export.xlsx?book_id=no_such_book")
        assert status == 400 and "Cannot export workbook" in json.loads(raw)["error"]
        assert get("/no-such-file.html")[0] == 404 and get("/../scripts/server.py")[0] == 404

        # A failure inside a route is reported as JSON; it used to close the connection with no reply.
        monkeypatch.setattr(server, "handle_eval_status", lambda: 1 / 0)
        status, content_type, _, raw = get("/api/eval/status")
        assert status == 500 and json.loads(raw)["error"].startswith("Server error: ZeroDivisionError")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_full_suite_descriptions_say_when_to_use_each_tool_and_name_a_sibling() -> None:
    """A host's model picks a tool from its description; one host answered a 10-K question from a search instead."""
    suite = mcp_server._build_full_suite_server()
    listed = {tool.name: _wire(tool) for tool in asyncio.run(suite.list_tools())}
    for_model = {
        name: " ".join(tool["description"].split())
        for name, tool in listed.items()
        if (tool.get("_meta") or {}).get("ui", {}).get("visibility") != ["app"]
    }
    assert sorted(for_model) == [
        "audit_book_10k_deltas",
        "lookup_3yr_reported_financials",
        "lookup_issuer_10k_facts",
        "read_scenario_from_google_sheet",
        "show_ripple_graph",
        "sync_comps_to_google_sheet",
        "sync_scenario_to_google_sheet",
    ]
    for name, description in for_model.items():
        assert "Use it" in description and "Returns an error" in description, name
        assert any(other in description for other in set(for_model) - {name}), name
    # Under the full suite the view tool points to the tools that answer what it does not.
    show = for_model["show_ripple_graph"]
    assert "they belong to the skill's ADK agent" not in show
    assert "use audit_book_10k_deltas" in show and "use lookup_3yr_reported_financials" in show
    assert "rather than a web or document search" in for_model["lookup_3yr_reported_financials"]
    # The view-only server has no such tools, and its description still says so.
    only = {tool.name: _wire(tool) for tool in asyncio.run(mcp_server.server.list_tools())}
    assert "this server has no tool for those" in " ".join(only["show_ripple_graph"]["description"].split())


def test_the_book_total_says_how_many_holdings_it_adds_up() -> None:
    """The total adds only holdings whose filing states a figure; one screen labelled it with every holding of the book."""
    flagship = ripple_view.call_tool_result("next27_flagship", "50", "25")["content"][0]["text"]
    assert (
        "Sum over the 5 of 12 holdings whose filing states a convertible figure: pre-tax -4,286.42 USD million, "
        "after-tax -3,402.21 USD million." in flagship
    )
    megacap = ripple_view.call_tool_result("megacap_tech", "50", "25")
    assert "Sum over the 2 of 5 holdings whose filing states a convertible figure: pre-tax -96.00 USD million" in megacap["content"][0]["text"]

    # A book in which every holding states a figure needs no such qualification.
    view = copy.deepcopy(megacap["structuredContent"])
    view["holdings"] = [holding for holding in view["holdings"] if ripple_view.has_convertible_figure(holding)]
    view["portfolio_totals"]["holdings_count"] = len(view["holdings"])
    assert "Sum over all 2 holdings: pre-tax -96.00 USD million" in ripple_view.summarize_for_model(view)

    # The same count is stated where the workstation and the view print the total.
    app_js = (SKILL_ROOT / "assets" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "state no figure" in app_js and "`${tot.holdings_count} holdings`" not in app_js
    html = APP_HTML.read_text(encoding="utf-8")
    assert "sum of ${withFigure} of ${totals.holdings_count} holdings" in html
    assert "the sum of the ${withFigure} of ${totals.holdings_count} holdings whose filing states a figure" in html
