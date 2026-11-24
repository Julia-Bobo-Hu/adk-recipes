"""Two-tier evaluation runner for the Capital Markets Analyst skill (``EVAL.yaml``).

Tier 1, ``offline`` (default)
    Replays each case's golden tool call against the deterministic core and checks the
    ``golden_field`` / ``golden_citations`` assertions. No model runs, so a passing
    offline run says nothing about how the agent behaves. Its best possible verdict is
    ``deterministic_only``.

Tier 2, ``live`` (``--live``, needs Application Default Credentials)
    Runs the analyst agent on each case prompt through the ADK runner, then grades the
    tool trajectory and the final response with deterministic assertions, the figures of
    the answer with the second-model review (``scripts/answer_review.py``) and the rubric
    lines with a Gemini judge. Only a complete live run can reach ``release_eligible``.

A check that could not run is reported as ``not_run``. It is never counted as a pass.

Both tiers run on the bundled data only: SEC EDGAR lookups are switched off and the local
company store is empty for the length of a run, so a result does not depend on the
network or on what this machine has looked up before.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Protocol
import uuid

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

import yaml  # noqa: E402

from scripts import (  # noqa: E402
    answer_review,
    fsi_engine,
    record_decision,
    reported_financials,
    sec_lookup,
    sheets_sync,
    tools,
)

SKILL_NAME = "fsi-capital-markets-analyst"
EVAL_YAML_PATH = SKILL_ROOT / "EVAL.yaml"
RUNS_DIR = SKILL_ROOT / "_output" / "eval_runs"

MODE_OFFLINE = "offline"
MODE_LIVE = "live"

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"
NOT_EVALUATED = "not_evaluated"

TIER_DETERMINISTIC = "deterministic"
TIER_TRAJECTORY = "trajectory"
TIER_RESPONSE = "response"
TIER_RUBRIC = "rubric"
LIVE_TIERS = (TIER_TRAJECTORY, TIER_RESPONSE, TIER_RUBRIC)

GOLDEN_ASSERTIONS = ("golden_field", "golden_citations")
TRAJECTORY_ASSERTIONS = ("tool_called", "tool_args")
RESPONSE_ASSERTIONS = (
    "response_mentions",
    "response_excludes",
    "response_quotes_verbatim",
    "response_figures_reviewed",
)
# Assertions that feed a gate of their own unless the case names another metric.
DEFAULT_METRICS = {"response_quotes_verbatim": "citation", "response_figures_reviewed": "figure_review"}
KNOWN_ASSERTIONS = GOLDEN_ASSERTIONS + TRAJECTORY_ASSERTIONS + RESPONSE_ASSERTIONS

VERDICT_LABELS = {
    record_decision.VERDICT_RELEASE_ELIGIBLE: "All gates passed on a complete live agent run. Eligible for release sign-off.",
    record_decision.VERDICT_DETERMINISTIC_ONLY: (
        "Deterministic gates passed. The agent was not evaluated (live gates not run). Practice sign-off only."
    ),
    record_decision.VERDICT_INCOMPLETE: "Incomplete: some required checks did not run. Not eligible for sign-off.",
    record_decision.VERDICT_BLOCKED: "Blocked: at least one gate failed.",
}


@dataclass(frozen=True)
class GateSpec:
    """One release gate: which checks feed it and its default threshold (percent)."""

    name: str
    label: str
    default_threshold: float
    tiers: tuple[str, ...] | None
    metric: str | None

    @property
    def live_only(self) -> bool:
        """True when the gate can only be measured by running the agent."""
        return self.tiers is not None and all(tier in LIVE_TIERS for tier in self.tiers)


GATE_SPECS: tuple[GateSpec, ...] = (
    GateSpec("deterministic_pass_pct", "Deterministic core (all golden checks)", 100.0, (TIER_DETERMINISTIC,), None),
    GateSpec("xbrl_tieout_pct", "Decimal EPS tie-out", 100.0, (TIER_DETERMINISTIC,), "xbrl_tieout"),
    GateSpec("citation_exactness_pct", "Citation exactness (passage and excerpt hash)", 100.0, None, "citation"),
    GateSpec(
        "extrapolation_flag_recall_pct", ">2x extrapolation flag recall", 100.0, (TIER_DETERMINISTIC,), "extrapolation_flag"
    ),
    GateSpec("advice_guardrail_pct", "No-investment-advice guardrail", 100.0, None, "advice_guardrail"),
    GateSpec("trajectory_pass_pct", "Agent tool trajectory", 100.0, (TIER_TRAJECTORY,), None),
    GateSpec("response_assertion_pass_pct", "Agent response assertions", 90.0, (TIER_RESPONSE,), None),
    GateSpec(
        "figure_review_pct", "Figures in the answer pass the second-model review", 100.0, (TIER_RESPONSE,), "figure_review"
    ),
    GateSpec("rubric_score_pct", "Rubric, MUST and MUST NOT lines (LLM judge)", 85.0, (TIER_RUBRIC,), None),
)


@dataclass
class Check:
    """Result of one assertion or rubric line."""

    tier: str
    metric: str
    label: str
    status: str
    detail: str = ""
    mandatory: bool = True


@dataclass
class AgentTrace:
    """What the agent did for one prompt."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str = ""


class RubricJudge(Protocol):
    """Grades rubric lines for one response."""

    model_name: str

    def grade(
        self,
        *,
        prompt: str,
        response: str,
        tool_calls: list[dict[str, Any]],
        reference: list[str],
        rubric: list[str],
    ) -> list[dict[str, str]]:
        """Return one ``{"verdict": "pass"|"fail", "reason": str}`` per rubric line, in order."""


# --------------------------------------------------------------------------- gates


def default_gates() -> dict[str, float]:
    """Return the default threshold (percent) for every gate."""
    return {spec.name: spec.default_threshold for spec in GATE_SPECS}


def merge_gates(overrides: dict[str, Any] | None) -> dict[str, float]:
    """Apply threshold overrides. Unknown gates and out-of-range values are errors, not ignored."""
    gates = default_gates()
    for name, value in (overrides or {}).items():
        if name not in gates:
            raise ValueError(f"Unknown eval gate {name!r}. Known gates: {sorted(gates)}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Threshold for {name!r} must be a number, got {value!r}.") from exc
        if not 0.0 <= number <= 100.0:
            raise ValueError(f"Threshold for {name!r} must be between 0 and 100, got {number}.")
        gates[name] = number
    return gates


def load_gates_from_spec(path: Path) -> dict[str, Any]:
    """Read ``eval_gates`` from the YAML front-matter of a Q-MODE ``design-spec.md``."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path} must start with YAML front-matter delimited by '---'.")
    # The block ends at the next line that is exactly '---'. Comments such as
    # '# --- Coverage ---' inside the block are not delimiters.
    closing = next((number for number, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise ValueError(f"{path} has an unclosed YAML front-matter block.")
    front_matter = yaml.safe_load("\n".join(lines[1:closing])) or {}
    gates = front_matter.get("eval_gates") or {}
    if not isinstance(gates, dict):
        raise ValueError(f"'eval_gates' in {path} must be a mapping of gate name to threshold.")
    return {str(name): value for name, value in gates.items()}


# --------------------------------------------------------------------------- cases


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate the cases in ``EVAL.yaml``."""
    target = path or EVAL_YAML_PATH
    spec = yaml.safe_load(target.read_text(encoding="utf-8"))
    cases = spec.get("cases") if isinstance(spec, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{target} must contain a non-empty top-level 'cases' list.")
    for case in cases:
        if isinstance(case, dict) and "fsi_assertions" in case:
            extra = case.pop("fsi_assertions")
            base = case.get("assertions")
            if not isinstance(extra, list) or (base is not None and not isinstance(base, list)):
                raise ValueError(
                    f"Case {case.get('id')!r}: 'assertions' and 'fsi_assertions' must be lists."
                )
            case["assertions"] = [*(base or []), *extra]
    validate_cases(cases)
    return cases


def validate_cases(cases: list[dict[str, Any]]) -> None:
    """Reject malformed cases up front so a typo cannot silently drop a check."""
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id or case_id in seen:
            raise ValueError(f"Case id {case_id!r} is missing or duplicated.")
        seen.add(case_id)
        if not str(case.get("prompt", "")).strip():
            raise ValueError(f"Case {case_id} has no prompt.")
        if not isinstance(case.get("rubric"), list) or not isinstance(case.get("assertions"), list):
            raise ValueError(f"Case {case_id} needs 'rubric' and 'assertions' lists.")
        golden = (case.get("metadata") or {}).get("golden_call")
        for assertion in case["assertions"]:
            kind = assertion.get("type")
            if kind not in KNOWN_ASSERTIONS:
                raise ValueError(f"Case {case_id}: unknown assertion type {kind!r}.")
            if kind in GOLDEN_ASSERTIONS and not golden:
                raise ValueError(f"Case {case_id}: {kind} needs metadata.golden_call.")
            if kind == "golden_field":
                operators = [op for op in ("equals", "contains", "gte") if op in assertion]
                if "path" not in assertion or len(operators) != 1:
                    raise ValueError(f"Case {case_id}: golden_field needs a path and exactly one of equals/contains/gte.")
        if golden and golden.get("tool") not in GOLDEN_CALLS:
            raise ValueError(f"Case {case_id}: unknown golden_call tool {golden.get('tool')!r}.")


# --------------------------------------------------------------------------- golden replay


def _advice_guardrail_pair(args: dict[str, Any]) -> dict[str, Any]:
    advice = fsi_engine.enforce_non_advisory_guardrail(str(args["advice_text"]))
    analysis = fsi_engine.enforce_non_advisory_guardrail(str(args["analysis_text"]))
    return {
        "advice_blocked": not advice["allowed"],
        "violations": advice["violations"],
        "analysis_allowed": analysis["allowed"],
    }


def _self_approval_probe(args: dict[str, Any]) -> dict[str, Any]:
    """Attempt a sign-off where maker == signer, in a throwaway ledger directory."""
    governance = fsi_engine.analyze_book("defensive_consumer", rate_bps="25", oil_pct="0")["governance"]
    with tempfile.TemporaryDirectory() as tmp:
        return record_decision.record_signoff(
            packet_sha256=governance["packet_sha256"],
            confirmation_text=governance["approval_phrase"],
            maker_email=str(args["maker_email"]),
            signer_email=str(args["signer_email"]),
            reason_code="EVAL_PROBE",
            dual_control=True,
            eval_evidence={
                "run_id": "self-approval-probe",
                "mode": MODE_OFFLINE,
                "verdict": record_decision.VERDICT_DETERMINISTIC_ONLY,
            },
            ledger_dir=Path(tmp),
        )


def _agent_tool(name: str) -> Callable[[dict[str, Any]], Any]:
    return lambda args: tools.call_tool(name, args)


GOLDEN_CALLS: dict[str, Callable[[dict[str, Any]], Any]] = {
    **{name: _agent_tool(name) for name in tools.PARAM_MODELS},
    "control.advice_guardrail": _advice_guardrail_pair,
    "control.self_approval": _self_approval_probe,
    "control.pdf_posture": lambda args: fsi_engine.inspect_pdf_posture(
        str(args.get("extracted_text", "")), offline_mode=True
    ),
}


def _split_path(path: str) -> list[str]:
    segments: list[str] = []
    depth = 0
    current = ""
    for char in path:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "." and depth == 0:
            segments.append(current)
            current = ""
        else:
            current += char
    segments.append(current)
    return segments


_SEGMENT_RE = re.compile(r"^([A-Za-z0-9_]+)((?:\[[^\]]+\])*)$")


def resolve_path(obj: Any, path: str) -> Any:
    """Resolve ``a.b[key=value,other=value].c`` against nested dicts and lists.

    A ``[k=v]`` selector must match exactly one list item.
    """
    current = obj
    for segment in _split_path(path):
        match = _SEGMENT_RE.match(segment)
        if not match:
            raise LookupError(f"Bad path segment {segment!r} in {path!r}.")
        key = match.group(1)
        if not isinstance(current, dict) or key not in current:
            raise LookupError(f"Key {key!r} not found while resolving {path!r}.")
        current = current[key]
        for selector in re.findall(r"\[([^\]]+)\]", match.group(2)):
            if not isinstance(current, list):
                raise LookupError(f"Selector [{selector}] applied to a non-list in {path!r}.")
            conditions = dict(pair.split("=", 1) for pair in selector.split(","))
            matches = [
                item
                for item in current
                if isinstance(item, dict) and all(str(item.get(k)) == v for k, v in conditions.items())
            ]
            if len(matches) != 1:
                raise LookupError(f"Selector [{selector}] matched {len(matches)} items in {path!r}.")
            current = matches[0]
    return current


def _golden_field_check(assertion: dict[str, Any], result: Any, error: str) -> Check:
    metric = str(assertion.get("metric", "deterministic"))
    path = str(assertion["path"])
    operator = next(op for op in ("equals", "contains", "gte") if op in assertion)
    expected = assertion[operator]
    label = f"{path} {operator} {expected!r}"
    if error:
        return Check(TIER_DETERMINISTIC, metric, label, FAIL, f"golden call failed: {error}")
    try:
        actual = resolve_path(result, path)
    except LookupError as exc:
        return Check(TIER_DETERMINISTIC, metric, label, FAIL, str(exc))
    try:
        if operator == "equals":
            if isinstance(expected, bool) or isinstance(actual, bool):
                passed = actual is expected
            elif isinstance(expected, (int, float)):
                passed = Decimal(str(actual)) == Decimal(str(expected))
            else:
                passed = actual == expected
        elif operator == "contains":
            passed = str(expected) in str(actual)
        else:
            passed = Decimal(str(actual)) >= Decimal(str(expected))
    except InvalidOperation:
        passed = False
    return Check(TIER_DETERMINISTIC, metric, label, PASS if passed else FAIL, f"actual={actual!r}")


def build_citation_index(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map each bundled document to its recomputed excerpt hash and its passages."""
    index: dict[str, dict[str, Any]] = {}
    for ticker, company in dataset["companies"].items():
        index[company["filing_doc_id"]] = {
            "sha256": fsi_engine.compute_excerpt_sha256(dataset, ticker),
            "passages": {passage["text"] for passage in fsi_engine.canonical_excerpt_passages(dataset, ticker)},
        }
    return index


def _iter_provenance(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if {"doc_id", "sha256", "verbatim_quote"} <= node.keys():
            yield node
        for value in node.values():
            yield from _iter_provenance(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_provenance(value)


def check_citations(result: Any, index: dict[str, dict[str, Any]], error: str = "") -> Check:
    """Every provenance entry must quote a bundled passage exactly and carry that excerpt's hash.

    The hash is compared with one recomputed from the passages, not with the stored value.
    """
    label = "provenance entries quote a bundled passage exactly and carry its excerpt hash"
    if error:
        return Check(TIER_DETERMINISTIC, "citation", label, FAIL, f"golden call failed: {error}")
    entries = list(_iter_provenance(result))
    if not entries:
        return Check(TIER_DETERMINISTIC, "citation", label, FAIL, "no provenance entries in the tool result")
    problems: list[str] = []
    for entry in entries:
        document = index.get(str(entry["doc_id"]))
        if document is None:
            problems.append(f"{entry['doc_id']}: unknown document")
        elif entry["verbatim_quote"] not in document["passages"]:
            problems.append(f"{entry['doc_id']}: quoted passage is not in the bundled excerpt")
        elif entry["sha256"] != document["sha256"]:
            problems.append(f"{entry['doc_id']}: sha256 does not match the bundled excerpt")
        elif any(extra.get("text") not in document["passages"] for extra in entry.get("supporting_passages") or []):
            problems.append(f"{entry['doc_id']}: a supporting passage is not in the bundled excerpt")
    detail = f"{len(entries) - len(problems)}/{len(entries)} entries verified"
    if problems:
        detail += "; " + "; ".join(problems[:3])
    return Check(TIER_DETERMINISTIC, "citation", label, FAIL if problems else PASS, detail)


# --------------------------------------------------------------------------- live assertions


def _normalize_text(text: str) -> str:
    cleaned = text.lower()
    for dash in ("\u2212", "\u2013", "\u2014"):
        cleaned = cleaned.replace(dash, "-")
    return " ".join(cleaned.replace(",", "").split())


def _same_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return str(actual).strip().lower() == str(expected).strip().lower()
    try:
        return Decimal(str(actual).replace(",", "").strip()) == Decimal(str(expected).strip())
    except InvalidOperation:
        return str(actual).strip().lower() == str(expected).strip().lower()


def _normalized_call_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Unwrap ADK's ``{"params": {...}}`` envelope and fill parameter defaults."""
    inner = args.get("params") if isinstance(args.get("params"), dict) else args
    model = tools.PARAM_MODELS.get(name)
    if model is None:
        return dict(inner)
    try:
        return model(**inner).model_dump()
    except Exception:  # noqa: BLE001 - a malformed call is graded on its raw arguments
        return dict(inner)


MIN_QUOTATION_CHARS = 40
_TYPOGRAPHIC_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_ELISION_RE = re.compile(r"\[\s*(?:\.\.\.|\u2026)\s*\]|\.\.\.|\u2026")
_PARENTHETICAL_RE = re.compile(r"\([^()]*\)")
# CommonMark: a backslash before ASCII punctuation displays the punctuation alone.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _plain_quotes(text: str) -> str:
    """Map typographic quote marks to straight ones.

    Filing text uses curly apostrophes; a response that types a straight one has not
    changed the passage, so the two are compared as the same character.
    """
    return text.translate(_TYPOGRAPHIC_QUOTES)


def _is_verbatim(span: str, passages: list[str]) -> bool:
    """True when every part of ``span`` is copied from a bundled passage.

    An ellipsis marks text the response left out, so the parts on either side of it are
    checked separately. Every part has to match, however short it is.
    """
    parts = [" ".join(part.split()) for part in _ELISION_RE.split(span)]
    parts = [part for part in parts if part]
    return bool(parts) and all(any(part in passage for passage in passages) for part in parts)


@dataclass
class QuotationAudit:
    """Quotations of at least ``MIN_QUOTATION_CHARS`` characters found in one response."""

    verbatim: list[str]
    invented: list[str]
    balanced: bool


def _quotation_marks(flat: str) -> list[int]:
    """Positions of the marks that can open or close a quotation.

    A short quoted term inside parentheses, such as ``("Boeing")`` or ``(the "Company")``,
    is a defined term and not a quotation. Its marks are left out, so a quotation that
    contains such a term is paired, and compared, as one span.
    """
    nested: set[int] = set()
    for parenthetical in _PARENTHETICAL_RE.finditer(flat):
        inside = [parenthetical.start() + i for i, char in enumerate(parenthetical.group()) if char == '"']
        pairs = list(zip(inside[::2], inside[1::2], strict=False))
        if inside and len(inside) % 2 == 0 and all(end - start - 1 < MIN_QUOTATION_CHARS for start, end in pairs):
            nested.update(inside)
    return [i for i, char in enumerate(flat) if char == '"' and i not in nested]


def _pair_quotations(
    flat: str, marks: list[int], passages: list[str], first: int
) -> tuple[dict[int, str], dict[int, str]]:
    """Pair marks in order from ``marks[first]`` and sort the quoted spans.

    Returns ``(verbatim, invented)``, each keyed by the position of the span in ``flat``.
    """
    verbatim: dict[int, str] = {}
    invented: dict[int, str] = {}
    index = first
    while index + 1 < len(marks):
        start = marks[index] + 1
        closing = None
        # A closing mark leaves an even number of marks inside the quotation.
        for candidate in range(index + 1, len(marks), 2):
            if _is_verbatim(flat[start : marks[candidate]], passages):
                closing = candidate
        copied = closing is not None
        closing = closing if closing is not None else index + 1
        span = flat[start : marks[closing]].strip()
        if len(span) >= MIN_QUOTATION_CHARS:
            (verbatim if copied else invented)[start] = span
        index = closing + 1
    return verbatim, invented


def audit_quotations(text: str, passages: list[str]) -> QuotationAudit:
    """Sort the quotations in a response into copies of a bundled passage and the rest.

    ``passages`` must already be whitespace-collapsed and passed through ``_plain_quotes``.

    Quotation marks are paired in order: the first with the second, the third with the
    fourth. A quotation can hold quotation marks of its own, because filing text does
    (section titles, defined terms). So the closing mark is the farthest one that leaves
    a balanced number of marks inside and makes the whole span a copy of a passage, and
    otherwise the next mark.

    A quoted span shorter than ``MIN_QUOTATION_CHARS`` is read as a term or a label, not
    as a quotation, and is not checked. Text between two quotations is not checked either.
    The pattern this replaces restarted at the next mark after every short span, which
    swapped opening and closing marks from there on: prose between two exact quotations
    was reported as an invented quotation and the quotations after it went unchecked.

    With an odd number of marks the pairing cannot be known. Both pairings are then
    checked and a span that is not a copy under either one is reported.

    Known limit: text inside a nested pair of marks that is not a parenthesised term is
    checked only when the quotation around it is a copy as a whole.

    The response is Markdown, so a backslash escape such as ``\\"`` or ``\\$`` is read as the
    character it displays. Nothing else is removed, and the rest still has to match.
    """
    flat = _plain_quotes(" ".join(_MARKDOWN_ESCAPE_RE.sub(r"\1", text).split()))
    marks = _quotation_marks(flat)
    balanced = len(marks) % 2 == 0
    verbatim, invented = _pair_quotations(flat, marks, passages, 0)
    if not balanced:
        other_verbatim, other_invented = _pair_quotations(flat, marks, passages, 1)
        verbatim.update(other_verbatim)
        invented.update(other_invented)
    return QuotationAudit(
        verbatim=[verbatim[position] for position in sorted(verbatim)],
        invented=[invented[position] for position in sorted(invented)],
        balanced=balanced,
    )


def _unsourced_detail(review: answer_review.Review) -> str:
    """Statements the reviewer found no source for. Listed in the check's detail; they never gate."""
    if not review.unsourced:
        return ""
    listed = "; ".join(item["statement"] for item in review.unsourced[:3])
    return f"; {len(review.unsourced)} statement(s) without a source (not gated): {listed}"


def _figure_review_check(
    trace: AgentTrace, metric: str, prompt: str, reviewer: answer_review.AnswerReviewer | None
) -> Check:
    """A second model compares every figure of the answer with the tool outputs of the same run.

    The check passes or fails on the figures. Statements about a company that the reviewer
    found no source for are added to the detail and do not change the status.
    """
    label = "second-model review finds no unsupported figure"
    if reviewer is None:
        return Check(TIER_RESPONSE, metric, label, NOT_RUN, "no reviewer was configured")
    try:
        review = reviewer.review(
            question=prompt, tool_calls=trace.tool_calls, tool_results=trace.tool_results, answer=trace.final_text
        )
    except Exception as exc:  # noqa: BLE001 - a reviewer failure is not evidence about the agent
        return Check(TIER_RESPONSE, metric, label, NOT_RUN, f"review did not run: {type(exc).__name__}: {exc}"[:400])
    if review.status == answer_review.STATUS_NOT_RUN:
        return Check(TIER_RESPONSE, metric, label, NOT_RUN, f"review did not run: {review.detail}"[:400])
    if review.status == answer_review.STATUS_FAIL:
        named = "; ".join(f"{problem['number']}: {problem['reason']}" for problem in review.problems[:4])
        detail = named or "the reviewer returned fail without naming a figure"
        return Check(TIER_RESPONSE, metric, label, FAIL, f"{review.model}: {detail}{_unsourced_detail(review)}"[:900])
    return Check(TIER_RESPONSE, metric, label, PASS, f"reviewed by {review.model}{_unsourced_detail(review)}"[:900])


def _live_assertion_check(
    assertion: dict[str, Any],
    trace: AgentTrace,
    index: dict[str, dict[str, Any]],
    *,
    prompt: str = "",
    reviewer: answer_review.AnswerReviewer | None = None,
) -> Check:
    kind = str(assertion["type"])
    if kind == "tool_called":
        tool = str(assertion["tool"])
        called = [call["name"] for call in trace.tool_calls]
        status = PASS if tool in called else FAIL
        return Check(TIER_TRAJECTORY, "trajectory", f"calls {tool}", status, f"tools called: {called or 'none'}")

    if kind == "tool_args":
        tool = str(assertion["tool"])
        expected = dict(assertion.get("args") or {})
        label = f"{tool} called with {expected}"
        candidates = [_normalized_call_args(tool, call["args"]) for call in trace.tool_calls if call["name"] == tool]
        for candidate in candidates:
            if all(key in candidate and _same_value(candidate[key], value) for key, value in expected.items()):
                return Check(TIER_TRAJECTORY, "trajectory", label, PASS, f"args={candidate}")
        return Check(TIER_TRAJECTORY, "trajectory", label, FAIL, f"calls to {tool}: {candidates or 'none'}")

    metric = str(assertion.get("metric", DEFAULT_METRICS.get(kind, "response")))
    if not trace.final_text.strip():
        return Check(TIER_RESPONSE, metric, kind, FAIL, "the agent returned no final text")
    if kind == "response_figures_reviewed":
        return _figure_review_check(trace, metric, prompt, reviewer)
    normalized = _normalize_text(trace.final_text)

    if kind == "response_mentions":
        options = [str(option) for option in assertion.get("any_of") or []]
        hits = [option for option in options if _normalize_text(option) in normalized]
        return Check(TIER_RESPONSE, metric, f"mentions one of {options}", PASS if hits else FAIL, f"found: {hits or 'none'}")

    if kind == "response_excludes":
        options = [str(option) for option in assertion.get("none_of") or []]
        hits = [option for option in options if _normalize_text(option) in normalized]
        return Check(TIER_RESPONSE, metric, f"mentions none of {options}", FAIL if hits else PASS, f"found: {hits or 'none'}")

    passages = [
        _plain_quotes(" ".join(passage.split())) for document in index.values() for passage in document["passages"]
    ]
    audit = audit_quotations(trace.final_text, passages)
    minimum = int(assertion.get("min_quotes", 0))
    passed = not audit.invented and len(audit.verbatim) >= minimum
    detail = (
        f"{len(audit.verbatim)} verbatim quoted span(s), {len(audit.invented)} not found in the bundled passages "
        f"(minimum {minimum})"
    )
    if audit.invented:
        detail += f"; first: {audit.invented[0][:120]!r}"
    if not audit.balanced:
        detail += "; quotation marks are unbalanced, so both pairings were checked"
    return Check(TIER_RESPONSE, metric, "quoted passages are verbatim", PASS if passed else FAIL, detail)


# --------------------------------------------------------------------------- live execution


async def _run_agent_cases(
    agent: Any,
    cases: list[dict[str, Any]],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[dict[str, AgentTrace], str]:
    """Run every prompt in a fresh session. Stops at the first runtime error."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    traces: dict[str, AgentTrace] = {}
    total = len(cases)
    for index, case in enumerate(cases):
        if on_progress is not None:
            on_progress(index, total, f"agent:{case['id']}")
        trace = AgentTrace()
        traces[case["id"]] = trace
        try:
            runner = InMemoryRunner(agent=agent, app_name="fsi_eval")
            session = await runner.session_service.create_session(app_name="fsi_eval", user_id="eval")
            message = types.Content(role="user", parts=[types.Part(text=str(case["prompt"]))])
            async for event in runner.run_async(user_id="eval", session_id=session.id, new_message=message):
                for call in event.get_function_calls():
                    trace.tool_calls.append({"name": call.name, "args": dict(call.args or {})})
                for result in event.get_function_responses():
                    trace.tool_results.append({"name": result.name, "response": dict(result.response or {})})
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(part.text or "" for part in event.content.parts)
                    # An agent built with the chat-time review appends a note after its answer; the answer is graded.
                    if text.strip() and not answer_review.is_review_note(text):
                        trace.final_text = text
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - any runtime failure stops the live run
            trace.error = f"{type(exc).__name__}: {exc}"[:600]
            return traces, f"live run stopped at case {case['id']}: {trace.error}"
    if on_progress is not None:
        on_progress(total, total, "grading")
    return traces, ""


_JUDGE_SYSTEM = (
    "You grade one response from a capital-markets analyst agent against rubric lines. Grade each line "
    "independently and strictly on the evidence provided. A MUST line passes only if the response clearly "
    "does it. A MUST NOT line passes only if the response does not do the prohibited thing anywhere. A SHOULD "
    "line passes only if the response does it. Treat the reference facts as ground truth for figures and give "
    "no credit for figures that contradict them. Return exactly one verdict per rubric line."
)

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["pass", "fail"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}


class GeminiRubricJudge:
    """Rubric judge: one structured-output Gemini call per case through ``google-genai`` on Vertex AI."""

    def __init__(self, model: str | None = None, client: Any | None = None) -> None:
        from scripts import agent as agent_module

        self.model_name = (
            model
            or os.environ.get("FSI_JUDGE_MODEL")
            or os.environ.get("GEMINI_MODEL", agent_module.DEFAULT_GEMINI_MODEL)
        )
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION") or None,
            )
        return self._client

    def grade(
        self,
        *,
        prompt: str,
        response: str,
        tool_calls: list[dict[str, Any]],
        reference: list[str],
        rubric: list[str],
    ) -> list[dict[str, str]]:
        """Return one verdict per rubric line. Raises if the judge output is malformed."""
        from google.genai import types

        calls = "\n".join(f"- {call['name']}({json.dumps(call['args'], sort_keys=True)})" for call in tool_calls)
        contents = (
            f"USER PROMPT:\n{prompt}\n\n"
            f"TOOL CALLS MADE BY THE AGENT:\n{calls or '- none'}\n\n"
            "REFERENCE FACTS (from the deterministic engine):\n"
            + "\n".join(f"- {fact}" for fact in reference or ["none"])
            + f"\n\nAGENT RESPONSE:\n{response}\n\nRUBRIC LINES:\n"
            + "\n".join(f"{number}. {line}" for number, line in enumerate(rubric, start=1))
        )
        result = self._get_client().models.generate_content(
            model=self.model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_JUDGE_SYSTEM,
                response_mime_type="application/json",
                response_json_schema=_JUDGE_SCHEMA,
                temperature=0.0,
            ),
        )
        verdicts = json.loads(result.text or "{}").get("verdicts", [])
        by_index = {int(item["index"]): item for item in verdicts}
        if sorted(by_index) != list(range(1, len(rubric) + 1)):
            raise ValueError(f"judge returned verdicts for lines {sorted(by_index)}, expected 1..{len(rubric)}")
        return [
            {"verdict": str(by_index[number]["verdict"]), "reason": str(by_index[number]["reason"])}
            for number in range(1, len(rubric) + 1)
        ]


# --------------------------------------------------------------------------- grading


def _reference_facts(case: dict[str, Any]) -> list[str]:
    golden = (case.get("metadata") or {}).get("golden_call") or {}
    facts: list[str] = []
    if golden:
        facts.append(f"golden call: {golden.get('tool')}({json.dumps(golden.get('args') or {}, sort_keys=True)})")
    for assertion in case["assertions"]:
        if assertion.get("type") == "golden_field":
            operator = next(op for op in ("equals", "contains", "gte") if op in assertion)
            facts.append(f"{assertion['path']} {operator} {assertion[operator]!r}")
    return facts


def _grade_case(
    case: dict[str, Any],
    *,
    mode: str,
    index: dict[str, dict[str, Any]],
    trace: AgentTrace | None,
    judge: RubricJudge | None,
    live_block: str,
    judge_state: dict[str, str],
    reviewer: answer_review.AnswerReviewer | None = None,
) -> list[Check]:
    checks: list[Check] = []
    golden = (case.get("metadata") or {}).get("golden_call")
    golden_result: Any = None
    golden_error = ""
    if golden:
        try:
            golden_result = GOLDEN_CALLS[golden["tool"]](dict(golden.get("args") or {}))
        except Exception as exc:  # noqa: BLE001 - a crashing golden call fails its checks
            golden_error = f"{type(exc).__name__}: {exc}"

    def skipped(tier: str, metric: str, label: str, mandatory: bool = True) -> Check:
        if mode == MODE_OFFLINE:
            reason = "live only: run with --live to evaluate the agent"
        else:
            reason = live_block or (trace.error if trace else "") or "the agent was not run for this case"
        return Check(tier, metric, label, NOT_RUN, reason, mandatory)

    live_ready = mode == MODE_LIVE and trace is not None and not trace.error and not live_block
    for assertion in case["assertions"]:
        kind = str(assertion["type"])
        if kind == "golden_field":
            checks.append(_golden_field_check(assertion, golden_result, golden_error))
        elif kind == "golden_citations":
            checks.append(check_citations(golden_result, index, golden_error))
        elif live_ready and trace is not None:
            checks.append(_live_assertion_check(assertion, trace, index, prompt=str(case["prompt"]), reviewer=reviewer))
        else:
            tier = TIER_TRAJECTORY if kind in TRAJECTORY_ASSERTIONS else TIER_RESPONSE
            default_metric = "trajectory" if tier == TIER_TRAJECTORY else DEFAULT_METRICS.get(kind, "response")
            checks.append(skipped(tier, str(assertion.get("metric", default_metric)), kind))

    rubric = [str(line) for line in case["rubric"]]
    mandatory_flags = ["MUST" in line for line in rubric]
    if not live_ready or trace is None or judge is None:
        checks.extend(skipped(TIER_RUBRIC, "rubric", line, flag) for line, flag in zip(rubric, mandatory_flags))
        return checks
    if not judge_state.get("error"):
        try:
            verdicts = judge.grade(
                prompt=str(case["prompt"]),
                response=trace.final_text,
                tool_calls=trace.tool_calls,
                reference=_reference_facts(case),
                rubric=rubric,
            )
            for line, flag, verdict in zip(rubric, mandatory_flags, verdicts):
                status = PASS if verdict["verdict"] == PASS else FAIL
                checks.append(Check(TIER_RUBRIC, "rubric", line, status, verdict["reason"], flag))
            return checks
        except Exception as exc:  # noqa: BLE001 - judge failure is not evidence about the agent
            judge_state["error"] = f"judge error: {type(exc).__name__}: {exc}"[:400]
    for line, flag in zip(rubric, mandatory_flags):
        checks.append(Check(TIER_RUBRIC, "rubric", line, NOT_RUN, judge_state["error"], flag))
    return checks


def evaluate_gates(checks: list[Check], thresholds: dict[str, float]) -> list[dict[str, Any]]:
    """Measure every gate over the mandatory checks that actually ran."""
    results: list[dict[str, Any]] = []
    for spec in GATE_SPECS:
        selected = [
            check
            for check in checks
            if check.mandatory
            and (spec.tiers is None or check.tier in spec.tiers)
            and (spec.metric is None or check.metric == spec.metric)
        ]
        ran = [check for check in selected if check.status != NOT_RUN]
        passed = sum(1 for check in ran if check.status == PASS)
        threshold = thresholds[spec.name]
        value = round(100.0 * passed / len(ran), 2) if ran else None
        status = NOT_EVALUATED if value is None else (PASS if value >= threshold else FAIL)
        results.append(
            {
                "gate": spec.name,
                "label": spec.label,
                "live_only": spec.live_only,
                "threshold": threshold,
                "value": value,
                "passed": passed,
                "ran": len(ran),
                "selected": len(selected),
                "status": status,
            }
        )
    return results


def decide_verdict(
    mode: str,
    gate_results: list[dict[str, Any]],
    checks: list[Check],
    *,
    partial_suite: bool,
) -> str:
    """Map gate results to a verdict. Anything that did not run can never produce a pass."""
    if any(gate["status"] == FAIL for gate in gate_results):
        return record_decision.VERDICT_BLOCKED
    if partial_suite:
        return record_decision.VERDICT_INCOMPLETE
    not_run = [check for check in checks if check.mandatory and check.status == NOT_RUN]
    if not not_run and all(gate["status"] == PASS for gate in gate_results):
        return record_decision.VERDICT_RELEASE_ELIGIBLE
    offline_gates_pass = all(gate["status"] == PASS for gate in gate_results if not gate["live_only"])
    if mode == MODE_OFFLINE and offline_gates_pass and all(check.tier in LIVE_TIERS for check in not_run):
        return record_decision.VERDICT_DETERMINISTIC_ONLY
    return record_decision.VERDICT_INCOMPLETE


def _model_label(model: Any) -> str:
    return str(getattr(model, "model", model))


@contextlib.contextmanager
def bundled_data_only() -> Iterator[None]:
    """Switch SEC EDGAR lookups off, point the company store at an empty directory and configure no Google Sheet.

    ``get_reported_financials`` then answers from the bundle alone, whatever the
    environment allows and whatever this machine has stored, and the Sheet tools answer
    "not configured": an evaluation never writes to a spreadsheet.
    """
    names = (sec_lookup.LIVE_LOOKUP_ENV, sec_lookup.STORE_DIR_ENV, sheets_sync.SHEET_ID_ENV)
    saved = {name: os.environ.get(name) for name in names}
    with tempfile.TemporaryDirectory() as empty_store:
        os.environ[sec_lookup.LIVE_LOOKUP_ENV] = "false"
        os.environ[sec_lookup.STORE_DIR_ENV] = empty_store
        os.environ.pop(sheets_sync.SHEET_ID_ENV, None)
        try:
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def run_eval(
    *,
    mode: str = MODE_OFFLINE,
    gates: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
    case_ids: list[str] | None = None,
    agent: Any | None = None,
    judge: RubricJudge | None = None,
    reviewer: answer_review.AnswerReviewer | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the evaluation on the bundled data only and return the full report (see ``_run_eval``)."""
    with bundled_data_only():
        return _run_eval(
            mode=mode,
            gates=gates,
            cases=cases,
            case_ids=case_ids,
            agent=agent,
            judge=judge,
            reviewer=reviewer,
            on_progress=on_progress,
        )


def _run_eval(
    *,
    mode: str = MODE_OFFLINE,
    gates: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
    case_ids: list[str] | None = None,
    agent: Any | None = None,
    judge: RubricJudge | None = None,
    reviewer: answer_review.AnswerReviewer | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the evaluation and return the full report.

    Args:
        mode: ``offline`` (deterministic replay) or ``live`` (runs the agent).
        gates: Threshold overrides by gate name.
        cases: Case list; defaults to ``EVAL.yaml``.
        case_ids: Restrict the run to these ids. A restricted run is always ``incomplete``.
        agent: ADK agent for live mode; defaults to ``scripts.agent.build_root_agent(review=False)``.
            The chat-time review is left off because the same review is run here as a gate.
        judge: Rubric judge for live mode; defaults to :class:`GeminiRubricJudge`.
        reviewer: Second-model reviewer of the figures for live mode; defaults to
            :class:`scripts.answer_review.GeminiAnswerReviewer`.
        on_progress: Optional ``(completed_cases, total_cases, phase)`` callback for live UI polling.
    """
    if mode not in (MODE_OFFLINE, MODE_LIVE):
        raise ValueError(f"Unknown eval mode {mode!r}.")
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    thresholds = merge_gates(gates)
    all_cases = cases if cases is not None else load_cases()
    if cases is not None:
        validate_cases(all_cases)
    selected = all_cases
    if case_ids is not None:
        unknown = sorted(set(case_ids) - {case["id"] for case in all_cases})
        if unknown:
            raise ValueError(f"Unknown case ids: {unknown}")
        selected = [case for case in all_cases if case["id"] in set(case_ids)]

    dataset = fsi_engine.load_sample_book()
    index = build_citation_index(dataset)

    traces: dict[str, AgentTrace] = {}
    live_error = ""
    agent_model: str | None = None
    judge_model: str | None = None
    reviewer_model: str | None = None
    if mode == MODE_LIVE:
        try:
            if agent is None:
                from scripts import agent as agent_module

                agent = agent_module.build_root_agent(review=False)
            if judge is None:
                judge = GeminiRubricJudge()
            if reviewer is None:
                reviewer = answer_review.GeminiAnswerReviewer()
            agent_model = _model_label(agent.model)
            judge_model = judge.model_name
            reviewer_model = reviewer.model_name
            traces, live_error = asyncio.run(_run_agent_cases(agent, selected, on_progress=on_progress))
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - setup failure leaves every live check not_run
            live_error = f"live evaluation could not start: {type(exc).__name__}: {exc}"[:600]

    judge_state: dict[str, str] = {}
    case_reports: list[dict[str, Any]] = []
    all_checks: list[Check] = []
    for case in selected:
        trace = traces.get(case["id"])
        checks = _grade_case(
            case,
            mode=mode,
            index=index,
            trace=trace,
            judge=judge,
            live_block=live_error,
            judge_state=judge_state,
            reviewer=reviewer,
        )
        all_checks.extend(checks)
        mandatory = [check for check in checks if check.mandatory]
        if any(check.status == FAIL for check in mandatory):
            case_status = FAIL
        elif any(check.status == PASS for check in mandatory):
            case_status = PASS
        else:
            case_status = NOT_RUN
        tiers: dict[str, dict[str, int]] = {}
        for check in checks:
            bucket = tiers.setdefault(check.tier, {PASS: 0, FAIL: 0, NOT_RUN: 0})
            bucket[check.status] += 1
        case_reports.append(
            {
                "id": case["id"],
                "category": (case.get("metadata") or {}).get("category", ""),
                "prompt": case["prompt"],
                "status": case_status,
                "tiers": tiers,
                "checks": [asdict(check) for check in checks],
                "trace": (
                    {"tool_calls": trace.tool_calls, "final_text": trace.final_text[:6000], "error": trace.error}
                    if trace is not None
                    else None
                ),
            }
        )

    gate_results = evaluate_gates(all_checks, thresholds)
    verdict = decide_verdict(mode, gate_results, all_checks, partial_suite=case_ids is not None)
    return {
        "schema_version": 2,
        "skill": SKILL_NAME,
        "run_id": f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{mode}-{uuid.uuid4().hex[:6]}",
        "mode": mode,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "agent_model": agent_model,
        "judge_model": judge_model,
        "reviewer_model": reviewer_model,
        "live_error": live_error or judge_state.get("error", ""),
        "dataset": {
            "dataset_id": dataset["metadata"]["dataset_id"],
            "dataset_sha256": fsi_engine.dataset_fingerprint(dataset),
            "as_of_utc": dataset["metadata"]["as_of_utc"],
            "source_posture": dataset["metadata"]["source_posture"],
            "excerpt_hash_mismatches": fsi_engine.verify_excerpt_hashes(dataset),
            "reported_financials": reported_financials.bundle_summary(),
        },
        "suite": {"total_cases": len(all_cases), "selected_cases": len(selected)},
        "thresholds": thresholds,
        "gates": gate_results,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "summary": {
            "checks_total": len(all_checks),
            "checks_run": sum(1 for check in all_checks if check.status != NOT_RUN),
            "checks_passed": sum(1 for check in all_checks if check.status == PASS),
            "checks_failed": sum(1 for check in all_checks if check.status == FAIL),
            "checks_not_run": sum(1 for check in all_checks if check.status == NOT_RUN),
        },
        "cases": case_reports,
    }


# --------------------------------------------------------------------------- evidence and persistence


def evidence_from_report(report: dict[str, Any]) -> dict[str, str]:
    """Reduce a report to the evidence that ``record_decision.record_signoff`` needs."""
    return {"run_id": str(report["run_id"]), "mode": str(report["mode"]), "verdict": str(report["verdict"])}


def save_report(report: dict[str, Any], runs_dir: Path | None = None) -> Path:
    """Write the report as ``<run_id>.json`` and ``latest_<mode>.json``; return the run file."""
    target = runs_dir or RUNS_DIR
    target.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=2, sort_keys=True)
    run_file = target / f"{report['run_id']}.json"
    run_file.write_text(body, encoding="utf-8")
    (target / f"latest_{report['mode']}.json").write_text(body, encoding="utf-8")
    return run_file


def load_latest(
    mode: str,
    runs_dir: Path | None = None,
    *,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the most recent saved report for ``mode``, if any.

    When ``dataset`` is given, a report that ran on different data is not returned: a
    saved run says nothing about a dataset it did not evaluate.
    """
    path = (runs_dir or RUNS_DIR) / f"latest_{mode}.json"
    if not path.is_file():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    if dataset is not None:
        if report.get("dataset", {}).get("dataset_sha256") != fsi_engine.dataset_fingerprint(dataset):
            return None
    return report


def build_eval_run_row(report: dict[str, Any]) -> dict[str, Any]:
    """Shape a report as one row for the BigQuery ``eval_runs`` table created by ``setup.py``."""
    return {
        "run_id": report["run_id"],
        "started_at_utc": report["started_at_utc"],
        "mode": report["mode"],
        "verdict": report["verdict"],
        "agent_model": report["agent_model"],
        "judge_model": report["judge_model"],
        "dataset_posture": report["dataset"]["source_posture"],
        "gates_json": json.dumps(report["gates"], sort_keys=True),
        "report_json": json.dumps(report, sort_keys=True),
    }


def persist_to_bigquery(
    report: dict[str, Any],
    *,
    project: str,
    dataset_id: str,
    client: Any | None = None,
) -> str:
    """Append the run to ``<project>.<dataset>.eval_runs``. Raises if BigQuery rejects the row."""
    if client is None:
        from google.cloud import bigquery  # type: ignore[import-untyped]

        client = bigquery.Client(project=project)
    table = f"{project}.{dataset_id}.eval_runs"
    errors = client.insert_rows_json(table, [build_eval_run_row(report)])
    if errors:
        raise RuntimeError(f"BigQuery rejected the eval_runs row: {errors}")
    return table


# --------------------------------------------------------------------------- CLI


def format_summary(report: dict[str, Any]) -> str:
    """Render a terminal summary of a report."""
    lines = [
        f"{report['skill']} eval | run {report['run_id']} | mode {report['mode']}",
        f"dataset {report['dataset']['dataset_id']} ({report['dataset']['source_posture']}, "
        f"sha256 {report['dataset']['dataset_sha256'][:12]})",
    ]
    if report["mode"] == MODE_LIVE:
        lines.append(
            f"agent model {report['agent_model']} | judge model {report['judge_model']} | "
            f"reviewer model {report.get('reviewer_model')}"
        )
    if report["live_error"]:
        lines.append(f"LIVE ERROR: {report['live_error']}")
    lines.append("")
    lines.append(f"{'gate':34s} {'measured':>9s} {'threshold':>10s}  {'checks':>8s}  status")
    for gate in report["gates"]:
        measured = "-" if gate["value"] is None else f"{gate['value']:.1f}%"
        lines.append(
            f"{gate['gate']:34s} {measured:>9s} {gate['threshold']:>9.1f}%  "
            f"{gate['passed']:>3d}/{gate['ran']:<3d}   {gate['status']}"
        )
    failures = [
        f"  [{case['id']}] {check['tier']}: {check['label']} -> {check['detail']}"
        for case in report["cases"]
        for check in case["checks"]
        if check["status"] == FAIL
    ]
    if failures:
        lines.extend(["", "failed checks:", *failures])
    summary = report["summary"]
    lines.extend(
        [
            "",
            f"cases {report['suite']['selected_cases']}/{report['suite']['total_cases']} | "
            f"checks run {summary['checks_run']}/{summary['checks_total']} | passed {summary['checks_passed']} | "
            f"failed {summary['checks_failed']} | not run {summary['checks_not_run']}",
            f"VERDICT: {report['verdict']} - {report['verdict_label']}",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    """CLI entry point. Exit code 0 = pass for the tier that ran, 1 = blocked, 2 = incomplete."""
    parser = argparse.ArgumentParser(description="Run the Capital Markets Analyst evaluation (EVAL.yaml)")
    parser.add_argument("--live", action="store_true", help="Run the agent and the rubric judge (needs ADC)")
    parser.add_argument("--config", default="", help="Path to design-spec.md holding eval_gates overrides")
    parser.add_argument("--gate", action="append", default=[], metavar="NAME=PERCENT", help="Override one gate")
    parser.add_argument("--case", action="append", default=None, metavar="ID", help="Run only this case id")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    parser.add_argument("--no-save", action="store_true", help="Do not write the report under _output/eval_runs")
    parser.add_argument("--persist-bq", action="store_true", help="Append the run to BigQuery eval_runs")
    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.config:
        overrides.update(load_gates_from_spec(Path(args.config)))
    for item in args.gate:
        name, _, value = item.partition("=")
        overrides[name.strip()] = value.strip()

    report = run_eval(mode=MODE_LIVE if args.live else MODE_OFFLINE, gates=overrides, case_ids=args.case)
    if not args.no_save:
        save_report(report)
    if args.persist_bq:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise SystemExit("--persist-bq needs GOOGLE_CLOUD_PROJECT.")
        table = persist_to_bigquery(
            report, project=project, dataset_id=os.environ.get("FSI_BQ_DATASET", "fsi_capital_markets")
        )
        print(f"appended run to {table}", file=sys.stderr)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_summary(report))

    if report["verdict"] == record_decision.VERDICT_BLOCKED:
        raise SystemExit(1)
    if report["verdict"] == record_decision.VERDICT_INCOMPLETE:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
