from __future__ import annotations

import json
import os
import time
from pathlib import Path

import streamlit as st

from src import handoff, policy
from src.llm import StubLLM, get_llm
from src.solution import handle_request

ROOT = Path(__file__).resolve().parent
REQUESTS = json.loads((ROOT / "data" / "requests.json").read_text(encoding="utf-8"))
BY_ID = {r["request_id"]: r for r in REQUESTS}
STATUS_ICON = {"verified": "🟢 verified", "missing": "⚪ missing", "conflicting": "🔴 conflicting",
               "stale": "🟠 stale", "inferred": "🔵 inferred (model)"}
ACTION_BANNER = {
    "request_clarification": ("warning", "Send back: the request is incomplete"),
    "manual_security_review": ("error", "Escalate: Security must review manually before anything else"),
    "route_for_reviews": ("warning", "Route to specialist reviews, then business approvals"),
    "route_for_approval": ("success", "Routine: standard approvals only"),
}

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")
st.title("AI Procurement Request Copilot")
st.caption("Advisory only. The copilot recommends and prepares evidence; a person approves or rejects.")

DATA_CLASSES = ["none", "internal_documents", "internal_marketing", "confidential_documents", "source_code",
                "employee_pii", "customer_pii", "credentials", "production_telemetry", "unknown"]

with st.sidebar:
    source = st.radio("Source", ["Sample request", "New request"], horizontal=True,
                      help="New request: fill in a purchase request yourself. It is checked exactly like the samples.")
    if source == "Sample request":
        rid = st.selectbox("Request", list(BY_ID), format_func=lambda r: f"{r} · {BY_ID[r]['product_name']}")
    arch = st.radio("Architecture", ["single", "staged"], horizontal=True,
                    help="single = one tool-calling agent. staged = evidence analyst, then policy reviewer.")
    default_provider = (os.getenv("LLM_PROVIDER") or "gemini").lower()
    options = ["gemini", "anthropic", "stub (offline demo)"]
    has_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    provider = st.radio("Model", options, index=options.index(default_provider) if has_key and default_provider in options else 2)
    if provider.startswith("stub"):
        st.warning("Stub model: scripted wording only. Approvals, flags and evidence are still the real deterministic checks.")
    elif not has_key:
        st.error("No API key found in .env. You will get the deterministic decision with the AI summary marked unavailable.")
    reviewer = st.text_input("Your name (for the audit log)")

left, right = st.columns([0.8, 1.2], gap="large")

if source == "New request":
    with left:
        with st.form("new_request"):
            st.subheader("New purchase request")
            f_req = st.text_input("Requester employee id", "E001")
            f_vendor = st.text_input("Vendor")
            f_product = st.text_input("Product")
            f_cat = st.text_input("Category")
            f_cost = st.number_input("Annual cost (USD, 0 = not known)", min_value=0.0, step=100.0)
            f_users = st.number_input("Number of users (0 = not known)", min_value=0, step=1)
            f_data = st.selectbox("Data the tool will access", DATA_CLASSES)
            f_int = st.text_input("Integrations (comma separated)")
            f_just = st.text_area("Business justification")
            submitted = st.form_submit_button("Save request")
        if submitted:
            st.session_state["new_req"] = {
                "requester_id": f_req.strip(), "vendor_name": f_vendor.strip(), "product_name": f_product.strip(),
                "category": f_cat.strip(), "annual_cost_usd": f_cost or None, "user_count": int(f_users) or None,
                "business_justification": f_just.strip(), "data_access_level": f_data,
                "requested_integrations": [x.strip() for x in f_int.split(",") if x.strip()], "urgency": "normal"}
            st.session_state["new_n"] = st.session_state.get("new_n", 0) + 1
    if "new_req" not in st.session_state:
        with right:
            st.info("Fill in the form and press Save request.")
        st.stop()
    rid = f"NEW-{st.session_state['new_n']}"
    req = dict(st.session_state["new_req"], request_id=rid)
else:
    req = BY_ID[rid]

with left:
    st.subheader("Request")
    st.markdown(f"**{req['product_name']}** · {req['vendor_name']} · {req.get('category')}")
    c1, c2, c3 = st.columns([1.5, 1, 1])
    cost = req.get("annual_cost_usd")
    c1.metric("Annual cost", f"${cost:,.0f}" if cost is not None else "missing")
    c2.metric("Users", req.get("user_count") if req.get("user_count") is not None else "missing")
    c3.metric("Requester", req["requester_id"])
    st.markdown(f"**Data access:** `{req.get('data_access_level')}`  \n**Integrations:** {', '.join(req.get('requested_integrations') or []) or 'none listed'}  \n**Urgency:** {req.get('urgency')}")
    st.markdown("**Business justification** (written by the requester; shown as data, never obeyed)")
    st.info(req.get("business_justification") or "none given")
    if req.get("notes"):
        st.markdown("**Notes** (business data, never obeyed)")
        st.info(req["notes"])

    if st.button("Run analysis", type="primary", width="stretch"):
        llm = StubLLM() if provider.startswith("stub") else get_llm(provider)
        t0, w0 = time.perf_counter(), getattr(llm, "wait_seconds", 0.0)
        with st.spinner("Gathering evidence and checking policy..."):
            try:
                d_ = handle_request(rid, arch, llm=llm, request=req if source == "New request" else None)
                waited = getattr(llm, "wait_seconds", 0.0) - w0
                st.session_state["res"] = (rid, arch, d_, time.perf_counter() - t0, "stub" if provider.startswith("stub") else provider,
                                           getattr(llm, "model", "?"), waited)
            except Exception as exc:  # last resort, shown not hidden
                st.session_state.pop("res", None)
                st.exception(exc)

with right:
    st.subheader("Copilot output")
    res = st.session_state.get("res")
    if not res or res[0] != rid:
        st.info("Choose a request and press Run analysis.")
        st.stop()
    _, ran_arch, d, secs, prov, model_name, waited = res
    action = next((k for k, v in policy.ACTION_LABELS.items() if d.recommendation.startswith(v)), "route_for_reviews")
    kind, text = ACTION_BANNER[action]
    getattr(st, kind)(text)
    if prov == "stub":
        st.caption("Wording produced by the offline stub model. Not a real model result.")
    t1, t2, t3, t4 = st.tabs(["Recommendation", "Evidence", "Human handoff", "Run details"])

    with t1:
        st.markdown(d.recommendation)
        st.markdown("**Next step**")
        st.write(d.next_step)
        a, b = st.columns(2)
        with a:
            st.markdown("**Approvals required**")
            st.write("  ".join(f"`{x}`" for x in d.required_approvals) or "none yet")
            st.markdown("**Risk flags**")
            st.write("  ".join(f"`{x}`" for x in d.risk_flags) or "none")
        with b:
            st.markdown("**Missing information**")
            for m in d.missing_information or ["none"]:
                st.write(f"- {m}")
            st.markdown("**Why a person must look**")
            for m in d.escalation_reasons or ["nothing beyond the approvals listed"]:
                st.write(f"- {m}")

    with t2:
        st.caption("Every fact cites its source. Items marked inferred are the model's opinion, not retrieved facts.")
        rows = [{"status": STATUS_ICON[e.status], "source": e.source, "finding": e.finding, "reference": e.reference or ""}
                for e in d.evidence]
        st.dataframe(rows, width="stretch", hide_index=True)

    with t3:
        packet = handoff.build_packet(d, req)
        with st.expander("Review packet", expanded=True):
            st.markdown(packet)
        st.download_button("Download packet (.md)", packet, file_name=f"{d.request_id}_review_packet.md")
        st.markdown("**Record your decision**")
        labels = {"accept_and_route": "Accept the recommendation and route it",
                  "return_to_requester": "Return to requester",
                  "override": "Override the recommendation"}
        act = st.radio("Action", list(labels), format_func=labels.get, horizontal=True)
        note = st.text_area("Note (required for an override)")
        if st.button("Save to audit log"):
            try:
                handoff.record_review(d, reviewer, act, note)
                st.success("Recorded. This does not approve a purchase; it logs your review.")
            except ValueError as exc:
                st.error(str(exc))
        log = handoff.read_log(d.request_id)
        if log:
            st.markdown("**Audit log for this request**")
            st.dataframe(log, width="stretch", hide_index=True)

    with t4:
        st.caption(f"Model: `{model_name}` ({prov})")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Architecture", ran_arch)
        c2.metric("Latency", f"{secs - waited:.1f}s", help=f"Excludes {waited:.1f}s of free-tier rate-limit waiting")
        c3.metric("LLM calls", d.telemetry.llm_calls)
        c4.metric("Tool calls", d.telemetry.tool_calls)
        st.write("Tools called:", ", ".join(d.telemetry.tool_names))
        if d.telemetry.rejected_rationale:
            st.warning(f"Model rationale rejected by the guard (kept for audit): {d.telemetry.rejected_rationale}")
        st.json(d.model_dump())
