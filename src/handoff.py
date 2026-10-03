"""Human handoff: what the reviewer receives, and an append-only record of what they decided.

The copilot never approves. A reviewer reads the packet, then records one of three actions. The
record keeps who, when, what the AI recommended (by hash), and the reason when the human disagrees.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from src.contracts import ProcurementDecision

LOG = Path(__file__).resolve().parents[1] / "data" / "audit_log.jsonl"
ACTIONS = ("accept_and_route", "return_to_requester", "override")


def decision_hash(d: ProcurementDecision) -> str:
    body = d.model_dump(exclude={"telemetry"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def build_packet(d: ProcurementDecision, request: dict) -> str:
    lines = [f"# Review packet: {d.request_id} ({request.get('product_name')}, {request.get('vendor_name')})", "",
             f"Requester {request.get('requester_id')}, annual cost {_money(request.get('annual_cost_usd'))}, data: {request.get('data_access_level')}", "",
             "## Recommendation", d.recommendation, "", "## Next step", d.next_step, "",
             "## Required approvals", ", ".join(d.required_approvals) or "none yet", "",
             "## Risk flags", ", ".join(d.risk_flags) or "none", "",
             "## Missing information", *(f"- {m}" for m in d.missing_information or ["none"]), "",
             "## Why a human must look", *(f"- {r}" for r in d.escalation_reasons or ["routine approvals only"]), "",
             "## Evidence", *(f"- [{e.status}] {e.finding} ({e.reference or e.source})" for e in d.evidence), "",
             f"Packet id {decision_hash(d)}. Advisory only: the copilot cannot approve a purchase."]
    return "\n".join(lines)


def record_review(d: ProcurementDecision, reviewer: str, action: str, note: str = "", log: Path = LOG) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    if not reviewer.strip():
        raise ValueError("reviewer name is required")
    if action == "override" and len(note.split()) < 3:
        raise ValueError("an override needs a written reason")
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request_id": d.request_id,
             "reviewer": reviewer.strip(), "action": action, "note": note.strip(),
             "ai_packet_id": decision_hash(d), "ai_required_approvals": d.required_approvals, "ai_risk_flags": d.risk_flags}
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log(request_id: str | None = None, log: Path = LOG) -> list[dict]:
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    return [r for r in rows if not request_id or r["request_id"] == request_id]


def _money(v) -> str:
    return "not given" if v is None else f"${v:,.0f}"

