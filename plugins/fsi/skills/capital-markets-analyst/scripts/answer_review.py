"""Second-model review of an answer: its figures, and its statements that have no source.

After the analyst agent has answered, a second model is given the user's question, the
tool calls, the full tool outputs and the answer. It reports every number it cannot find
in the tool outputs or the question, and every number attached to the wrong company,
metric or period. It also lists every statement of fact about a named company (its
business, an event, the reason for a change in its results, a plan, a risk) that neither
the tool outputs nor the question support. In the chat the review is appended under the
answer (``make_review_callback``). In the live evaluation the figures are a gate (the
``response_figures_reviewed`` assertion in ``EVAL.yaml``); the statements are listed in
the detail of that check and do not gate.

Two rules hold everywhere. A review that could not run is reported as not run, never as
a pass. And a pass is a model's opinion, not proof: the README gives what the reviewer
caught and missed in the planted-error tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import os
import re
from typing import Any, Protocol

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_RUN = "not_run"

REVIEWER_MODEL_ENV = "FSI_REVIEWER_MODEL"
REVIEW_SWITCH_ENV = "FSI_ANSWER_REVIEW"
# The model the planted-error test was run with. A reviewer other than the agent's own model is preferable.
DEFAULT_REVIEWER_MODEL = "gemini-3.7-flash"
REVIEW_HEADING = "Second-model review"
MAX_PROBLEMS_SHOWN = 8

REVIEW_SYSTEM = (
    "You review an answer written by a financial analysis assistant. You are given the user's question, "
    "the tool calls the assistant made, the full tool outputs, and the answer. Do two things.\n\n"
    "1. Figures. Check every number in the answer. A number is supported when it appears in the tool "
    "outputs, in the tool call arguments or in the question (rounding, and a change of unit such as "
    "millions to billions, are fine) and it is attached to the right company, metric and period. Under "
    "\"problems\" report every number that is unsupported or attached to the wrong thing. Do not report a "
    "number that is supported.\n\n"
    "2. Statements. Under \"unsourced_statements\" report every statement of fact about a named company "
    "that neither the tool outputs nor the question support: what its business is, an event, the reason "
    "for a change in its results, a plan, a risk it faces. Copy the statement from the answer word for "
    "word. Do not report a statement that the tool outputs or the question support, even when it is "
    "reworded. Do not report a company's name, ticker or sector, a statement about what the tools or the "
    "data cover, a warning or a note that a tool returned, or a general explanation of a method, an "
    "accounting term or how markets behave that is not about one named company.\n\n"
    "Return verdict \"fail\" if you report at least one number under \"problems\", otherwise \"pass\". "
    "Statements do not change the verdict."
)
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [STATUS_PASS, STATUS_FAIL]},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"number": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["number", "reason"],
            },
        },
        "unsourced_statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"statement": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["statement", "reason"],
            },
        },
    },
    "required": ["verdict", "problems", "unsourced_statements"],
}

_EMPHASIS_RE = re.compile(r"[*_`]+")
_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Review:
    """Outcome of one review.

    ``status`` is about the figures: ``pass``, ``fail`` with the figures at fault, or
    ``not_run`` with the reason. ``unsourced`` lists the statements about a company for
    which the reviewer found no source; it never changes the status.
    """

    status: str
    model: str
    problems: tuple[dict[str, str], ...] = ()
    detail: str = ""
    unsourced: tuple[dict[str, str], ...] = ()


class AnswerReviewer(Protocol):
    """Reviews the figures and statements of one answer against the tool outputs of the same turn."""

    model_name: str

    def review(
        self,
        *,
        question: str,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        answer: str,
    ) -> Review:
        """Return the review. Must not raise: a failure to review is ``not_run``."""


def review_enabled() -> bool:
    """True unless ``FSI_ANSWER_REVIEW`` switches the chat-time review off."""
    return os.environ.get(REVIEW_SWITCH_ENV, "").strip().lower() not in _OFF


def _plain(text: str) -> str:
    """The answer without Markdown emphasis, so that a bold marker is not read as part of a number."""
    cleaned = _EMPHASIS_RE.sub("", text)
    for dash in ("\u2212", "\u2013", "\u2014"):
        cleaned = cleaned.replace(dash, "-")
    return cleaned


def _unwrapped(args: dict[str, Any]) -> dict[str, Any]:
    """ADK wraps a tool's single parameter object in ``{"params": {...}}``."""
    inner = args.get("params") if isinstance(args, dict) else None
    return inner if isinstance(inner, dict) else args


def review_request(
    question: str, tool_calls: list[dict[str, Any]], tool_results: list[dict[str, Any]], answer: str
) -> str:
    """The text the reviewer reads: question, tool calls, full tool outputs, answer."""
    calls = [{"tool": call["name"], "args": _unwrapped(call.get("args") or {})} for call in tool_calls]
    results = [{"tool": result["name"], "result": result.get("response")} for result in tool_results]
    return (
        f"QUESTION:\n{question}\n\n"
        f"TOOL CALLS:\n{json.dumps(calls, ensure_ascii=False, default=str)}\n\n"
        f"TOOL OUTPUTS (JSON):\n{json.dumps(results, ensure_ascii=False, default=str)}\n\n"
        f"ANSWER:\n{_plain(answer)}"
    )


class GeminiAnswerReviewer:
    """Reviewer: one structured-output Gemini call per answer through ``google-genai`` on Vertex AI."""

    def __init__(self, model: str | None = None, client: Any | None = None) -> None:
        self.model_name = model or os.environ.get(REVIEWER_MODEL_ENV, "").strip() or DEFAULT_REVIEWER_MODEL
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

    def review(
        self,
        *,
        question: str,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        answer: str,
    ) -> Review:
        """Ask the model for a verdict. Any failure, including malformed output, is ``not_run``."""
        try:
            from google.genai import types

            result = self._get_client().models.generate_content(
                model=self.model_name,
                contents=review_request(question, tool_calls, tool_results, answer),
                config=types.GenerateContentConfig(
                    system_instruction=REVIEW_SYSTEM,
                    response_mime_type="application/json",
                    response_json_schema=REVIEW_SCHEMA,
                    temperature=0.0,
                ),
            )
            parsed = json.loads(result.text or "")
            verdict = parsed["verdict"]
            problems = tuple(
                {"number": str(problem["number"]), "reason": str(problem["reason"])} for problem in parsed["problems"]
            )
            # A reply without the statements list did not review the statements, so it is not read as a review.
            unsourced = tuple(
                {"statement": str(item["statement"]), "reason": str(item["reason"])}
                for item in parsed["unsourced_statements"]
            )
            if verdict not in (STATUS_PASS, STATUS_FAIL):
                raise ValueError(f"unknown verdict {verdict!r}")
        except Exception as exc:  # noqa: BLE001 - a reviewer failure is not evidence about the answer
            return Review(STATUS_NOT_RUN, self.model_name, detail=f"{type(exc).__name__}: {exc}"[:300])
        # A listed problem always fails the review, whatever verdict came with it.
        status = STATUS_FAIL if problems or verdict == STATUS_FAIL else STATUS_PASS
        return Review(status, self.model_name, problems, unsourced=unsourced)


def _unsourced_lines(review: Review) -> list[str]:
    """The part of the note that lists the statements without a source; empty when there are none."""
    if not review.unsourced:
        return []
    lines = [
        f"{len(review.unsourced)} statement(s) about a company in the answer above have no source in the "
        "tool output. Read them as the model's own background, not as filing content."
    ]
    for item in review.unsourced[:MAX_PROBLEMS_SHOWN]:
        lines.append(f"- {item['statement']} ({item['reason']})")
    if len(review.unsourced) > MAX_PROBLEMS_SHOWN:
        lines.append(f"- and {len(review.unsourced) - MAX_PROBLEMS_SHOWN} more")
    return lines


def format_review(review: Review) -> str:
    """The note shown under an answer in the chat."""
    if review.status == STATUS_NOT_RUN:
        return (
            f"**{REVIEW_HEADING} did not run** ({review.detail or 'no reason given'}). "
            "The figures and statements in the answer above were not reviewed."
        )
    caveat = "This is a second model's check, not a guarantee."
    statements = _unsourced_lines(review)
    if review.status == STATUS_PASS:
        figures = (
            f"**{REVIEW_HEADING} ({review.model}):** no figure in the answer above was found to differ from "
            "the tool output"
        )
        if not statements:
            return f"{figures}, and no statement about a company was found without a source in it. {caveat}"
        return "\n".join([f"{figures}. {statements[0]}", *statements[1:], "", caveat])
    lines = [
        f"**{REVIEW_HEADING} ({review.model}):** {len(review.problems) or 'some'} figure(s) in the answer "
        "above could not be matched to the tool output. Check them before use."
    ]
    for problem in review.problems[:MAX_PROBLEMS_SHOWN]:
        lines.append(f"- `{problem['number']}`: {problem['reason']}")
    if len(review.problems) > MAX_PROBLEMS_SHOWN:
        lines.append(f"- and {len(review.problems) - MAX_PROBLEMS_SHOWN} more")
    if statements:
        lines.extend(["", *statements])
    return "\n".join(lines)


def is_review_note(text: str) -> bool:
    """True for the note the chat-time review appends, so that nothing mistakes it for the answer."""
    return text.lstrip().startswith(f"**{REVIEW_HEADING}")


def turn_from_events(events: list[Any], invocation_id: str, user_content: Any) -> dict[str, Any]:
    """Collect one turn from ADK session events: the question, tool calls, tool results and the answer."""
    question = "".join(part.text or "" for part in (getattr(user_content, "parts", None) or []))
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    answer = ""
    for event in events:
        if event.invocation_id != invocation_id or event.author == "user":
            continue
        for call in event.get_function_calls():
            tool_calls.append({"name": call.name, "args": dict(call.args or {})})
        for result in event.get_function_responses():
            tool_results.append({"name": result.name, "response": dict(result.response or {})})
        if event.partial or not event.content or not event.content.parts:
            continue
        text = "".join(part.text or "" for part in event.content.parts if not getattr(part, "thought", False))
        if text.strip() and not is_review_note(text):
            answer = text
    return {"question": question, "tool_calls": tool_calls, "tool_results": tool_results, "answer": answer}


def make_review_callback(
    reviewer: AnswerReviewer | None = None, *, always: bool = False
) -> Callable[[Any], Awaitable[Any]]:
    """Build the ADK after-agent callback that appends a review under an answer that used a tool.

    Args:
        reviewer: The reviewer to use; a ``GeminiAnswerReviewer`` is created on first use otherwise.
        always: Review even when ``FSI_ANSWER_REVIEW`` switches the review off.
    """
    state: dict[str, AnswerReviewer | None] = {"reviewer": reviewer}

    async def review_after_answer(context: Any) -> Any:
        if not always and not review_enabled():
            return None
        invocation = context.get_invocation_context()
        turn = turn_from_events(invocation.session.events, invocation.invocation_id, invocation.user_content)
        # Without a tool result there is nothing to compare the answer with (a refusal, a clarification).
        if not turn["tool_results"] or not turn["answer"].strip():
            return None
        if state["reviewer"] is None:
            state["reviewer"] = GeminiAnswerReviewer()
        try:
            review = await asyncio.to_thread(state["reviewer"].review, **turn)
        except Exception as exc:  # noqa: BLE001 - the answer stands; the note says the review did not run
            model = str(getattr(state["reviewer"], "model_name", "reviewer"))
            review = Review(STATUS_NOT_RUN, model, detail=f"{type(exc).__name__}: {exc}"[:300])
        from google.genai import types

        return types.Content(role="model", parts=[types.Part(text=format_review(review))])

    return review_after_answer
