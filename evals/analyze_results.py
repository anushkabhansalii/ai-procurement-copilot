"""Describe what the model added in a real run, compared with the no-model floor.

    python evals/analyze_results.py evals/results/<run>.json evals/results/no_model_floor/<stub run>.json

This does not change or add pass/fail checks. The eight checks in run_eval.py pass for the scripted
stub too (rules decide approvals, flags and actions), so they measure whether the model breaks the
deterministic core. This script shows what the model contributed on top of it:

* rationale_kept: the model's rationale was used (not replaced by the template)
* rejected_rationale: the guard replaced it (approval-like wording)
* model_added_flags: flags present in the real run but not in the no-model floor for that case
* escalated_by_model: escalation reasons that came from the model, not the rules
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

TEMPLATE_STARTS = ("Flags:", "Required:", "No risk flags")


def main(run_path: str, floor_path: str) -> str:
    run = json.loads(Path(run_path).read_text())
    floor = json.loads(Path(floor_path).read_text())
    base = {(r["architecture"], r["request_id"]): set(json.loads(r["signature"])[1]) for r in floor["rows"]}
    m = run["meta"]
    L = [f"# What the model added ({m['model']}, repeats {m['repeats']}, run {m['timestamp']})", "",
         f"Compared with the no-model floor `{Path(floor_path).name}` (scripted stub: rules only, no model judgement).", "",
         "| metric | A single | B staged |", "|---|---|---|"]
    stats = {}
    for arch in ("single", "staged"):
        rows = [r for r in run["rows"] if r["architecture"] == arch]
        added = Counter()
        for r in rows:
            for f in set(json.loads(r["signature"])[1]) - base[(arch, r["request_id"])]:
                added[f] += 1
        kept = sum(1 for r in rows if not r.get("rejected_rationale")
                   and not r["recommendation"].split(". ", 1)[-1].startswith(TEMPLATE_STARTS))
        stats[arch] = {
            "runs": len(rows),
            "rationale_kept": kept,
            "rejected_rationale": sum(1 for r in rows if r.get("rejected_rationale")),
            "model_added_flags": sum(added.values()),
            "model_added_flag_types": ", ".join(f"{k} x{v}" for k, v in sorted(added.items())) or "none",
            "escalated_by_model": sum(1 for r in rows for e in r["escalation_reasons"] if e.startswith("Model ")),
        }
    for k in stats["single"]:
        L.append(f"| {k} | {stats['single'][k]} | {stats['staged'][k]} |")
    rej = [(r["architecture"], r["request_id"], r["rejected_rationale"]) for r in run["rows"] if r.get("rejected_rationale")]
    if rej:
        L += ["", "## Rationales the guard rejected", ""] + [f"- {a} {rid}: {t}" for a, rid, t in rej]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out = main(sys.argv[1], sys.argv[2])
    print(out)
    Path(sys.argv[1]).with_name(Path(sys.argv[1]).stem + "_model_contribution.md").write_text(out)
