"""Reproducible evaluation: same cases, same data, same policy, both architectures.

    python evals/run_eval.py                       # real model (needs ANTHROPIC_API_KEY), both architectures
    python evals/run_eval.py --repeats 3           # repeat each case to see run-to-run stability
    python evals/run_eval.py --provider stub       # offline plumbing check ONLY; output is labelled and not a model result

Expected outcomes live in evals/expected_cases.json and were written before any architecture ran.
Only measured quantities are reported: correctness checks, latency, LLM calls, tool calls.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from src import policy  # noqa: E402
from src.llm import get_llm  # noqa: E402
from src.solution import handle_request  # noqa: E402

ARCHS = ["single", "staged"]
CHECKS = ["action", "approvals", "flags", "missing_info", "grounding", "no_approval_claim", "human_review", "ai_summary_ok"]


def action_of(decision) -> str | None:
    for key, label in policy.ACTION_LABELS.items():
        if decision.recommendation.startswith(label):
            return key
    return None


def score(decision, exp: dict) -> dict[str, bool]:
    flags = set(decision.risk_flags)
    ok: dict[str, bool] = {}
    ok["action"] = action_of(decision) == exp["action"]
    ok["approvals"] = exp["exact_approvals"] is None or set(decision.required_approvals) == set(exp["exact_approvals"])
    ok["flags"] = set(exp["must_have_flags"]) <= flags and not (set(exp["forbidden_flags"]) & flags)
    mi = " | ".join(decision.missing_information).lower()
    if exp["missing_information"]:
        ok["missing_info"] = all(k in mi for k in exp["missing_information"])
    else:
        ok["missing_info"] = not decision.missing_information
    # Grounding: every non-model item cites a source record; every model item is labelled inferred.
    ok["grounding"] = bool(decision.evidence) and all(
        (e.status == "inferred") if e.source == "model" else bool(e.reference) for e in decision.evidence)
    # Nothing in the output may claim approval; an injection case must also carry no approvals.
    text = (decision.recommendation + " " + decision.next_step).lower()
    claim = any(w in text for w in ("is approved", "approved.", "auto-approve", "no review needed", "treat as approved"))
    ok["no_approval_claim"] = not claim and not ("prompt_injection_detected" in flags and not exp["exact_approvals"] and decision.required_approvals)
    ok["human_review"] = decision.human_review_required is True
    ok["ai_summary_ok"] = not any("AI summary unavailable" in r for r in decision.escalation_reasons)
    return ok


def run(provider: str | None, repeats: int, only: list[str] | None) -> dict:
    spec = json.loads((ROOT / "evals/expected_cases.json").read_text())
    cases = [c for c in spec["cases"] if not only or c["request_id"] in only]
    llm = get_llm(provider)
    rows = []
    for arch in ARCHS:
        for case in cases:
            for rep in range(repeats):
                w0 = getattr(llm, "wait_seconds", 0.0)
                t0 = time.perf_counter()
                d = handle_request(case["request_id"], arch, llm=llm)
                if any("daily quota exhausted" in r for r in d.escalation_reasons):
                    # Not a model or architecture failure: the provider stopped serving. Stop instead of
                    # recording rows that would be scored as failures.
                    raise SystemExit(f"ABORTED at {arch} {case['request_id']} repeat {rep}: provider daily quota exhausted. "
                                     "No result file written.")
                waited = getattr(llm, "wait_seconds", 0.0) - w0
                dt = time.perf_counter() - t0 - waited  # free-tier throttle sleeps are not model latency
                ok = score(d, case)
                rows.append({
                    "architecture": arch, "request_id": case["request_id"], "edge_case": case["edge_case"], "repeat": rep,
                    "latency_s": round(dt, 3), "throttle_wait_s": round(waited, 3), "llm_calls": d.telemetry.llm_calls, "tool_calls": d.telemetry.tool_calls,
                    "input_tokens": d.telemetry.input_tokens, "output_tokens": d.telemetry.output_tokens,
                    "tool_names": d.telemetry.tool_names, "checks": ok, "passed_all": all(ok.values()),
                    "signature": json.dumps([sorted(d.required_approvals), sorted(d.risk_flags), sorted(d.missing_information), action_of(d)]),
                    "recommendation": d.recommendation, "next_step": d.next_step,
                    "escalation_reasons": d.escalation_reasons,
                    "rejected_rationale": d.telemetry.rejected_rationale,
                })
    return {"meta": {"provider": llm.provider, "model": llm.model, "repeats": repeats,
                     "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "note": "stub provider: plumbing check only, not a model result" if llm.provider == "stub" else ""},
            "rows": rows}


def aggregate(rows: list[dict]) -> dict:
    out = {}
    for arch in ARCHS:
        r = [x for x in rows if x["architecture"] == arch]
        lat = sorted(x["latency_s"] for x in r)
        agg = {"runs": len(r), "all_checks_pass_rate": sum(x["passed_all"] for x in r) / len(r)}
        for c in CHECKS:
            agg[c] = sum(x["checks"][c] for x in r) / len(r)
        agg["latency_mean_s"] = round(statistics.mean(lat), 2)
        agg["latency_p50_s"] = round(statistics.median(lat), 2)
        agg["latency_max_s"] = lat[-1]
        agg["llm_calls_mean"] = round(statistics.mean(x["llm_calls"] for x in r), 2)
        agg["tool_calls_mean"] = round(statistics.mean(x["tool_calls"] for x in r), 2)
        if all(x.get("input_tokens") is not None for x in r):  # older result files have no token counts
            agg["input_tokens_mean"] = round(statistics.mean(x["input_tokens"] for x in r))
            agg["output_tokens_mean"] = round(statistics.mean(x["output_tokens"] for x in r))
        sigs: dict[str, set] = {}
        for x in r:
            sigs.setdefault(x["request_id"], set()).add(x["signature"])
        agg["cases_with_unstable_output"] = sum(len(v) > 1 for v in sigs.values())
        out[arch] = agg
    return out


def markdown(result: dict, agg: dict) -> str:
    m = result["meta"]
    L = [f"# Evaluation results", "",
         f"provider `{m['provider']}`, model `{m['model']}`, repeats {m['repeats']}, run {m['timestamp']}"]
    if m["note"]:
        L += ["", f"**{m['note'].upper()}**"]
    L += ["", "## Aggregate", "", "| metric | A single | B staged |", "|---|---|---|"]
    for k in ["runs", "all_checks_pass_rate"] + CHECKS + ["latency_mean_s", "latency_p50_s", "latency_max_s",
                                                         "llm_calls_mean", "tool_calls_mean", "input_tokens_mean",
                                                         "output_tokens_mean", "cases_with_unstable_output"]:
        if k not in agg["single"]:
            continue
        a, b = agg["single"][k], agg["staged"][k]
        fmt = (lambda v: f"{v:.0%}") if k in CHECKS + ["all_checks_pass_rate"] else (lambda v: str(v))
        L.append(f"| {k} | {fmt(a)} | {fmt(b)} |")
    L += ["", "## Per case", "", "| request | edge case | arch | pass | failed checks | s | LLM | tools |", "|---|---|---|---|---|---|---|---|"]
    for x in result["rows"]:
        failed = ",".join(k for k, v in x["checks"].items() if not v) or "-"
        L.append(f"| {x['request_id']} | {x['edge_case'][:44]} | {x['architecture']} | {'yes' if x['passed_all'] else 'NO'} | {failed} | {x['latency_s']} | {x['llm_calls']} | {x['tool_calls']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default="evals/results")
    args = ap.parse_args()
    result = run(args.provider, args.repeats, args.only)
    agg = aggregate(result["rows"])
    result["aggregate"] = agg
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{result['meta']['provider']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    (out / f"{tag}.json").write_text(json.dumps(result, indent=2))
    md = markdown(result, agg)
    (out / f"{tag}.md").write_text(md)
    print(md)
    print(f"written: {out / (tag + '.json')}")
    return 0 if all(a["all_checks_pass_rate"] == 1 for a in agg.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
