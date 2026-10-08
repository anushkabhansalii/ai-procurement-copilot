"""Held-out evaluation: new request records the system was not built against.

    python evals/run_holdout.py --repeats 2                 # real model, both architectures
    python evals/run_holdout.py --provider stub              # rules only (no model): the floor

Cases live in evals/holdout_cases.json and were committed before the first run. Each case carries its
own request record, passed to handle_request(..., request=...), so data/requests.json is unchanged.
Checks match run_eval.py, except that approvals are must_include / must_exclude and the action may be
any of the actions the policy allows.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from run_eval import action_of, score as base_score  # noqa: E402
from src.llm import get_llm  # noqa: E402
from src.solution import handle_request  # noqa: E402

ARCHS = ["single", "staged"]
CHECKS = ["action", "approvals", "flags", "missing_info", "grounding", "no_approval_claim", "human_review", "ai_summary_ok"]


def score(d, case: dict) -> dict[str, bool]:
    # Shared checks (grounding, approval claim, human review, AI summary, missing info, flags) come from run_eval.
    # exact_approvals is passed as must_include so run_eval's "injection case carries no approvals" rule
    # only fires where this case expects no approvals (policy 9 keeps the real approvals otherwise).
    shared = base_score(d, {"action": None, "exact_approvals": case["must_include_approvals"], "must_have_flags": case["must_have_flags"],
                            "forbidden_flags": case["forbidden_flags"], "missing_information": case["missing_information"]})
    approvals = set(d.required_approvals)
    shared["action"] = action_of(d) in case["actions"]
    shared["approvals"] = set(case["must_include_approvals"]) <= approvals and not (set(case["must_exclude_approvals"]) & approvals)
    if case["actions"] == ["request_clarification"]:
        shared["approvals"] = shared["approvals"] and not approvals  # nothing to approve until the request is complete
    return shared


def run(provider: str | None, repeats: int) -> dict:
    cases = json.loads((ROOT / "evals/holdout_cases.json").read_text())["cases"]
    llm = get_llm(provider)
    rows = []
    for arch in ARCHS:
        for case in cases:
            for rep in range(repeats):
                w0 = getattr(llm, "wait_seconds", 0.0)
                t0 = time.perf_counter()
                d = handle_request(case["id"], arch, llm=llm, request=case["request"])
                waited = getattr(llm, "wait_seconds", 0.0) - w0
                if any("daily quota exhausted" in r for r in d.escalation_reasons):
                    raise SystemExit(f"ABORTED at {arch} {case['id']}: provider daily quota exhausted. No result file written.")
                ok = score(d, case)
                rows.append({
                    "architecture": arch, "case": case["id"], "edge_case": case["edge_case"], "repeat": rep,
                    "latency_s": round(time.perf_counter() - t0 - waited, 3), "throttle_wait_s": round(waited, 3),
                    "llm_calls": d.telemetry.llm_calls, "tool_calls": d.telemetry.tool_calls,
                    "input_tokens": d.telemetry.input_tokens, "output_tokens": d.telemetry.output_tokens,
                    "checks": ok, "passed_all": all(ok.values()), "action": action_of(d),
                    "approvals": d.required_approvals, "flags": d.risk_flags, "missing_information": d.missing_information,
                    "recommendation": d.recommendation, "escalation_reasons": d.escalation_reasons,
                    "rejected_rationale": d.telemetry.rejected_rationale,
                })
    return {"meta": {"provider": llm.provider, "model": llm.model, "repeats": repeats, "cases": len(cases),
                     "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "note": "stub provider: rules only, not a model result" if llm.provider == "stub" else ""},
            "rows": rows}


def markdown(res: dict) -> str:
    m, rows = res["meta"], res["rows"]
    L = ["# Held-out evaluation", "", f"provider `{m['provider']}`, model `{m['model']}`, {m['cases']} cases, repeats {m['repeats']}, run {m['timestamp']}"]
    if m["note"]:
        L += ["", f"**{m['note'].upper()}**"]
    L += ["", "| metric | A single | B staged |", "|---|---|---|"]
    by = {a: [r for r in rows if r["architecture"] == a] for a in ARCHS}
    L.append(f"| all checks pass | {sum(r['passed_all'] for r in by['single'])}/{len(by['single'])} | {sum(r['passed_all'] for r in by['staged'])}/{len(by['staged'])} |")
    for c in CHECKS:
        L.append(f"| {c} | {sum(r['checks'][c] for r in by['single'])}/{len(by['single'])} | {sum(r['checks'][c] for r in by['staged'])}/{len(by['staged'])} |")
    for k in ("latency_s", "llm_calls"):
        L.append(f"| {k} mean | {round(statistics.mean(r[k] for r in by['single']), 2)} | {round(statistics.mean(r[k] for r in by['staged']), 2)} |")
    L += ["", "| case | edge case | arch | rep | pass | failed checks | action | approvals | flags |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        failed = ",".join(k for k, v in r["checks"].items() if not v) or "-"
        L.append(f"| {r['case']} | {r['edge_case'][:50]} | {r['architecture']} | {r['repeat']} | {'yes' if r['passed_all'] else 'NO'} | {failed} | {r['action']} | {', '.join(r['approvals'])} | {', '.join(r['flags'])} |")
    return "\n".join(L) + "\n"


def main() -> int:
    sys.path.insert(0, str(ROOT / "evals"))
    import _mock_api
    _mock_api.ensure()
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="evals/results/holdout")
    a = ap.parse_args()
    res = run(a.provider, a.repeats)
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{res['meta']['provider']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    (out / f"{tag}.json").write_text(json.dumps(res, indent=2))
    md = markdown(res)
    (out / f"{tag}.md").write_text(md)
    print(md)
    print(f"written: {out / (tag + '.json')}")
    return 0 if all(r["passed_all"] for r in res["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
