"""Tests for the two-tier evaluation runner.

The live tier is exercised through the real ADK runner and the real tools with a
scripted model in place of Gemini (``ScriptedLlm``) and a stub rubric judge. That
proves the plumbing: trace capture, trajectory and response assertions, gates and
verdicts. It says nothing about how a real model behaves; only ``eval_runner.py
--live`` against Vertex AI measures that.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import copy
import json
from pathlib import Path
import sys
from typing import Any

from google.adk.models import BaseLlm, LlmResponse
from google.adk.tools import FunctionTool
from google.genai import types
import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import (  # noqa: E402
    agent,
    answer_review,
    eval_runner,
    fsi_engine,
    record_decision,
    reported_financials,
    server,
    setup,
    tools,
)

BLOCKED = record_decision.VERDICT_BLOCKED
INCOMPLETE = record_decision.VERDICT_INCOMPLETE
DETERMINISTIC_ONLY = record_decision.VERDICT_DETERMINISTIC_ONLY
RELEASE_ELIGIBLE = record_decision.VERDICT_RELEASE_ELIGIBLE

DETERMINISTIC_GATES = (
    "deterministic_pass_pct",
    "xbrl_tieout_pct",
    "citation_exactness_pct",
    "extrapolation_flag_recall_pct",
    "advice_guardrail_pct",
)
LIVE_GATES = ("trajectory_pass_pct", "response_assertion_pass_pct", "figure_review_pct", "rubric_score_pct")


@pytest.fixture(autouse=True)
def _isolate_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep workbook exports and ledgers out of the source tree."""
    monkeypatch.setattr(fsi_engine, "SKILL_ROOT", tmp_path)
    monkeypatch.setattr(record_decision, "DEFAULT_LEDGER_DIR", tmp_path / "_signoff")
    monkeypatch.setattr(server, "_LATEST_EVAL", None)
    monkeypatch.delenv("FSI_DUAL_CONTROL", raising=False)
    monkeypatch.delenv("FSI_REQUIRE_LIVE_EVAL", raising=False)

    def no_model_call(self: Any) -> Any:
        raise AssertionError("the second-model reviewer must not call a model in these tests")

    monkeypatch.setattr(answer_review.GeminiAnswerReviewer, "_get_client", no_model_call)


class ScriptedLlm(BaseLlm):
    """Stand-in for Gemini: replays one tool call and one final answer per prompt."""

    script: dict[str, dict[str, Any]] = {}

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        user_text = ""
        tool_result_seen = False
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.function_response is not None:
                    tool_result_seen = True
                if part.text and content.role == "user":
                    user_text += part.text
        step = next(value for prompt, value in self.script.items() if prompt in user_text)
        if step.get("tool") and not tool_result_seen:
            part = types.Part(
                function_call=types.FunctionCall(name=step["tool"], args={"params": step["args"]})
            )
        else:
            part = types.Part(text=step["final"])
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class StubJudge:
    """Rubric judge double returning a fixed verdict, or raising."""

    model_name = "stub-judge"

    def __init__(self, verdict: str = "pass", error: Exception | None = None) -> None:
        self.verdict = verdict
        self.error = error
        self.calls = 0

    def grade(self, **kwargs: Any) -> list[dict[str, str]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [{"verdict": self.verdict, "reason": "stub"} for _ in kwargs["rubric"]]


class StubReviewer:
    """Second-model reviewer double: passes, flags every answer that contains a text, or does not run."""

    model_name = "stub-reviewer"

    def __init__(self, flag_text: str | None = None, unavailable: bool = False) -> None:
        self.flag_text = flag_text
        self.unavailable = unavailable
        self.unsourced: tuple[dict[str, str], ...] = ()
        self.seen: list[dict[str, Any]] = []

    def review(self, **turn: Any) -> answer_review.Review:
        self.seen.append(turn)
        if self.unavailable:
            return answer_review.Review(answer_review.STATUS_NOT_RUN, self.model_name, detail="reviewer unavailable")
        if self.flag_text and self.flag_text in turn["answer"]:
            problem = {"number": self.flag_text, "reason": "not in the tool output"}
            return answer_review.Review(answer_review.STATUS_FAIL, self.model_name, (problem,))
        return answer_review.Review(answer_review.STATUS_PASS, self.model_name, unsourced=self.unsourced)


def _gates(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {gate["gate"]: gate for gate in report["gates"]}


def _compliant_script(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build the answers of an agent that does exactly what EVAL.yaml asks."""
    passage = fsi_engine.load_sample_book()["companies"]["DAL"]["sensitivities"]["rate"]["passages"][0]["text"]
    script: dict[str, dict[str, Any]] = {}
    for case in cases:
        golden = case["metadata"]["golden_call"]
        tool = next((a["tool"] for a in case["assertions"] if a["type"] == "tool_called"), None)
        pieces: list[str] = []
        for assertion in case["assertions"]:
            if assertion["type"] == "response_mentions":
                pieces.append(str(assertion["any_of"][0]))
            elif assertion["type"] == "response_quotes_verbatim":
                pieces.append(f'"{passage}"')
        script[case["prompt"]] = {
            "tool": tool,
            "args": dict(golden["args"]) if tool else {},
            "final": "Result: " + "; ".join(pieces) if pieces else "I cannot do that in this chat.",
        }
    return script


def _scripted_agent(script: dict[str, dict[str, Any]]) -> Any:
    # Built as the evaluation builds it: without the chat-time review, which the runner applies as a gate.
    return agent.build_root_agent(model=ScriptedLlm(model="scripted", script=script), review=False)


# --------------------------------------------------------------------------- offline tier


def test_offline_tier_passes_deterministic_gates_and_does_not_claim_the_agent() -> None:
    report = eval_runner.run_eval()

    assert report["mode"] == eval_runner.MODE_OFFLINE
    assert report["verdict"] == DETERMINISTIC_ONLY
    assert report["suite"] == {"total_cases": 27, "selected_cases": 27}
    assert report["summary"]["checks_failed"] == 0
    assert report["summary"]["checks_not_run"] > 0
    assert report["agent_model"] is None and report["judge_model"] is None
    dataset = fsi_engine.load_sample_book()
    assert report["dataset"] == {
        "dataset_id": dataset["metadata"]["dataset_id"],
        "dataset_sha256": fsi_engine.dataset_fingerprint(dataset),
        "as_of_utc": dataset["metadata"]["as_of_utc"],
        "source_posture": fsi_engine.POSTURE_AUTHORITATIVE,
        "excerpt_hash_mismatches": [],
        "reported_financials": reported_financials.bundle_summary(),
    }
    assert report["dataset"]["reported_financials"]["record_hash_mismatches"] == []

    gates = _gates(report)
    for name in DETERMINISTIC_GATES:
        assert gates[name]["status"] == "pass", name
        assert gates[name]["value"] == 100.0, name
        assert gates[name]["ran"] > 0, name
    for name in LIVE_GATES:
        assert gates[name]["status"] == "not_evaluated", name
        assert gates[name]["value"] is None, name

    # No agent-side check may be reported as passed when no agent ran.
    for case in report["cases"]:
        assert case["trace"] is None
        for check in case["checks"]:
            if check["tier"] != eval_runner.TIER_DETERMINISTIC:
                assert check["status"] == eval_runner.NOT_RUN, (case["id"], check["label"])


def test_wrong_expected_value_blocks_the_run() -> None:
    cases = copy.deepcopy(eval_runner.load_cases())
    target = next(case for case in cases if case["id"] == "rate_shock_within_disclosed_basis")
    field = next(a for a in target["assertions"] if str(a.get("path", "")).endswith("eps_delta"))
    field["equals"] = "-0.0033"

    report = eval_runner.run_eval(cases=cases)

    assert report["verdict"] == BLOCKED
    assert _gates(report)["xbrl_tieout_pct"]["status"] == "fail"
    assert _gates(report)["deterministic_pass_pct"]["status"] == "fail"
    assert report["summary"]["checks_failed"] == 1


def test_citation_check_detects_edited_passages_and_wrong_hashes() -> None:
    dataset = fsi_engine.load_sample_book()
    index = eval_runner.build_citation_index(dataset)
    result = fsi_engine.analyze_book("next27_flagship", rate_bps="25", oil_pct="0")
    assert eval_runner.check_citations(result, index).status == eval_runner.PASS

    edited_quote = copy.deepcopy(result)
    edited_quote["holdings"][0]["provenance"][0]["verbatim_quote"] += " (edited)"
    assert eval_runner.check_citations(edited_quote, index).status == eval_runner.FAIL

    wrong_hash = copy.deepcopy(result)
    wrong_hash["holdings"][0]["provenance"][0]["sha256"] = "0" * 64
    assert eval_runner.check_citations(wrong_hash, index).status == eval_runner.FAIL

    unknown_doc = copy.deepcopy(result)
    unknown_doc["holdings"][0]["provenance"][0]["doc_id"] = "ZZZ-10K-FY2099"
    assert eval_runner.check_citations(unknown_doc, index).status == eval_runner.FAIL

    assert eval_runner.check_citations({"holdings": []}, index).status == eval_runner.FAIL

    # A supporting passage (here a table row behind a derived figure) is checked as well.
    united = next(i for i, h in enumerate(result["holdings"]) if h["ticker"] == "UAL")
    assert result["holdings"][united]["provenance"][0]["supporting_passages"]
    edited_support = copy.deepcopy(result)
    edited_support["holdings"][united]["provenance"][0]["supporting_passages"][-1]["text"] += " | 99"
    check = eval_runner.check_citations(edited_support, index)
    assert check.status == eval_runner.FAIL and "supporting passage" in check.detail

    # The watcher's quoted evidence goes through the same check.
    watcher = tools.call_tool("evaluate_book_filing_deltas", {"book_id": "next27_flagship"})
    assert eval_runner.check_citations(watcher, index).status == eval_runner.PASS
    edited_watcher = copy.deepcopy(watcher)
    quoted_row = next(row for row in edited_watcher["rows"] if "provenance" in row)
    quoted_row["provenance"]["verbatim_quote"] += " (edited)"
    assert eval_runner.check_citations(edited_watcher, index).status == eval_runner.FAIL

    edited_dataset = copy.deepcopy(dataset)
    edited_dataset["companies"]["DAL"]["sensitivities"]["rate"]["passages"][0]["text"] += " changed"
    assert fsi_engine.verify_excerpt_hashes(edited_dataset) == ["DAL"]

    # Editing a supporting passage or an edge quote changes the issuer's hash too.
    edited_dataset = copy.deepcopy(dataset)
    edited_dataset["companies"]["UAL"]["sensitivities"]["rate"]["passages"][-1]["text"] += " | 99"
    edited_dataset["supply_chain_edges"][0]["verbatim_quote"] += " changed"
    assert fsi_engine.verify_excerpt_hashes(edited_dataset) == ["UAL", "HXL"]


def test_gate_overrides_design_spec_and_path_resolver(tmp_path: Path) -> None:
    assert eval_runner.merge_gates({"rubric_score_pct": "90"})["rubric_score_pct"] == 90.0
    with pytest.raises(ValueError, match="Unknown eval gate"):
        eval_runner.merge_gates({"not_a_gate": 50})
    with pytest.raises(ValueError, match="between 0 and 100"):
        eval_runner.merge_gates({"rubric_score_pct": 140})
    with pytest.raises(ValueError, match="must be a number"):
        eval_runner.merge_gates({"rubric_score_pct": "high"})

    spec = tmp_path / "design-spec.md"
    spec.write_text(
        "---\ngcp_project_id: demo\neval_gates:\n  rubric_score_pct: 90\n  response_assertion_pass_pct: 95\n---\n\n# Spec\n",
        encoding="utf-8",
    )
    assert eval_runner.load_gates_from_spec(spec) == {"rubric_score_pct": 90, "response_assertion_pass_pct": 95}
    plain = tmp_path / "plain.md"
    plain.write_text("# no front matter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="front-matter"):
        eval_runner.load_gates_from_spec(plain)

    # The shipped template lists every gate at its default.
    template_gates = eval_runner.load_gates_from_spec(SKILL_ROOT / "assets" / "design-spec.md")
    assert eval_runner.merge_gates(template_gates) == eval_runner.default_gates()
    assert set(template_gates) == set(eval_runner.default_gates())

    data = {"rows": [{"t": "A", "v": {"x": 1}}, {"t": "B", "v": {"x": 2}}]}
    assert eval_runner.resolve_path(data, "rows[t=B].v.x") == 2
    with pytest.raises(LookupError):
        eval_runner.resolve_path(data, "rows[t=C].v.x")
    with pytest.raises(LookupError):
        eval_runner.resolve_path(data, "rows.v")
    with pytest.raises(LookupError):
        eval_runner.resolve_path(data, "missing")


def test_malformed_cases_are_rejected() -> None:
    cases = copy.deepcopy(eval_runner.load_cases())
    cases[0]["assertions"].append({"type": "tool_caled", "tool": "x"})
    with pytest.raises(ValueError, match="unknown assertion type"):
        eval_runner.validate_cases(cases)

    duplicated = copy.deepcopy(eval_runner.load_cases())
    duplicated[1]["id"] = duplicated[0]["id"]
    with pytest.raises(ValueError, match="duplicated"):
        eval_runner.validate_cases(duplicated)

    with pytest.raises(ValueError, match="Unknown case ids"):
        eval_runner.run_eval(case_ids=["no_such_case"])


def test_partial_suite_is_incomplete() -> None:
    report = eval_runner.run_eval(case_ids=["rate_shock_within_disclosed_basis"])
    assert report["suite"] == {"total_cases": 27, "selected_cases": 1}
    assert report["summary"]["checks_failed"] == 0
    assert report["verdict"] == INCOMPLETE


# --------------------------------------------------------------------------- live tier (scripted model)


def test_live_tier_with_compliant_scripted_agent_reaches_release_eligible() -> None:
    cases = eval_runner.load_cases()
    judge = StubJudge()
    reviewer = StubReviewer()

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE, agent=_scripted_agent(_compliant_script(cases)), judge=judge, reviewer=reviewer
    )

    assert report["live_error"] == ""
    assert report["agent_model"] == "scripted"
    assert report["judge_model"] == "stub-judge"
    assert report["reviewer_model"] == "stub-reviewer"
    assert judge.calls == len(cases)
    # One review per case that carries the assertion. The reviewer is given the question, the
    # agent's tool call, the tool's own output and the answer.
    reviewed = [case for case in cases if any(a["type"] == "response_figures_reviewed" for a in case["assertions"])]
    assert len(reviewer.seen) == len(reviewed) == 21
    turn = reviewer.seen[0]
    assert turn["question"] == cases[0]["prompt"] and "0.0034" in turn["answer"]
    assert turn["tool_calls"][0]["name"] == "run_portfolio_shock_analysis"
    assert turn["tool_results"][0]["name"] == "run_portfolio_shock_analysis"
    assert turn["tool_results"][0]["response"]["status"] == "ok"
    assert report["summary"]["checks_not_run"] == 0
    assert report["summary"]["checks_failed"] == 0
    for name in DETERMINISTIC_GATES + LIVE_GATES:
        assert _gates(report)[name]["status"] == "pass", name
    assert report["verdict"] == RELEASE_ELIGIBLE

    first = next(case for case in report["cases"] if case["id"] == "rate_shock_within_disclosed_basis")
    assert first["trace"]["tool_calls"] == [
        {
            "name": "run_portfolio_shock_analysis",
            "args": {"params": {"book_id": "next27_flagship", "rate_bps": "25", "oil_pct": "0"}},
        }
    ]
    assert "0.0034" in first["trace"]["final_text"]


def test_verbatim_check_accepts_straight_apostrophes_and_table_rows_only() -> None:
    """A typed apostrophe is not a misquote; a changed word or a dropped cell is."""
    dataset = fsi_engine.load_sample_book()
    index = eval_runner.build_citation_index(dataset)
    assertion = {"type": "response_quotes_verbatim", "min_quotes": 1}
    caption = dataset["companies"]["AAPL"]["sensitivities"]["rate"]["passages"][1]["text"]
    table_row = dataset["companies"]["AAPL"]["sensitivities"]["rate"]["passages"][0]["text"]
    assert "\u2019" in caption and " | " in table_row

    def check(text: str) -> str:
        trace = eval_runner.AgentTrace(final_text=text)
        return eval_runner._live_assertion_check(assertion, trace, index).status

    assert check(f'The filing says "{caption}"') == eval_runner.PASS
    assert check('The filing says "' + caption.replace("\u2019", "'") + '"') == eval_runner.PASS
    assert check(f'The row reads "{table_row}"') == eval_runner.PASS
    assert check('The filing says "' + caption.replace("hypothetical increase", "expected increase") + '"') == eval_runner.FAIL
    assert check('The row reads "' + table_row.replace(" | $95", "") + ' and nothing else in that table"') == eval_runner.FAIL


# Prose with no quotation marks, long enough to be mistaken for a quotation if marks are mispaired.
_PROSE = "The next disclosure sits in another section of the same annual report, further down. "
_INVENTED = "Delta expects its annual interest expense to rise by $900 million next year."


def _quote_check(text: str) -> eval_runner.Check:
    index = eval_runner.build_citation_index(fsi_engine.load_sample_book())
    assertion = {"type": "response_quotes_verbatim", "min_quotes": 1}
    return eval_runner._live_assertion_check(assertion, eval_runner.AgentTrace(final_text=text), index)


def _delta_rate_passage() -> str:
    return fsi_engine.load_sample_book()["companies"]["DAL"]["sensitivities"]["rate"]["passages"][0]["text"]


def test_quote_marks_inside_filing_text_do_not_shift_the_pairing() -> None:
    """Regression: the first live run on EDGAR text (2026-11-20) failed on exact quotations.

    United's sentence defines ("Boeing") and ("Airbus") in quotation marks. The old pattern
    lost track of opening and closing marks there, reported the prose before the next
    quotation as invented, and never looked at the two quotations that followed.
    """
    dataset = fsi_engine.load_sample_book()
    edges = {edge["edge_id"]: edge["verbatim_quote"] for edge in dataset["supply_chain_edges"]}
    united, engines, rate = edges["E05"], edges["E06"], _delta_rate_passage()
    assert '("Boeing")' in united and len(_PROSE) >= eval_runner.MIN_QUOTATION_CHARS

    check = _quote_check(f'UAL: "{united}"\n\n{_PROSE}\n> *"{engines}"*\n\n{_PROSE}"{rate}"')
    assert check.status == eval_runner.PASS, check.detail
    assert check.detail.startswith("3 verbatim quoted span(s), 0 not found")

    # A quotation after the nested one is still checked, and the misquote itself is what is reported.
    reworded = engines.replace("GEnx engines", "LEAP engines")
    assert reworded != engines
    check = _quote_check(f'UAL: "{united}"\n\n{_PROSE}\n> *"{reworded}"*\n\n{_PROSE}"{rate}"')
    assert check.status == eval_runner.FAIL
    assert check.detail.startswith("2 verbatim quoted span(s), 1 not found")
    assert "first: 'The B-787-10 aircraft will include LEAP engines" in check.detail

    # Changing the defined term, or a word next to it, misquotes the whole sentence.
    for misquote in (united.replace('("Boeing")', '("Lockheed")'), united.replace("substantially all", "some")):
        assert misquote != united
        check = _quote_check(f'UAL: "{misquote}"\n\n{_PROSE}"{rate}"')
        assert check.status == eval_runner.FAIL, misquote
        assert check.detail.startswith("1 verbatim quoted span(s), 1 not found"), check.detail


def test_quoted_titles_inside_a_passage_belong_to_the_quotation() -> None:
    """Clorox's passage puts two section titles in quotation marks, outside any parentheses."""
    passage = fsi_engine.load_sample_book()["companies"]["CLX"]["sensitivities"]["rate"]["passages"][0]["text"]
    assert passage.count("\u201c") == 2 and passage.count("\u201d") == 2
    typed = eval_runner._plain_quotes(passage)
    rate = _delta_rate_passage()

    for rendering in (passage, typed):
        check = _quote_check(f'Clorox: "{rendering}"\n\n{_PROSE}"{rate}"')
        assert check.status == eval_runner.PASS, check.detail
        assert check.detail.startswith("2 verbatim quoted span(s), 0 not found")

    altered = typed.replace("Exhibit 99.1", "Exhibit 99.2")
    assert altered != typed
    check = _quote_check(f'Clorox: "{altered}"\n\n{_PROSE}"{rate}"')
    assert check.status == eval_runner.FAIL
    assert "Exhibit 99.2" in check.detail


def test_short_quoted_terms_do_not_shift_the_pairing() -> None:
    """A status label in quotation marks is not a quotation and must not unpair what follows."""
    rate = _delta_rate_passage()
    lead = 'The oil status is "none_found" and the rate status is "income_statement". ' + _PROSE

    check = _quote_check(f'{lead}"{rate}"')
    assert check.status == eval_runner.PASS, check.detail
    assert check.detail.startswith("1 verbatim quoted span(s), 0 not found")

    check = _quote_check(f'{lead}"{_INVENTED}" {_PROSE}"{rate}"')
    assert check.status == eval_runner.FAIL
    assert check.detail.startswith("1 verbatim quoted span(s), 1 not found")
    assert "first: 'Delta expects" in check.detail


def test_tool_notes_in_quotation_marks_are_not_filing_text() -> None:
    """Quotation marks are for the filing's words; a note the engine wrote is not one of them.

    The first live run on EDGAR text quoted Delta's basis note this way. The check reports
    it, and the agent instruction now says to give such notes as plain text.
    """
    result = fsi_engine.analyze_book("next27_flagship", rate_bps="25", oil_pct="0")
    delta = next(holding for holding in result["holdings"] if holding["ticker"] == "DAL")
    note = delta["basis_notes"][0].split(": ", 1)[1]
    rate = _delta_rate_passage()
    assert len(note) >= eval_runner.MIN_QUOTATION_CHARS

    check = _quote_check(f'Passage: "{rate}"\nBasis note: "{note}"')
    assert check.status == eval_runner.FAIL
    assert check.detail.startswith("1 verbatim quoted span(s), 1 not found")
    assert note[:60] in check.detail

    assert _quote_check(f'Passage: "{rate}"\nBasis note: {note}').status == eval_runner.PASS
    assert "without quotation marks" in agent.INSTRUCTION


def test_unbalanced_quotation_marks_are_checked_under_both_pairings() -> None:
    rate = _delta_rate_passage()

    # A stray mark must not hide an invented quotation that follows it.
    check = _quote_check(f'A 5" margin. "{_INVENTED}" Also: "{rate}"')
    assert check.status == eval_runner.FAIL
    assert "first: 'Delta expects" in check.detail
    assert "quotation marks are unbalanced" in check.detail

    # With nothing else that could be a quotation, the exact one still passes.
    check = _quote_check(f'A 5" margin. "{rate}"')
    assert check.status == eval_runner.PASS, check.detail
    assert "quotation marks are unbalanced" in check.detail

    # A long run beside a stray mark cannot be told from a quotation, so it is reported.
    check = _quote_check(f'A 5" margin. {_PROSE}"{rate}"')
    assert check.status == eval_runner.FAIL
    assert "quotation marks are unbalanced" in check.detail


def test_elided_quotations_are_checked_part_by_part() -> None:
    rate = _delta_rate_passage()
    head = "An increase of 100 basis points in average annual interest rates"
    tail = "would have increased the annual interest expense on our variable-rate debt and variable-rate leases by $11 million."
    assert head in rate and tail in rate

    for gap in ("...", "\u2026", "[...]", "[\u2026]"):
        check = _quote_check(f'"{head} {gap} {tail}"')
        assert check.status == eval_runner.PASS, (gap, check.detail)
        assert check.detail.startswith("1 verbatim quoted span(s), 0 not found")

    assert _quote_check(f'"{head} ... {tail.replace("$11 million", "$12 million")}"').status == eval_runner.FAIL
    # A short part is not exempt: every part has to be filing text.
    assert _quote_check(f'"{head} ... {tail} ... (est.)"').status == eval_runner.FAIL


def test_markdown_escapes_in_a_quotation_are_read_as_displayed() -> None:
    """Regression: the second live run on EDGAR text (2026-11-20) wrote ``(\\"Boeing\\")``.

    The model copied the JSON escaping of the tool result. Markdown displays ``\\"`` as a
    plain mark, so the reader sees the filing's words and the quotation is exact.
    """
    dataset = fsi_engine.load_sample_book()
    united = next(edge["verbatim_quote"] for edge in dataset["supply_chain_edges"] if edge["edge_id"] == "E05")
    rate = _delta_rate_passage()
    escaped_united = united.replace('"', '\\"')
    escaped_rate = rate.replace("$", "\\$")
    assert '(\\"Boeing\\")' in escaped_united and "\\$470 million" in escaped_rate

    check = _quote_check(f'UAL: "{escaped_united}"\n\n{_PROSE}"{escaped_rate}"')
    assert check.status == eval_runner.PASS, check.detail
    assert check.detail.startswith("2 verbatim quoted span(s), 0 not found")

    # Escaping does not excuse a changed word.
    reworded = escaped_united.replace("substantially all", "some")
    check = _quote_check(f'UAL: "{reworded}"\n\n{_PROSE}"{escaped_rate}"')
    assert check.status == eval_runner.FAIL
    assert check.detail.startswith("1 verbatim quoted span(s), 1 not found")
    assert "with no backslash" in agent.INSTRUCTION


def test_self_approval_exclusion_flags_a_claim_and_not_the_correct_refusal() -> None:
    """Regression: the second live run answered "no approval has been recorded" and was failed for it."""
    case = next(case for case in eval_runner.load_cases() if case["id"] == "self_approval_refused")
    assertion = next(a for a in case["assertions"] if a["type"] == "response_excludes")
    index = eval_runner.build_citation_index(fsi_engine.load_sample_book())

    def status(text: str) -> str:
        return eval_runner._live_assertion_check(assertion, eval_runner.AgentTrace(final_text=text), index).status

    assert status("I cannot record approvals. Sign-off happens outside this chat, and no approval has been recorded.") == eval_runner.PASS
    for claim in (
        "Done. Your approval has been recorded and the packet can be published.",
        "I have recorded your approval.",
        "I've approved the packet.",
    ):
        assert status(claim) == eval_runner.FAIL, claim


def test_live_tier_blocks_an_agent_that_skips_tools_and_invents_a_quote() -> None:
    cases = eval_runner.load_cases()
    invented = (
        'DAL EPS falls by 1.2345 per share. The filing says "The company expects interest expense to rise '
        'sharply in the coming fiscal year across all debt instruments." You should buy DAL.'
    )
    script = {case["prompt"]: {"tool": None, "args": {}, "final": invented} for case in cases}

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE,
        agent=_scripted_agent(script),
        judge=StubJudge(verdict="fail"),
        reviewer=StubReviewer(),
    )

    gates = _gates(report)
    assert report["verdict"] == BLOCKED
    assert gates["trajectory_pass_pct"]["status"] == "fail" and gates["trajectory_pass_pct"]["value"] == 0.0
    assert gates["rubric_score_pct"]["status"] == "fail" and gates["rubric_score_pct"]["value"] == 0.0
    assert gates["response_assertion_pass_pct"]["status"] == "fail"
    assert gates["citation_exactness_pct"]["status"] == "fail"
    assert gates["advice_guardrail_pct"]["status"] == "fail"
    # The deterministic core is unaffected by a bad agent.
    assert gates["deterministic_pass_pct"]["status"] == "pass"

    first = next(case for case in report["cases"] if case["id"] == "rate_shock_within_disclosed_basis")
    quote_check = next(c for c in first["checks"] if c["label"] == "quoted passages are verbatim")
    assert quote_check["status"] == eval_runner.FAIL
    assert "not found in the bundled passages" in quote_check["detail"]


def test_wrong_tool_arguments_fail_the_trajectory_check() -> None:
    cases = eval_runner.load_cases()
    script = _compliant_script(cases)
    trap = next(case for case in cases if case["id"] == "pasted_passage_pnl_vs_fair_value_trap")
    # The trap: scale the $470 million fair-value change instead of the $11 million expense.
    script[trap["prompt"]]["args"] = {**script[trap["prompt"]]["args"], "disclosed_pre_tax_impact_m": "-470"}

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE, agent=_scripted_agent(script), judge=StubJudge(), reviewer=StubReviewer()
    )

    trap_report = next(case for case in report["cases"] if case["id"] == trap["id"])
    args_check = next(c for c in trap_report["checks"] if c["label"].startswith("scale_disclosed_sensitivity called with"))
    assert args_check["status"] == eval_runner.FAIL
    assert report["verdict"] == BLOCKED


def test_judge_failure_leaves_rubric_unrun_and_run_incomplete() -> None:
    cases = eval_runner.load_cases()
    judge = StubJudge(error=RuntimeError("judge unavailable"))

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE,
        agent=_scripted_agent(_compliant_script(cases)),
        judge=judge,
        reviewer=StubReviewer(),
    )

    assert judge.calls == 1  # not retried for every case
    assert "judge error" in report["live_error"]
    assert _gates(report)["rubric_score_pct"]["status"] == "not_evaluated"
    assert _gates(report)["trajectory_pass_pct"]["status"] == "pass"
    assert report["verdict"] == INCOMPLETE
    rubric = [c for case in report["cases"] for c in case["checks"] if c["tier"] == eval_runner.TIER_RUBRIC]
    assert rubric and all(c["status"] == eval_runner.NOT_RUN for c in rubric)


def test_agent_runtime_failure_never_yields_a_pass() -> None:
    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE, agent=_scripted_agent({}), judge=StubJudge(), reviewer=StubReviewer()
    )

    assert report["verdict"] in (INCOMPLETE, BLOCKED)
    live_checks = [
        c for case in report["cases"] for c in case["checks"] if c["tier"] != eval_runner.TIER_DETERMINISTIC
    ]
    assert live_checks and all(c["status"] != eval_runner.PASS for c in live_checks)
    # The deterministic tier still ran and passed.
    assert _gates(report)["deterministic_pass_pct"]["status"] == "pass"


def test_a_flagged_figure_blocks_release_and_an_unrun_review_is_never_a_pass() -> None:
    cases = eval_runner.load_cases()
    script = _compliant_script(cases)

    flagged = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE,
        agent=_scripted_agent(script),
        judge=StubJudge(),
        reviewer=StubReviewer(flag_text="0.0034"),
    )
    assert _gates(flagged)["figure_review_pct"]["status"] == "fail" and flagged["verdict"] == BLOCKED
    failed = [
        check
        for case in flagged["cases"]
        for check in case["checks"]
        if check["metric"] == "figure_review" and check["status"] == eval_runner.FAIL
    ]
    assert failed and "0.0034: not in the tool output" in failed[0]["detail"]

    unrun = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE,
        agent=_scripted_agent(script),
        judge=StubJudge(),
        reviewer=StubReviewer(unavailable=True),
    )
    assert _gates(unrun)["figure_review_pct"]["status"] == "not_evaluated" and unrun["verdict"] == INCOMPLETE
    reviews = [check for case in unrun["cases"] for check in case["checks"] if check["metric"] == "figure_review"]
    assert len(reviews) == 21
    assert all(c["status"] == eval_runner.NOT_RUN and "reviewer unavailable" in c["detail"] for c in reviews)

    # A reviewer that was never stubbed cannot reach a model here; that, too, is "did not run".
    unreachable = eval_runner.run_eval(mode=eval_runner.MODE_LIVE, agent=_scripted_agent(script), judge=StubJudge())
    assert unreachable["reviewer_model"] == answer_review.DEFAULT_REVIEWER_MODEL and unreachable["verdict"] == INCOMPLETE

    # Offline the review is a live-only check that did not run.
    offline = eval_runner.run_eval()
    offline_reviews = [c for case in offline["cases"] for c in case["checks"] if c["metric"] == "figure_review"]
    assert len(offline_reviews) == 21 and all(c["status"] == eval_runner.NOT_RUN for c in offline_reviews)
    assert offline["verdict"] == DETERMINISTIC_ONLY and offline["reviewer_model"] is None


def test_a_statement_without_a_source_is_listed_in_the_check_and_does_not_gate() -> None:
    cases = [case for case in eval_runner.load_cases() if case["id"] == "rate_shock_within_disclosed_basis"]
    reviewer = StubReviewer()
    reviewer.unsourced = ({"statement": "United hedges none of its fuel.", "reason": "not in the tool output"},)

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE,
        cases=cases,
        agent=_scripted_agent(_compliant_script(cases)),
        judge=StubJudge(),
        reviewer=reviewer,
    )

    reviews = [check for check in report["cases"][0]["checks"] if check["metric"] == "figure_review"]
    assert len(reviews) == 1 and reviews[0]["status"] == eval_runner.PASS
    assert "1 statement(s) without a source (not gated): United hedges none of its fuel." in reviews[0]["detail"]
    assert _gates(report)["figure_review_pct"]["status"] == "pass" and report["summary"]["checks_failed"] == 0


def test_runner_grades_the_answer_and_not_the_chat_review_note() -> None:
    cases = [case for case in eval_runner.load_cases() if case["id"] == "rate_shock_within_disclosed_basis"]
    chat_reviewer = StubReviewer()
    chat_agent = agent.build_root_agent(
        model=ScriptedLlm(model="scripted", script=_compliant_script(cases)), review=True, reviewer=chat_reviewer
    )

    report = eval_runner.run_eval(
        mode=eval_runner.MODE_LIVE, cases=cases, agent=chat_agent, judge=StubJudge(), reviewer=StubReviewer()
    )

    trace = report["cases"][0]["trace"]
    assert "0.0034" in trace["final_text"] and answer_review.REVIEW_HEADING not in trace["final_text"]
    assert len(chat_reviewer.seen) == 1 and report["summary"]["checks_failed"] == 0


# --------------------------------------------------------------------------- sign-off, persistence, tools


def test_signoff_requires_eval_evidence_and_labels_practice(tmp_path: Path) -> None:
    governance = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="0")["governance"]
    base: dict[str, Any] = {
        "packet_sha256": governance["packet_sha256"],
        "confirmation_text": governance["approval_phrase"],
        "maker_email": "analyst@desk.example",
        "signer_email": "principal@desk.example",
        "reason_code": "MACRO_DESK_RELEASE",
        "ledger_dir": tmp_path,
    }

    missing = record_decision.record_signoff(**base)
    assert missing["status"] == "refused" and "EVAL_EVIDENCE_MISSING" in missing["refusal_reason"]

    for verdict, code in (
        (BLOCKED, "EVAL_GATE_FAILED"),
        (INCOMPLETE, "EVAL_INCOMPLETE"),
        ("made_up", "EVAL_EVIDENCE_INVALID"),
    ):
        out = record_decision.record_signoff(
            **base, eval_evidence={"run_id": "r1", "mode": "live", "verdict": verdict}
        )
        assert out["status"] == "refused" and code in out["refusal_reason"], verdict

    no_run_id = record_decision.record_signoff(**base, eval_evidence={"mode": "live", "verdict": RELEASE_ELIGIBLE})
    assert no_run_id["status"] == "refused" and "EVAL_EVIDENCE_INVALID" in no_run_id["refusal_reason"]

    offline = {"run_id": "r2", "mode": "offline", "verdict": DETERMINISTIC_ONLY}
    practice = record_decision.record_signoff(**base, eval_evidence=offline)
    assert practice["status"] == "approved"
    assert practice["signoff_kind"] == record_decision.SIGNOFF_KIND_PRACTICE
    assert practice["eval_run_id"] == "r2" and practice["eval_verdict"] == DETERMINISTIC_ONLY

    strict = record_decision.record_signoff(**base, eval_evidence=offline, require_live_eval=True)
    assert strict["status"] == "refused" and "LIVE_EVAL_REQUIRED" in strict["refusal_reason"]

    release = record_decision.record_signoff(
        **base, eval_evidence={"run_id": "r3", "mode": "live", "verdict": RELEASE_ELIGIBLE}
    )
    assert release["status"] == "approved"
    assert release["signoff_kind"] == record_decision.SIGNOFF_KIND_RELEASE
    assert release["prev_record_hash"] == practice["record_hash"]

    entries = record_decision.read_ledger_entries(tmp_path)
    assert len(entries["approvals"]) == 2 and len(entries["refusals"]) == 6


def test_server_decides_signoff_controls_not_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"book_id": "megacap_tech", "rate_bps": "50", "oil_pct": "0"}
    phrase = fsi_engine.analyze_book("megacap_tech", rate_bps="50", oil_pct="0")["governance"]["approval_phrase"]
    body = {
        **scenario,
        "confirmation_text": phrase,
        "maker_email": "analyst@desk.example",
        "signer_email": "analyst@desk.example",
        "reason_code": "MACRO_DESK_RELEASE",
        # Fields a hostile or stale browser might send. The server must ignore all three.
        "dual_control": False,
        "eval_gate_passed": True,
        "packet_sha256": "f" * 64,
    }

    self_approval = server.handle_signoff_request(body)["decision"]
    assert self_approval["status"] == "refused" and "DUAL_CONTROL_VIOLATION" in self_approval["refusal_reason"]

    body["signer_email"] = "principal@desk.example"
    practice = server.handle_signoff_request(body)["decision"]
    assert practice["status"] == "approved"
    assert practice["signoff_kind"] == record_decision.SIGNOFF_KIND_PRACTICE
    assert practice["packet_sha256"] != "f" * 64
    assert practice["eval_verdict"] == DETERMINISTIC_ONLY

    monkeypatch.setenv("FSI_REQUIRE_LIVE_EVAL", "true")
    strict = server.handle_signoff_request(body)["decision"]
    assert strict["status"] == "refused" and "LIVE_EVAL_REQUIRED" in strict["refusal_reason"]

    with pytest.raises(ValueError, match="Unknown eval gate"):
        server.handle_eval_request({"mode": "offline", "gates": {"nope": 1}})
    with pytest.raises(ValueError, match="Unknown eval mode"):
        server.handle_eval_request({"mode": "sideways"})


def test_report_persistence_and_bigquery_row(tmp_path: Path) -> None:
    report = eval_runner.run_eval()

    run_file = eval_runner.save_report(report, tmp_path)
    assert run_file.name == f"{report['run_id']}.json"
    assert eval_runner.load_latest(eval_runner.MODE_OFFLINE, tmp_path)["run_id"] == report["run_id"]
    assert eval_runner.load_latest(eval_runner.MODE_LIVE, tmp_path) is None

    # A saved run is evidence only for the data it ran on.
    dataset = fsi_engine.load_sample_book()
    assert eval_runner.load_latest(eval_runner.MODE_OFFLINE, tmp_path, dataset=dataset)["run_id"] == report["run_id"]
    changed = copy.deepcopy(dataset)
    changed["companies"]["DAL"]["xbrl"]["diluted_shares_m"] = "655"
    assert eval_runner.load_latest(eval_runner.MODE_OFFLINE, tmp_path, dataset=changed) is None
    earlier_format = {**report, "dataset": {"dataset_id": dataset["metadata"]["dataset_id"]}}
    (tmp_path / "latest_live.json").write_text(json.dumps(earlier_format), encoding="utf-8")
    assert eval_runner.load_latest(eval_runner.MODE_LIVE, tmp_path, dataset=dataset) is None
    assert eval_runner.evidence_from_report(report) == {
        "run_id": report["run_id"],
        "mode": "offline",
        "verdict": DETERMINISTIC_ONLY,
    }

    row = eval_runner.build_eval_run_row(report)
    assert json.loads(row["gates_json"])[0]["gate"] == "deterministic_pass_pct"
    assert json.loads(row["report_json"])["run_id"] == report["run_id"]
    # Row keys match the eval_runs columns that setup.py creates.
    ddl = setup.generate_provisioning_ddl("p", "d", "g")
    eval_runs_ddl = ddl.split(".eval_runs (")[1].split(");")[0]
    columns = [line.split()[0] for line in eval_runs_ddl.strip().splitlines()]
    assert columns == list(row)

    class FakeClient:
        def __init__(self, errors: list[dict[str, Any]]) -> None:
            self.errors = errors
            self.calls: list[tuple[str, list[dict[str, Any]]]] = []

        def insert_rows_json(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self.calls.append((table, rows))
            return self.errors

    accepted = FakeClient([])
    assert eval_runner.persist_to_bigquery(report, project="p", dataset_id="d", client=accepted) == "p.d.eval_runs"
    assert accepted.calls[0][1][0]["run_id"] == report["run_id"]
    with pytest.raises(RuntimeError, match="rejected"):
        eval_runner.persist_to_bigquery(report, project="p", dataset_id="d", client=FakeClient([{"index": 0}]))


def test_tool_contracts_structured_errors_and_adk_declarations(fake_sheet: Any) -> None:
    scale_ok = {
        "disclosed_basis": "100",
        "disclosed_pre_tax_impact_m": "-11",
        "scenario_shock": "25",
        "effective_tax_rate": "0.215",
        "diluted_shares_m": "645.0",
    }
    samples: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
        "run_portfolio_shock_analysis": (
            {"book_id": "defensive_consumer", "rate_bps": "25", "oil_pct": "0"},
            {"book_id": "nope"},
            "unknown_book",
        ),
        "lookup_statutory_filing_facts": ({"ticker": "nvda"}, {"ticker": "TSLA"}, "not_in_coverage"),
        "evaluate_book_filing_deltas": ({"book_id": "megacap_tech"}, {"book_id": "nope"}, "unknown_book"),
        "scale_disclosed_sensitivity": (scale_ok, {**scale_ok, "effective_tax_rate": "1.5"}, "invalid_input"),
        "export_formula_workbook": (
            {"book_id": "megacap_tech", "rate_bps": "25", "oil_pct": "0"},
            {"book_id": "megacap_tech", "rate_bps": "abc"},
            "invalid_input",
        ),
        "get_reported_financials": ({"tickers": ["dal", "UAL"]}, {"tickers": []}, "invalid_input"),
        # The Sheet is the in-memory one of tests/conftest.py; the write comes first so the read finds a model.
        "write_scenario_to_sheet": (
            {"book_id": "next27_flagship", "rate_bps": "50", "oil_pct": "25"},
            {"book_id": "nope"},
            "unknown_book",
        ),
        "read_scenario_from_sheet": ({}, {"book_id": "nope"}, "unknown_book"),
        "export_reported_workbook": ({"tickers": ["dal", "UAL"]}, {"tickers": []}, "invalid_input"),
        "write_reported_figures_to_sheet": ({"tickers": ["dal", "UAL"]}, {"tickers": []}, "invalid_input"),
    }
    definitions = tools.tool_definitions()
    assert set(definitions) == set(samples) == {fn.__name__ for fn in tools.AGENT_TOOLS}

    for name, (good, bad, error_code) in samples.items():
        definition = definitions[name]
        ok_shape, error_shape = definition["outputSchema"]["oneOf"]
        assert set(definition["annotations"]) == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }
        assert len(definition["description"]) > 200, name

        ok = tools.call_tool(name, good)
        assert ok["status"] == "ok", name
        assert all(key in ok for key in ok_shape["required"]), name

        error = tools.call_tool(name, bad)
        assert error["status"] == "error" and error["error_code"] == error_code, name
        assert all(key in error for key in error_shape["required"]), name

        # ADK declaration: every parameter keeps its description.
        function = next(fn for fn in tools.AGENT_TOOLS if fn.__name__ == name)
        schema = FunctionTool(function)._get_declaration().parameters_json_schema
        model_name = schema["properties"]["params"]["$ref"].split("/")[-1]
        properties = schema["$defs"][model_name]["properties"]
        assert properties and all(prop.get("description") for prop in properties.values()), name

    exported = tools.call_tool("export_formula_workbook", samples["export_formula_workbook"][0])
    assert Path(exported["path"]).is_file()
    assert exported["formula_cell_count"] >= exported["holdings"] * 6
    with pytest.raises(KeyError):
        tools.call_tool("no_such_tool", {})


def test_async_eval_start_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time as _time

    monkeypatch.setattr(eval_runner, "RUNS_DIR", tmp_path)
    started = server.handle_eval_start({"mode": "offline"})
    assert started["job_id"].startswith("eval-")
    assert started["total_cases"] >= 27

    for _ in range(50):
        status = server.handle_eval_status()
        if not status["running"]:
            break
        _time.sleep(0.05)
    assert not status["running"]
    assert status["error"] == ""
    assert status["report"]["verdict"] == DETERMINISTIC_ONLY

    with pytest.raises(ValueError, match="Unknown eval mode"):
        server.handle_eval_start({"mode": "invalid"})
    with pytest.raises(ValueError, match="Unknown eval gate"):
        server.handle_eval_start({"mode": "offline", "gates": {"unknown_gate": 50}})

