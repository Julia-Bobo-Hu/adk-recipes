"""Fail-closed maker-checker sign-off ledger and IAP-safe Gemini Enterprise link builder.

A sign-off is only recorded when an evaluation run is attached as evidence. The
verdict of that run decides what kind of sign-off is possible:

* ``release_eligible``   every gate passed on a live agent run -> ``release`` sign-off
* ``deterministic_only`` offline replay only, the agent was not evaluated -> ``practice``
  sign-off (Quick Start), or a refusal when ``require_live_eval`` is set (Full Setup)
* ``incomplete`` / ``blocked`` / missing evidence -> refused
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from scripts import fsi_engine

DEFAULT_LEDGER_DIR = fsi_engine.SKILL_ROOT / "_signoff"

VERDICT_RELEASE_ELIGIBLE = "release_eligible"
VERDICT_DETERMINISTIC_ONLY = "deterministic_only"
VERDICT_INCOMPLETE = "incomplete"
VERDICT_BLOCKED = "blocked"

SIGNOFF_KIND_RELEASE = "release"
SIGNOFF_KIND_PRACTICE = "practice"


def format_iap_safe_url(url: str, corp_account_email: str | None = None) -> str:
    """Append Google corporate account hint (`authuser`) to prevent multi-account IAP 403s."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if corp_account_email and corp_account_email.strip():
        query["authuser"] = corp_account_email.strip()
    elif "authuser" not in query:
        query["authuser"] = "0"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _last_ledger_hash(ledger_file: Path) -> str:
    if not ledger_file.exists():
        return "GENESIS"
    lines = [ln for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return "GENESIS"
    last_entry = json.loads(lines[-1])
    return str(last_entry["record_hash"])


def read_ledger_entries(ledger_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return both approved hash-chained ledger entries and logged refusals."""
    target_dir = ledger_dir or DEFAULT_LEDGER_DIR
    ledger_file = target_dir / "decision_ledger.jsonl"
    refusals_file = target_dir / "refusals.jsonl"

    approvals = (
        [json.loads(ln) for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if ledger_file.exists()
        else []
    )
    refusals = (
        [json.loads(ln) for ln in refusals_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if refusals_file.exists()
        else []
    )
    return {"approvals": approvals, "refusals": refusals}


def assess_eval_evidence(
    eval_evidence: dict[str, Any] | None,
    *,
    require_live_eval: bool = False,
) -> tuple[str | None, str | None]:
    """Decide whether an evaluation run permits sign-off.

    Returns ``(refusal_reason, signoff_kind)``; exactly one of the two is ``None``.
    """
    if not eval_evidence:
        return (
            "EVAL_EVIDENCE_MISSING: no evaluation run is attached. Run the Q-MODE evaluation before sign-off.",
            None,
        )
    run_id = str(eval_evidence.get("run_id", "")).strip()
    verdict = str(eval_evidence.get("verdict", "")).strip()
    if not run_id:
        return "EVAL_EVIDENCE_INVALID: the evaluation evidence has no run_id.", None
    if verdict == VERDICT_BLOCKED:
        return f"EVAL_GATE_FAILED: evaluation run {run_id} has at least one failing gate.", None
    if verdict == VERDICT_INCOMPLETE:
        return f"EVAL_INCOMPLETE: evaluation run {run_id} did not run every required check.", None
    if verdict == VERDICT_DETERMINISTIC_ONLY:
        if require_live_eval:
            return (
                f"LIVE_EVAL_REQUIRED: evaluation run {run_id} is an offline replay; the agent itself "
                "was not evaluated. Run the live evaluation before a release sign-off.",
                None,
            )
        return None, SIGNOFF_KIND_PRACTICE
    if verdict == VERDICT_RELEASE_ELIGIBLE:
        return None, SIGNOFF_KIND_RELEASE
    return f"EVAL_EVIDENCE_INVALID: unknown evaluation verdict {verdict!r}.", None


def record_signoff(
    *,
    packet_sha256: str,
    confirmation_text: str,
    maker_email: str,
    signer_email: str,
    reason_code: str,
    notes: str = "",
    dual_control: bool = True,
    eval_evidence: dict[str, Any] | None = None,
    require_live_eval: bool = False,
    ledger_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate evaluation evidence, maker-checker separation and hash confirmation, then log.

    ``eval_evidence`` is ``{"run_id", "mode", "verdict"}`` from ``scripts.eval_runner``.
    It defaults to ``None``, which refuses: a caller cannot obtain a sign-off by
    simply omitting the evaluation.
    """
    target_dir = ledger_dir or DEFAULT_LEDGER_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = target_dir / "decision_ledger.jsonl"
    refusals_file = target_dir / "refusals.jsonl"

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_code6 = packet_sha256[:6].upper()
    expected_phrase = f"APPROVE {expected_code6}"

    maker_norm = maker_email.strip().lower()
    signer_norm = signer_email.strip().lower()
    reason_norm = reason_code.strip().upper()
    confirm_norm = " ".join(confirmation_text.strip().upper().split())
    evidence = dict(eval_evidence or {})
    eval_fields = {
        "eval_run_id": str(evidence.get("run_id", "")),
        "eval_mode": str(evidence.get("mode", "")),
        "eval_verdict": str(evidence.get("verdict", "")),
    }

    refusal_reason, signoff_kind = assess_eval_evidence(eval_evidence, require_live_eval=require_live_eval)
    if refusal_reason is None:
        if not packet_sha256.strip():
            refusal_reason = "MISSING_PACKET_HASH: there is no analysis packet to sign."
        elif not maker_norm or "@" not in maker_norm or not signer_norm or "@" not in signer_norm:
            refusal_reason = "INVALID_IDENTITY: Both maker_email and signer_email must be valid emails."
        elif dual_control and maker_norm == signer_norm:
            refusal_reason = (
                f"DUAL_CONTROL_VIOLATION: signer ({signer_norm}) cannot approve their own packet "
                f"(maker={maker_norm}). A second person must approve."
            )
        elif not reason_norm:
            refusal_reason = "MISSING_REASON_CODE: Explicit supervisory reason_code is required."
        elif confirm_norm != expected_phrase:
            refusal_reason = (
                f"HASH_CONFIRMATION_MISMATCH: Expected '{expected_phrase}', received '{confirmation_text.strip()}'."
            )
        elif not fsi_engine.enforce_non_advisory_guardrail(notes)["allowed"]:
            refusal_reason = "PROHIBITED_ADVISORY_LANGUAGE: Reviewer notes cannot contain buy/sell/hold advice."

    if refusal_reason is not None:
        refusal_entry = {
            "timestamp_utc": now_utc,
            "status": "refused",
            "packet_sha256": packet_sha256,
            "maker_email": maker_norm,
            "signer_email": signer_norm,
            "dual_control": dual_control,
            "reason_code": reason_norm,
            "refusal_reason": refusal_reason,
            **eval_fields,
        }
        with refusals_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(refusal_entry, sort_keys=True) + "\n")
        return refusal_entry

    prev_hash = _last_ledger_hash(ledger_file)
    unsigned_record = {
        "timestamp_utc": now_utc,
        "status": "approved",
        "signoff_kind": signoff_kind,
        "packet_sha256": packet_sha256,
        "confirm_code_6": expected_code6,
        "maker_email": maker_norm,
        "signer_email": signer_norm,
        "dual_control": dual_control,
        "reason_code": reason_norm,
        "notes": notes.strip(),
        "prev_record_hash": prev_hash,
        **eval_fields,
    }
    record_hash = hashlib.sha256(
        json.dumps(unsigned_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    signed_record = {**unsigned_record, "record_hash": record_hash}

    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(signed_record, sort_keys=True) + "\n")

    return signed_record
