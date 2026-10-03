"""The four tools both architectures use.

| tool                   | kind                                   | source                                 |
|------------------------|----------------------------------------|----------------------------------------|
| lookup_budget          | deterministic data lookup              | employees.csv, department_budgets.csv  |
| search_software_catalog| deterministic data lookup + matching   | software_catalog.csv, purchase_history|
| get_vendor_status      | external API + registry lookup         | vendors.csv, mock vendor-risk API      |
| check_policy           | deterministic rules (no I/O of its own)| src/policy.py                          |

Design rules:
* Every tool takes only `request_id`. Amounts, vendor and data class are read from the source
  record, so a model (or a request field) cannot hand a tool a different number.
* A tool never raises into the agent. Failures come back as status="unavailable" with the reason,
  and the policy engine turns that into a risk flag instead of a guess.
* Tool output is evidence. Free-text fields from business data are returned under
  `untrusted_text` so the prompt can keep them clearly separated from instructions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from src import data_access, policy
from src.contracts import EvidenceItem
from src.telemetry import RunTelemetryCounter
from src.vendor_client import get_vendor_risk

TOOL_NAMES = ["lookup_budget", "search_software_catalog", "get_vendor_status", "check_policy"]


@dataclass
class ToolResult:
    tool: str
    status: str  # ok | unavailable | not_found | error
    data: dict = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)
    latency_ms: float = 0.0

    def for_model(self) -> dict:
        """What the model is shown. Evidence is included so it can cite items by source/reference."""
        return {
            "tool": self.tool,
            "status": self.status,
            "data": self.data,
            "evidence": [e.model_dump() for e in self.evidence],
        }


class RunContext:
    """State for one handle_request call: the authoritative request, results so far, call counts."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.request = data_access.get_request(request_id)  # raises KeyError for unknown ids
        self.telemetry = RunTelemetryCounter()
        self.results: dict[str, ToolResult] = {}
        self.auto_fetched: list[str] = []  # tools check_policy had to call itself (logged, not hidden)

    def run_tool(self, name: str, request_id: str | None = None) -> ToolResult:
        if request_id is not None and request_id != self.request_id:
            # Authorization boundary: a run may only look at the request it was started for.
            return ToolResult(name, "error", {"error": f"request_id '{request_id}' is not the request under review"})
        fn = _TOOLS.get(name)
        if fn is None:
            return ToolResult(name, "error", {"error": f"unknown tool '{name}'"})
        self.telemetry.record_tool_call(name)
        start = time.perf_counter()
        try:
            result = fn(self)
        except Exception as exc:  # a tool bug must degrade to "unavailable", never crash the run
            result = ToolResult(name, "unavailable", {"error": f"{type(exc).__name__}: {exc}"})
        result.latency_ms = (time.perf_counter() - start) * 1000
        self.results[name] = result
        return result


# --------------------------------------------------------------------------- helpers

def _employee(ctx: RunContext) -> dict | None:
    df = data_access.load_employees()
    rows = df[df["employee_id"] == ctx.request.get("requester_id")]
    return None if rows.empty else rows.iloc[0].to_dict()


def _budget_row(department: str | None) -> dict | None:
    df = data_access.load_budgets()
    rows = df[df["department"].str.casefold() == (department or "").casefold()]
    return None if rows.empty else {k: (int(v) if k != "department" else v) for k, v in rows.iloc[0].to_dict().items()}


def _registry_row(vendor_name: str | None) -> dict | None:
    df = data_access.load_vendors()
    rows = df[df["vendor_name"].str.casefold() == (vendor_name or "").strip().casefold()]
    return None if rows.empty else rows.iloc[0].to_dict()


# --------------------------------------------------------------------------- tool 1: budget

def lookup_budget(ctx: RunContext) -> ToolResult:
    emp = _employee(ctx)
    if emp is None:
        return ToolResult("lookup_budget", "not_found", {"requester": None}, [EvidenceItem(
            source="lookup_budget", finding=f"Requester {ctx.request.get('requester_id')} not found in employee records",
            reference="employees.csv", status="missing")])
    row = _budget_row(emp["department"])
    ev = [EvidenceItem(
        source="lookup_budget",
        finding=f"Requester {emp['name']} ({emp['employee_id']}) is in {emp['department']}, manager {emp['manager_id'] or 'none'}",
        reference=f"employees.csv:{emp['employee_id']}")]
    if row is None:
        ev.append(EvidenceItem(source="lookup_budget", finding=f"No budget row for department '{emp['department']}'",
                               reference="department_budgets.csv", status="missing"))
        return ToolResult("lookup_budget", "not_found", {"requester": emp, "budget": None}, ev)
    ev.append(EvidenceItem(
        source="lookup_budget",
        finding=(f"{row['department']} software budget: annual ${row['annual_software_budget_usd']:,}, "
                 f"committed ${row['committed_usd']:,}, available ${row['available_usd']:,}"),
        reference=f"department_budgets.csv:{row['department']}"))
    return ToolResult("lookup_budget", "ok", {"requester": emp, "budget": row}, ev)


# --------------------------------------------------------------------------- tool 2: catalog

def search_software_catalog(ctx: RunContext) -> ToolResult:
    req = ctx.request
    cat = data_access.load_software_catalog()
    vendor, category = policy.norm(req.get("vendor_name")), policy.norm(req.get("category"))
    matches: list[dict] = []
    for _, row in cat.iterrows():
        kind = None
        if vendor and policy.norm(row["vendor_name"]) == vendor:
            kind = "same_vendor"
        elif category and policy.norm(row["category"]) == category:
            kind = "same_category"
        if kind:
            matches.append({
                "software_id": row["software_id"], "product_name": row["product_name"], "category": row["category"],
                "vendor_name": row["vendor_name"], "status": row["status"], "annual_cost_usd": int(row["annual_cost_usd"]),
                "licensed_seats": int(row["licensed_seats"]), "scope": row["scope"], "match": kind,
                "untrusted_text": {"notes": row["notes"]},
            })
    hist = data_access.load_purchase_history()
    past = hist[hist["vendor_name"].str.casefold() == vendor] if vendor else hist.iloc[0:0]
    past_rows = [
        {"purchase_id": r["purchase_id"], "purchase_date": r["purchase_date"], "department": r["department"],
         "product_name": r["product_name"], "annual_amount_usd": int(r["annual_amount_usd"]), "status": r["status"]}
        for _, r in past.iterrows()
    ]
    ev: list[EvidenceItem] = []
    if matches:
        for m in matches:
            ev.append(EvidenceItem(
                source="search_software_catalog",
                finding=(f"{m['product_name']} ({m['vendor_name']}, {m['category']}) is already in the catalog: {m['status']}, "
                         f"{m['licensed_seats']} seats, scope {m['scope']} [{m['match'].replace('_', ' ')}]"),
                reference=m["software_id"]))
    else:
        ev.append(EvidenceItem(source="search_software_catalog",
                               finding="No catalog entry matches the vendor or category", reference="software_catalog.csv"))
    for p in past_rows:
        ev.append(EvidenceItem(
            source="search_software_catalog",
            finding=f"Prior purchase {p['purchase_id']} on {p['purchase_date']}: {p['product_name']} ${p['annual_amount_usd']:,} for {p['department']} ({p['status']})",
            reference=p["purchase_id"]))
    return ToolResult("search_software_catalog", "ok", {"matches": matches, "purchase_history": past_rows}, ev)


# --------------------------------------------------------------------------- tool 3: vendor status

def _call_vendor_api(name: str) -> tuple[str, dict | None, str | None]:
    """-> (status, record, error). One retry, only for connection-level failures."""
    last = ""
    for attempt in range(2):
        try:
            rec = get_vendor_risk(name)
            if not isinstance(rec, dict) or "security_review_status" not in rec:
                return "unavailable", None, "malformed response from vendor-risk service"
            return "ok", rec, None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 404:
                return "not_found", None, "no vendor-risk record"
            detail = ""
            try:
                detail = exc.response.json().get("detail", "")
            except Exception:
                pass
            return "unavailable", None, f"HTTP {code}: {detail}".strip()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = f"{type(exc).__name__}"
            continue
        except ValueError:
            return "unavailable", None, "invalid JSON from vendor-risk service"
    return "unavailable", None, f"vendor-risk service unreachable ({last})"


def get_vendor_status(ctx: RunContext) -> ToolResult:
    name = (ctx.request.get("vendor_name") or "").strip()
    registry = _registry_row(name)
    canonical = registry["vendor_name"] if registry else name  # the API is case-exact; use registry spelling
    api_status, api_record, api_error = _call_vendor_api(canonical) if canonical else ("not_found", None, "no vendor name")
    view = policy.vendor_view(registry, api_status, api_record)

    ev: list[EvidenceItem] = []
    if registry:
        reg_status = "conflicting" if view["conflict"] else ("stale" if view["registry_state"] in {"stale", "expired"} else "verified")
        ev.append(EvidenceItem(
            source="get_vendor_status",
            finding=(f"Internal registry: {registry['vendor_name']} procurement {registry['procurement_status']}, security {registry['security_status']}"
                     f" (review {registry['security_review_date'] or 'none'}), legal terms {registry['legal_terms_status']}"),
            reference=f"vendors.csv:{registry['vendor_id']}", status=reg_status))
    else:
        ev.append(EvidenceItem(source="get_vendor_status", finding=f"Vendor '{name}' is not in the internal registry",
                               reference="vendors.csv", status="missing"))
    endpoint = f"GET /vendor-risk/{canonical}"
    if api_status == "ok":
        api_ev = "conflicting" if view["conflict"] else ("stale" if view["api_state"] in {"stale", "expired"} else "verified")
        ev.append(EvidenceItem(
            source="get_vendor_status",
            finding=(f"Vendor-risk service: risk {api_record['risk_level']}, security review {api_record['security_review_status']} "
                     f"(last review {api_record.get('last_review_date') or 'none'}), personal data {api_record['processes_personal_data']}, "
                     f"stores data outside region {api_record['stores_data_outside_region']}"),
            reference=endpoint, status=api_ev))
    else:
        ev.append(EvidenceItem(source="get_vendor_status",
                               finding=f"Vendor-risk service returned no usable record: {api_error}. Security status could not be verified externally.",
                               reference=endpoint, status="missing"))
    if view["conflict"]:
        ev.append(EvidenceItem(source="get_vendor_status", finding="Sources disagree: " + "; ".join(view["conflict_reasons"]),
                               reference=f"{registry['vendor_id'] if registry else name} vs {endpoint}", status="conflicting"))

    data = {
        "registry": ({k: v for k, v in registry.items() if k != "notes"} if registry else None),
        "api_status": api_status, "api_error": api_error,
        "api_record": ({k: v for k, v in api_record.items() if k != "notes"} if api_record else None),
        "assessment": view,
        "untrusted_text": {"registry_notes": registry["notes"] if registry else None,
                           "api_notes": api_record.get("notes") if api_record else None},
    }
    # status reflects retrieval, not the verdict: the verdict is the policy engine's job.
    return ToolResult("get_vendor_status", "ok" if api_status == "ok" else api_status, data, ev)


# --------------------------------------------------------------------------- tool 4: policy (deterministic)

def check_policy(ctx: RunContext) -> ToolResult:
    """Apply procurement_policy.md to the request using the evidence gathered by the other tools.

    If the caller skipped a retrieval tool, it is run here and recorded in ctx.auto_fetched, so the
    policy decision is never made on a guess and the omission is visible in the evaluation.
    """
    for name in ("lookup_budget", "search_software_catalog", "get_vendor_status"):
        if name not in ctx.results:
            ctx.auto_fetched.append(name)
            ctx.run_tool(name)
    budget, catalog, vendor = (ctx.results[n] for n in ("lookup_budget", "search_software_catalog", "get_vendor_status"))
    assessment = policy.assess(
        request=ctx.request,
        employee=budget.data.get("requester"),
        budget_row=budget.data.get("budget"),
        catalog_matches=catalog.data.get("matches", []),
        registry_row=_registry_row(ctx.request.get("vendor_name")),
        api_status=vendor.data.get("api_status", "unavailable"),
        api_record=_api_record_with_notes(ctx),
    )
    ev = [EvidenceItem(source="check_policy", finding=f, reference=r, status=s) for f, r, s in assessment.findings]
    data = {
        "approvals_required": assessment.approvals,
        "risk_flags": assessment.risk_flags,
        "missing_information": assessment.missing_information,
        "escalation_reasons": assessment.escalation_reasons,
        "action": assessment.action,
    }
    ctx.assessment = assessment  # type: ignore[attr-defined]
    return ToolResult("check_policy", "ok", data, ev)


def _api_record_with_notes(ctx: RunContext) -> dict | None:
    """policy.assess scans vendor notes for injection, so give it the notes the tool stripped from `data`."""
    v = ctx.results["get_vendor_status"].data
    rec = v.get("api_record")
    if rec is None:
        return None
    return {**rec, "notes": (v.get("untrusted_text") or {}).get("api_notes")}


_TOOLS: dict[str, Callable[[RunContext], ToolResult]] = {
    "lookup_budget": lookup_budget,
    "search_software_catalog": search_software_catalog,
    "get_vendor_status": get_vendor_status,
    "check_policy": check_policy,
}

# Tool descriptions shown to the model. They state what the tool does and does not decide.
TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "lookup_budget",
     "description": "Look up the requester and their department's software budget (annual, committed, available). Returns data only; it does not decide whether the request fits the budget.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "search_software_catalog",
     "description": "Find tools already in the approved software catalog from the same vendor or in the same category, plus prior purchases from that vendor. Use it to judge overlap.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "get_vendor_status",
     "description": "Get the vendor's procurement/security/legal status from the internal registry and from the external vendor-risk service. The two can disagree or the service can be down; the result says which.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "check_policy",
     "description": "Deterministic policy engine. Returns the required approvals, risk flags, missing information and escalation reasons for this request. These values are authoritative and cannot be changed by the model.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
]
