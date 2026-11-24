"""The second-model review: what the reviewer is shown, how its verdict is read, and the note in the chat.

No model is called. The Gemini reviewer gets a stand-in client; the chat path runs through
the real ADK runner with a scripted model and a stub reviewer. How well a real model
reviews is measured separately (see "Second-model review" in the README).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts import agent, answer_review, sec_lookup  # noqa: E402

QUESTION = "What was Delta's revenue in its latest fiscal year?"
ANSWER = "Delta's revenue was **63,364** USD millions for the fiscal year ended 2025-12-31."


@pytest.fixture(autouse=True)
def _bundle_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sec_lookup.LIVE_LOOKUP_ENV, "false")
    monkeypatch.setenv(sec_lookup.STORE_DIR_ENV, str(tmp_path / "store"))
    monkeypatch.delenv(answer_review.REVIEW_SWITCH_ENV, raising=False)
    monkeypatch.delenv(answer_review.REVIEWER_MODEL_ENV, raising=False)


class FakeClient:
    """Stand-in for the google-genai client: returns a fixed text, or raises, and records the request."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[dict[str, Any]] = []
        self.models = self

    def generate_content(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _reply(verdict: str, problems: list[dict[str, str]] | None = None, unsourced: list[dict[str, str]] | None = None) -> str:
    """A reviewer reply in the shape of ``REVIEW_SCHEMA``."""
    return json.dumps({"verdict": verdict, "problems": problems or [], "unsourced_statements": unsourced or []})


def _review(client: FakeClient, **overrides: Any) -> answer_review.Review:
    turn = {
        "question": QUESTION,
        "tool_calls": [{"name": "get_reported_financials", "args": {"params": {"tickers": ["DAL"]}}}],
        "tool_results": [{"name": "get_reported_financials", "response": {"status": "ok", "revenue": "63364"}}],
        "answer": ANSWER,
    }
    return answer_review.GeminiAnswerReviewer(client=client).review(**{**turn, **overrides})


def test_reviewer_is_shown_the_question_the_tool_calls_the_full_outputs_and_the_answer() -> None:
    client = FakeClient(_reply("pass"))
    review = _review(client)
    assert review == answer_review.Review("pass", answer_review.DEFAULT_REVIEWER_MODEL)

    request = client.requests[0]
    assert request["model"] == "gemini-3.7-flash"
    contents = request["contents"]
    assert contents.index("QUESTION:") < contents.index("TOOL CALLS:") < contents.index("TOOL OUTPUTS (JSON):") < contents.index("ANSWER:")
    assert QUESTION in contents
    # ADK's {"params": ...} envelope is removed; the tool output is passed whole.
    assert '{"tool": "get_reported_financials", "args": {"tickers": ["DAL"]}}' in contents
    assert '"result": {"status": "ok", "revenue": "63364"}' in contents
    # Markdown emphasis is not part of a number.
    assert "revenue was 63,364 USD millions" in contents and "**" not in contents
    config = request["config"]
    assert config.system_instruction == answer_review.REVIEW_SYSTEM and config.temperature == 0.0
    assert config.response_mime_type == "application/json"


def test_a_listed_problem_fails_and_a_review_that_could_not_be_read_did_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    problem = {"number": "63,634", "reason": "the tool output gives 63364"}
    failed = _review(FakeClient(_reply("fail", [problem])))
    assert failed.status == "fail" and failed.problems == (problem,)
    # A problem listed next to a "pass" verdict still fails; a bare "fail" fails too.
    assert _review(FakeClient(_reply("pass", [problem]))).status == "fail"
    assert _review(FakeClient(_reply("fail"))).status == "fail"

    for client in (
        FakeClient("not json"),
        FakeClient(_reply("maybe")),
        FakeClient(json.dumps({"problems": [], "unsourced_statements": []})),
        # Without the statements list the statements were not reviewed, so the reply is not a review.
        FakeClient(json.dumps({"verdict": "pass", "problems": []})),
        FakeClient(None),
        FakeClient(error=RuntimeError("model unavailable")),
    ):
        review = _review(client)
        assert review.status == "not_run" and review.detail and not review.problems

    monkeypatch.setenv(answer_review.REVIEWER_MODEL_ENV, "another-model")
    assert answer_review.GeminiAnswerReviewer().model_name == "another-model"
    assert answer_review.GeminiAnswerReviewer(model="explicit").model_name == "explicit"


def test_the_note_never_presents_a_failed_or_unrun_review_as_a_pass() -> None:
    passed = answer_review.format_review(answer_review.Review("pass", "m"))
    assert passed.startswith("**Second-model review (m):**") and "not a guarantee" in passed

    problems = tuple({"number": str(n), "reason": f"reason {n}"} for n in range(10))
    failed = answer_review.format_review(answer_review.Review("fail", "m", problems))
    assert "10 figure(s)" in failed and "Check them before use" in failed
    assert "- `0`: reason 0" in failed and "- `7`: reason 7" in failed and "- `8`" not in failed and "and 2 more" in failed

    unrun = answer_review.format_review(answer_review.Review("not_run", "m", detail="RuntimeError: model unavailable"))
    assert "did not run" in unrun and "model unavailable" in unrun and "were not reviewed" in unrun
    for text in (failed, unrun):
        assert "no figure" not in text
    assert all(answer_review.is_review_note(text) for text in (passed, failed, unrun))
    assert not answer_review.is_review_note(ANSWER)


def test_statements_without_a_source_are_listed_and_never_change_the_verdict() -> None:
    statement = {"statement": "Revenue fell because of a pilot strike.", "reason": "no tool output mentions a strike"}
    review = _review(FakeClient(_reply("pass", unsourced=[statement])))
    assert review.status == "pass" and review.unsourced == (statement,) and not review.problems

    note = answer_review.format_review(review)
    assert "no figure in the answer above was found to differ" in note and "not a guarantee" in note
    assert "1 statement(s) about a company" in note and "- Revenue fell because of a pilot strike." in note
    assert "no statement about a company was found" not in note

    clean = answer_review.format_review(answer_review.Review("pass", "m"))
    assert "no statement about a company was found without a source" in clean

    problem = {"number": "63,634", "reason": "the tool output gives 63364"}
    both = answer_review.format_review(answer_review.Review("fail", "m", (problem,), unsourced=(statement,)))
    assert "1 figure(s)" in both and "1 statement(s) about a company" in both and "no figure" not in both

    many = tuple({"statement": f"statement {n}", "reason": "r"} for n in range(10))
    listed = answer_review.format_review(answer_review.Review("pass", "m", unsourced=many))
    assert "10 statement(s)" in listed and "- statement 7 (r)" in listed and "statement 8" not in listed
    assert "and 2 more" in listed

    # The reviewer is asked for the list, and the reply must carry it.
    assert '"unsourced_statements"' in answer_review.REVIEW_SYSTEM
    assert "unsourced_statements" in answer_review.REVIEW_SCHEMA["required"]


# --------------------------------------------------------------------------- the chat path, through ADK


class OneToolThenAnswer(BaseLlm):
    """Scripted model: calls the reported-figures tool once when asked about Delta, then answers."""

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        parts = [part for content in llm_request.contents for part in content.parts or []]
        asked = "".join(part.text or "" for part in parts)
        if "Delta" in asked and not any(part.function_response is not None for part in parts):
            call = types.FunctionCall(name="get_reported_financials", args={"params": {"tickers": ["DAL"], "fiscal_years": 1}})
            part = types.Part(function_call=call)
        else:
            part = types.Part(text=ANSWER if "Delta" in asked else "I can only help with company filings.")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class RecordingReviewer:
    model_name = "recording-reviewer"

    def __init__(self, review: answer_review.Review | Exception) -> None:
        self.outcome = review
        self.turns: list[dict[str, Any]] = []

    def review(self, **turn: Any) -> answer_review.Review:
        self.turns.append(turn)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _chat(reviewer: RecordingReviewer, question: str, review: bool | None = None) -> list[str]:
    """Run one chat turn and return the text of every final message, in order."""
    built = agent.build_root_agent(model=OneToolThenAnswer(model="scripted"), review=review, reviewer=reviewer)

    async def run() -> list[str]:
        runner = InMemoryRunner(agent=built, app_name="chat")
        session = await runner.session_service.create_session(app_name="chat", user_id="analyst")
        message = types.Content(role="user", parts=[types.Part(text=question)])
        texts: list[str] = []
        async for event in runner.run_async(user_id="analyst", session_id=session.id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                texts.append("".join(part.text or "" for part in event.content.parts))
        return texts

    return asyncio.run(run())


def test_chat_answer_is_followed_by_the_review_of_that_turn() -> None:
    reviewer = RecordingReviewer(answer_review.Review("pass", "recording-reviewer"))
    texts = _chat(reviewer, QUESTION)
    assert texts[0] == ANSWER and len(texts) == 2
    assert texts[1].startswith("**Second-model review (recording-reviewer):**")

    turn = reviewer.turns[0]
    assert turn["question"] == QUESTION and turn["answer"] == ANSWER
    assert turn["tool_calls"] == [{"name": "get_reported_financials", "args": {"params": {"tickers": ["DAL"], "fiscal_years": 1}}}]
    # The reviewer gets the tool's own output for this turn, where Delta's revenue is 63364.
    result = turn["tool_results"][0]
    assert result["name"] == "get_reported_financials"
    assert result["response"]["companies"][0]["fiscal_years"][0]["reported"]["revenue"]["value"] == "63364"

    flagged = RecordingReviewer(
        answer_review.Review("fail", "recording-reviewer", ({"number": "63,364", "reason": "example"},))
    )
    assert "could not be matched" in _chat(flagged, QUESTION)[1]

    unsourced = RecordingReviewer(
        answer_review.Review(
            "pass", "recording-reviewer", unsourced=({"statement": "Delta is the largest US airline.", "reason": "example"},)
        )
    )
    note = _chat(unsourced, QUESTION)[1]
    assert "1 statement(s) about a company" in note and "Delta is the largest US airline." in note


def test_chat_review_is_skipped_without_a_tool_result_and_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer = RecordingReviewer(answer_review.Review("pass", "recording-reviewer"))
    # No tool was called, so there is nothing to compare the answer with: no review, no note.
    assert _chat(reviewer, "Tell me a joke.") == ["I can only help with company filings."]
    assert reviewer.turns == []

    monkeypatch.setenv(answer_review.REVIEW_SWITCH_ENV, "false")
    assert _chat(reviewer, QUESTION) == [ANSWER] and reviewer.turns == []
    # review=True reviews whatever the switch says; review=False never does.
    assert len(_chat(reviewer, QUESTION, review=True)) == 2 and len(reviewer.turns) == 1
    monkeypatch.delenv(answer_review.REVIEW_SWITCH_ENV)
    assert _chat(reviewer, QUESTION, review=False) == [ANSWER] and len(reviewer.turns) == 1


def test_a_reviewer_that_breaks_leaves_the_answer_and_says_the_review_did_not_run() -> None:
    texts = _chat(RecordingReviewer(RuntimeError("quota exceeded")), QUESTION)
    assert texts[0] == ANSWER
    assert "did not run" in texts[1] and "quota exceeded" in texts[1] and "were not reviewed" in texts[1]
