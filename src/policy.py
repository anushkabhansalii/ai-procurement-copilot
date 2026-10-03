"""Deterministic policy engine for data/procurement_policy.md (v2026.09).

Everything that depends on an exact rule lives here: approval thresholds, budget comparison,
the 365-day review window, which reviews are required, and the missing-information check.
No model call is involved, and nothing in here reads the clock (REFERENCE_DATE is fixed).

Inputs are plain dicts so the functions can be tested without files, the network or an LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from src import config

APPROVAL_ORDER = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]


# --------------------------------------------------------------------------- helpers

def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def norm(value: Any) -> str:
    return str(value or "").strip().casefold()


# --------------------------------------------------------------------------- approvals (policy 4)

def approval_tier(annual_cost: Decimal | None) -> list[str] | None:
    """Minimum business approvals for an annual amount, or None when the amount is unknown.

    Boundaries follow the policy table: $1,000.00 is still the Manager tier, $1,000.01 is not.
    """
    if annual_cost is None or annual_cost < 0:
        return None
    for upper, approvals in config.APPROVAL_TIERS:
        if upper is None or annual_cost <= upper:
            return list(approvals)
    raise AssertionError("unreachable: last tier has no upper bound")


# --------------------------------------------------------------------------- budget (policy 2)

def budget_check(annual_cost: Decimal | None, available_usd: Decimal | None) -> dict:
    if annual_cost is None or available_usd is None:
        return {"checked": False, "within_budget": None, "shortfall": None}
    within = annual_cost <= available_usd
    return {
        "checked": True,
        "within_budget": within,
        "shortfall": None if within else annual_cost - available_usd,
    }


# --------------------------------------------------------------------------- vendor evidence (policy 5, 10)

def _review_state(label: str, review_date: date | None, ref: date) -> str:
    """Normalise one source's claim into: current | stale | expired | not_completed | unknown."""
    label = norm(label)
    if label in {"expired"}:
        return "expired"
    if label in {"pending", "not_completed", "not completed", "draft", "in_progress"}:
        return "not_completed"
    if label in {"approved", "current", "passed"}:
        if review_date is None:
            return "unknown"  # "approved" with no review date cannot be shown to be current
        age = (ref - review_date).days
        # Policy: current "for 365 days from its review date". Day 365 is still current.
        return "current" if age <= config.REVIEW_VALID_DAYS else "stale"
    return "unknown"


def vendor_view(
    registry_row: dict | None,
    api_status: str,
    api_record: dict | None,
    ref: date = config.REFERENCE_DATE,
) -> dict:
    """Combine the internal registry and the external risk service without silently picking one.

    api_status: ok | unavailable | not_found
    """
    reg_label = registry_row.get("security_status") if registry_row else None
    reg_date = parse_date(registry_row.get("security_review_date")) if registry_row else None
    reg_state = _review_state(reg_label, reg_date, ref) if registry_row else "unknown"

    api_state, api_date, api_label = "unavailable", None, None
    if api_status == "ok" and api_record:
        api_label = api_record.get("security_review_status")
        api_date = parse_date(api_record.get("last_review_date"))
        api_state = _review_state(api_label, api_date, ref)
    elif api_status == "not_found":
        api_state = "not_found"

    # Conflict = two sources that both made a claim and the claims differ. Compare the labels
    # each source reported, not only our normalised state, because a registry that says
    # "Approved" for a review that is 15 months old is exactly the disagreement to surface.
    def claim(label: str | None) -> str | None:
        n = norm(label)
        if n in {"approved", "current", "passed"}:
            return "approved"
        if n in {"expired"}:
            return "expired"
        if n in {"pending", "not_completed", "not completed", "draft", "in_progress"}:
            return "not_completed"
        return None  # unknown / absent = no claim

    reg_claim, api_claim = claim(reg_label), claim(api_label)
    conflict = False
    reasons: list[str] = []
    if reg_claim and api_claim and reg_claim != api_claim:
        conflict = True
        reasons.append(f"registry says '{reg_label}' but risk service says '{api_label}'")
    elif reg_claim == "approved" and api_claim == "approved" and reg_date and api_date and reg_date != api_date:
        conflict = True
        reasons.append(f"review dates differ (registry {reg_date}, risk service {api_date})")
    elif reg_claim == "approved" and reg_state == "stale" and api_status != "ok":
        reasons.append(f"registry marks the review approved but it is dated {reg_date}, older than {config.REVIEW_VALID_DAYS} days")

    states = {reg_state, api_state}
    expired = bool(states & {"expired", "stale"})
    not_completed = "not_completed" in states
    current = (
        registry_row is not None  # a vendor unknown to the internal registry has no vetted record
        and "current" in states
        and not expired
        and not not_completed
        and not conflict
    )
    return {
        "registry_state": reg_state,
        "api_state": api_state,
        "registry_review_date": reg_date.isoformat() if reg_date else None,
        "api_review_date": api_date.isoformat() if api_date else None,
        "conflict": conflict,
        "conflict_reasons": reasons,
        "expired": expired,
        "not_completed": not_completed,
        "unavailable": api_status != "ok",
        "current": current,
    }


# --------------------------------------------------------------------------- untrusted text (policy 9)

_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in config.INJECTION_PATTERNS]


def detect_injection(*texts: Any) -> list[str]:
    """Return short snippets of business data that try to instruct the system. Detection only:
    the text is never executed or obeyed, and the policy engine does not read it as a rule."""
    hits: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for rx in _INJECTION_RES:
            m = rx.search(text)
            if m:
                hits.append(m.group(0)[:80])
    return sorted(set(hits))


def purpose_text_without_instructions(text: Any) -> str:
    """Business justification with instruction-like sentences removed."""
    if not isinstance(text, str):
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not detect_injection(s)]
    return " ".join(kept).strip()


# --------------------------------------------------------------------------- the assessment

@dataclass
class PolicyAssessment:
    approvals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    # (finding, reference, status) triples; tools.py turns them into EvidenceItem objects.
    findings: list[tuple[str, str, str]] = field(default_factory=list)
    action: str = "route_for_approval"
    facts: dict = field(default_factory=dict)

    def add_flag(self, flag: str) -> None:
        if flag not in self.risk_flags:
            self.risk_flags.append(flag)

    def add_approval(self, name: str) -> None:
        if name not in self.approvals:
            self.approvals.append(name)

    def escalate(self, reason: str) -> None:
        if reason not in self.escalation_reasons:
            self.escalation_reasons.append(reason)


ACTION_LABELS = {
    "request_clarification": "Request clarification from the requester before review",
    "manual_security_review": "Escalate to Security for manual review before any approval",
    "route_for_reviews": "Route to the required reviewers; no approval can be recorded yet",
    "route_for_approval": "Route for the standard approvals",
}


def assess(
    request: dict,
    employee: dict | None,
    budget_row: dict | None,
    catalog_matches: list[dict],
    registry_row: dict | None,
    api_status: str,
    api_record: dict | None,
    ref: date = config.REFERENCE_DATE,
) -> PolicyAssessment:
    a = PolicyAssessment()
    cost = to_decimal(request.get("annual_cost_usd"))
    users = request.get("user_count")
    data_class = norm(request.get("data_access_level"))
    integrations = request.get("requested_integrations")
    vendor = request.get("vendor_name")

    # ---- policy 9: untrusted content (checked first so the flag is present on every path)
    injected = detect_injection(
        request.get("business_justification"), request.get("product_name"), request.get("vendor_name"),
        registry_row.get("notes") if registry_row else None,
        api_record.get("notes") if api_record else None,
    )
    if injected:
        a.add_flag("prompt_injection_detected")
        a.findings.append((f"Instruction-like text found in business data and ignored: {injected}", "policy §9", "verified"))

    # ---- policy 1: required information. Never invent values.
    if employee is None:
        a.missing_information.append("Requester / department (requester_id not found in employee records)")
    if not norm(vendor) or not norm(request.get("product_name")):
        a.missing_information.append("Product / vendor name")
    if cost is None:
        a.missing_information.append("Annual cost or a reasonable annual estimate")
    elif cost < 0:
        a.missing_information.append("Annual cost (provided value is negative and cannot be used)")
    if not isinstance(users, int) or isinstance(users, bool) or users <= 0:
        a.missing_information.append("Number of users / licenses")
    if len(purpose_text_without_instructions(request.get("business_justification")).split()) < 5:
        a.missing_information.append("Business purpose (the request gives no usable justification)")
    if data_class in config.UNKNOWN_DATA_CLASSES:
        a.missing_information.append("Intended data-access level (value is missing or 'unknown')")
    elif data_class not in config.SECURITY_DATA_CLASSES | config.NON_SENSITIVE_DATA_CLASSES:
        a.missing_information.append(f"Intended data-access level is not a recognised class: '{request.get('data_access_level')}'")
    if integrations is None:
        a.missing_information.append("Required integrations (field absent; an empty list means none)")
    if a.missing_information:
        a.add_flag("missing_information")
        a.escalate("Mandatory request information is missing")

    # ---- policy 4: approval thresholds. Unknown cost means the tier is unknown, not "Manager".
    tier = approval_tier(cost)
    if tier is None:
        a.findings.append(("Approval tier cannot be determined because the annual cost is unknown or invalid", "policy §4", "missing"))
    else:
        for name in tier:
            a.add_approval(name)
        a.findings.append((f"Annual cost ${cost:,.2f} requires: {', '.join(tier)}", "policy §4", "verified"))
    a.facts["annual_cost"] = cost

    # ---- policy 2: budget
    available = to_decimal(budget_row.get("available_usd")) if budget_row else None
    bc = budget_check(cost, available)
    a.facts["budget"] = bc
    if budget_row is None:
        a.findings.append(("No budget record found for the requester's department", "department_budgets.csv", "missing"))
        a.escalate("Department budget could not be verified")
    elif not bc["checked"]:
        a.findings.append(("Budget not compared because the annual cost is unknown", "policy §2", "missing"))
    elif bc["within_budget"]:
        a.findings.append((f"Cost ${cost:,.2f} is within available budget ${available:,.2f}", "policy §2", "verified"))
    else:
        a.add_flag("budget_insufficient")
        a.add_approval("Finance")
        a.escalate(f"Cost exceeds available department budget by ${bc['shortfall']:,.2f}; Finance budget exception review needed")
        a.findings.append((f"Cost ${cost:,.2f} exceeds available budget ${available:,.2f} (shortfall ${bc['shortfall']:,.2f})", "policy §2", "verified"))

    # ---- policy 3: overlap with existing catalog. Surface it; never auto-reject.
    # Flag only when an existing tool covers the same capability: same category (any vendor), or the
    # same vendor in the same category. A same-vendor match in a different category (e.g. a training
    # pack for a tool we already license) is kept as evidence but is not an overlap.
    request_category = norm(request.get("category"))
    # An explicit seat/add-on extension of a tool we already license is not an alternative to it
    # (ENGINEERING ASSUMPTION, narrow on purpose; a vaguer "wants a tracker" request still flags).
    extension_text = f"{request.get('product_name') or ''} {request.get('business_justification') or ''}"
    is_extension = bool(re.search(r"add[- ]?on|\b(additional|extra|more) (seats?|licen[cs]es?|users?|identities)\b", extension_text, re.I))
    overlapping = [
        m for m in catalog_matches
        if m["match"] == "same_category"
        or (norm(m.get("category")) == request_category and not (is_extension and m["match"] == "same_vendor"))
    ]
    if overlapping:
        a.add_flag("existing_tool_overlap")
    if catalog_matches:
        for m in catalog_matches:
            kind = "same vendor" if m["match"] == "same_vendor" else "same category"
            a.findings.append((
                f"Existing catalog tool ({kind}): {m['product_name']} by {m['vendor_name']}, "
                f"{m['status']}, {m['licensed_seats']} seats, scope {m['scope']}",
                m["software_id"], "verified",
            ))
    a.facts["catalog_matches"] = catalog_matches

    # ---- policy 5: vendor security status
    view = vendor_view(registry_row, api_status, api_record, ref)
    a.facts["vendor_view"] = view
    if view["conflict"]:
        a.add_flag("conflicting_vendor_evidence")
        a.escalate("Vendor registry and vendor-risk service disagree: " + "; ".join(view["conflict_reasons"]))
    if view["expired"]:
        a.add_flag("vendor_review_expired")
    if view["unavailable"]:
        a.add_flag("vendor_risk_unavailable")
        a.escalate("Vendor-risk service did not return a usable record; status could not be verified")

    sensitive = data_class in config.SECURITY_DATA_CLASSES
    production = any(k in norm(i) for i in (integrations or []) for k in config.PRODUCTION_INTEGRATION_KEYWORDS)
    reasons = []
    if sensitive:
        reasons.append(f"data access level '{request.get('data_access_level')}'")
    if production:
        reasons.append("production / cloud-account integration")
    if not view["current"]:
        reasons.append("vendor security assessment is not shown to be current")
    if reasons:
        a.add_flag("security_review_required")
        a.add_approval("Security")
        a.findings.append(("Security review required: " + "; ".join(reasons), "policy §5", "verified"))
    elif view["unavailable"]:
        a.escalate("Risk service unavailable; internal registry shows a current review, which should be re-confirmed")

    # ---- policy 6: privacy
    outside_region = bool(api_record and api_record.get("stores_data_outside_region")) and api_status == "ok"
    if data_class in config.PRIVACY_DATA_CLASSES or (outside_region and sensitive):
        a.add_flag("privacy_review_required")
        a.add_approval("Privacy")
        why = "employee/customer PII" if data_class in config.PRIVACY_DATA_CLASSES else "sensitive data stored outside the operating region"
        a.findings.append((f"Privacy review required: {why}", "policy §6", "verified"))

    # ---- policy 7: legal
    new_vendor = (registry_row is None) or norm(registry_row.get("procurement_status")) == "new"
    legal_terms_ok = bool(registry_row) and norm(registry_row.get("legal_terms_status")) == "approved"
    legal_reasons = []
    if new_vendor and cost is not None and cost >= config.LEGAL_NEW_VENDOR_MIN_SPEND:
        legal_reasons.append(f"new vendor with annual spend ${cost:,.2f} >= ${config.LEGAL_NEW_VENDOR_MIN_SPEND:,.0f}")
    if not legal_terms_ok:
        legal_reasons.append(f"legal terms not approved/standard (status: {registry_row.get('legal_terms_status') if registry_row else 'vendor not in registry'})")
    if outside_region and sensitive:
        legal_reasons.append("cross-region storage of sensitive data")
    if legal_reasons:
        a.add_flag("legal_review_required")
        a.add_approval("Legal")
        a.findings.append(("Legal review required: " + "; ".join(legal_reasons), "policy §7", "verified"))
    a.facts["new_vendor"] = new_vendor

    # ---- policy 8: an existing limited-use AI approval does not cover a sensitive data class
    for m in catalog_matches:
        if m["match"] == "same_vendor" and "limited" in norm(m["status"]) and sensitive:
            a.escalate(f"{m['product_name']}'s approval covers limited use only; the requested data class '{request.get('data_access_level')}' is outside that approval (policy §8)")

    # ---- final ordering + the action label (the human always makes the decision: policy §11)
    a.approvals.sort(key=APPROVAL_ORDER.index)
    if a.missing_information:
        a.action = "request_clarification"
    elif view["conflict"] or view["unavailable"] or view["expired"]:
        a.action = "manual_security_review"
    elif {"Security", "Privacy", "Legal"} & set(a.approvals) or "budget_insufficient" in a.risk_flags:
        a.action = "route_for_reviews"
    else:
        a.action = "route_for_approval"
    return a
