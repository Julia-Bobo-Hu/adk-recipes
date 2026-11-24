"""Period-over-period filing delta monitor (the-watcher), with optional credit rubric scores."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from scripts import fsi_engine

RUBRIC_DIMENSIONS = ("leverage_trajectory", "liquidity_headroom", "margin_resilience", "covenant_risk")
RUBRIC_NOT_SCORED_NOTE = (
    "Not scored. Rubric scores are analyst judgments, not filing facts, so a filing-sourced "
    "dataset does not carry them."
)


def _credit_scores(rubric: dict[str, Any] | None) -> dict[str, Any] | None:
    """Composite of the four rubric scores, or None when the dataset carries no rubric."""
    if not rubric:
        return None
    composite = sum((Decimal(str(rubric[name])) for name in RUBRIC_DIMENSIONS), Decimal("0")) / Decimal("4")
    scores: dict[str, Any] = {name: rubric[name] for name in RUBRIC_DIMENSIONS}
    scores["composite_score"] = str(composite.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    scores["overall_stance"] = rubric["overall_stance"]
    return scores


def evaluate_book_deltas(
    book_id: str = "next27_flagship",
    *,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build period-over-period records for a book, and the the-watcher.md brief.

    Each record compares one metric between two periods. ``basis`` says where the two
    values come from: ``filing_passage`` (one quoted passage states both periods) or
    ``xbrl`` (two balance-sheet dates reported in the current filing). Credit rubric
    scores are returned only when the dataset carries them; otherwise ``credit_scores``
    is None and ``rubric_status`` is ``not_scored``.
    """
    data = dataset or fsi_engine.load_sample_book()
    if book_id not in data["books"]:
        raise ValueError(f"Unknown book_id '{book_id}'")

    posture = data["metadata"]["source_posture"]
    tickers = data["books"][book_id]["tickers"]
    rows: list[dict[str, Any]] = []

    for ticker in tickers:
        comp = data["companies"][ticker]
        delta = comp["yoy_delta"]
        rubric = delta.get("credit_rubric")
        credit_scores = _credit_scores(rubric)
        evidence_quote = delta.get("evidence_quote") or (rubric or {}).get("evidence_quote")

        row: dict[str, Any] = {
            "ticker": ticker,
            "name": comp["name"],
            "sector": comp["sector"],
            "prior_doc_id": delta.get("prior_doc_id"),
            "current_doc_id": comp["filing_doc_id"],
            "excerpt_sha256": comp["excerpt_sha256"],
            "metric": delta["metric"],
            "basis": delta.get("basis", ""),
            "prior_period_end": delta.get("prior_period_end"),
            "current_period_end": delta.get("current_period_end"),
            "prior_value": delta["prior_value"],
            "current_value": delta["current_value"],
            "change_summary": delta["change_summary"],
            "value_source": delta.get("value_source", ""),
            "credit_scores": credit_scores,
            "rubric_status": "scored" if credit_scores else "not_scored",
            "evidence_quote": evidence_quote,
            "source_posture": posture,
        }
        if evidence_quote:
            row["provenance"] = {
                "doc_id": comp["filing_doc_id"],
                "sha256": comp["excerpt_sha256"],
                "filing_section": delta.get("evidence_section", ""),
                "verbatim_quote": evidence_quote,
                "source_posture": posture,
            }
        rows.append(row)

    watcher_md = render_watcher_markdown(
        data["books"][book_id]["label"],
        rows,
        data["metadata"]["as_of_utc"],
        posture,
    )
    return {
        "book_id": book_id,
        "book_label": data["books"][book_id]["label"],
        "rows": rows,
        "watcher_markdown": watcher_md,
    }


def _cell(text: Any) -> str:
    """Make a value safe inside a markdown table cell."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_watcher_markdown(
    book_label: str,
    rows: list[dict[str, Any]],
    as_of_utc: str,
    source_posture: str,
) -> str:
    """Render `the-watcher.md` period-over-period filing delta report."""
    scored = any(r["credit_scores"] for r in rows)
    lines = [
        "# `the-watcher` — Period-over-Period Filing Delta",
        f"**Book:** {book_label} · **As-Of:** `{as_of_utc}` · **Source Posture:** `{source_posture}`",
        "",
    ]
    if scored:
        lines += [
            "| Ticker | Metric (`Prior -> Current`) | Composite Credit (`1-5`) | Stance | Evidence |",
            "| :--- | :--- | :---: | :--- | :--- |",
        ]
    else:
        lines += [
            "| Ticker | Metric | Prior | Current | Change | Evidence |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    for r in rows:
        cs = r["credit_scores"]
        evidence = f'*"{_cell(r["evidence_quote"])}"*' if r["evidence_quote"] else _cell(r["value_source"])
        subject = f"**{r['ticker']}** (`{r['current_doc_id']}`)"
        change = f"**{_cell(r['metric'])}**: {_cell(r['prior_value'])} → **{_cell(r['current_value'])}**"
        if scored and cs:
            lines.append(f"| {subject} | {change} | `{cs['composite_score']}/5.00` | {_cell(cs['overall_stance'])} | {evidence} |")
        elif scored:
            lines.append(f"| {subject} | {change} | not scored | not scored | {evidence} |")
        else:
            lines.append(
                f"| {subject} | {_cell(r['metric'])} | {_cell(r['prior_value'])} ({_cell(r['prior_period_end'])}) | "
                f"**{_cell(r['current_value'])}** ({_cell(r['current_period_end'])}) | {_cell(r['change_summary'])} | {evidence} |"
            )
    lines.append("")
    if not scored:
        lines.append(f"> **Credit rubric:** {RUBRIC_NOT_SCORED_NOTE}")
    lines.append(
        "> **Compliance Notice:** This brief reports disclosed figures only. It is not investment advice or a credit rating."
    )
    return "\n".join(lines)
