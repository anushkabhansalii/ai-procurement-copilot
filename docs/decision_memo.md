# Architecture decision memo

**Question:** which architecture would I ship, and why?

**Answer:** Architecture A, the single tool-calling agent.

## Evidence
Same 10 cases, 3 repeats, same data, tools, rules and guard, on `gemini-3.5-flash-lite` (`evals/results/gemini_20261003_135950.md`).

| | A single | B staged |
|---|---|---|
| all checks pass | 29/30 | 30/30 |
| mean latency | 2.66 s | 4.92 s |
| max latency | 3.289 s | 7.255 s |
| LLM calls per request | 2 | 4.03 |
| tokens per request (in / out) | 3261 / 227 | 4556 / 440 |
| cases with output that changed across repeats | 1 | 0 |

Token counts come from a separate 1-repeat run (`evals/results/token_cost_run/`).

## Why A
1. **Quality is close, and not separated by this test set.** B passed one more run than A; with 30 runs each, one failure cannot separate them.
2. **A's one failure failed safe.** In REQ-1002 the model marked a harmless justification as a prompt injection. That added a flag and an escalation, so the request would get more human attention, not less. Approvals, the action and every required flag were still correct. No run in either architecture missed a required review, granted an approval, or claimed approval.
3. **A is faster and cheaper.** B makes 4.03 model calls per request to A's 2, sends more tokens, and takes 4.92 s on average against 2.66 s.
4. **The rules carry the safety, not the orchestration.** The checks also pass with no model at all (`evals/results/no_model_floor/`). Approvals, thresholds, expiry dates and data-class reviews are code in both architectures. Splitting the model into two stages does not add a control that A lacks; it adds a second place for the same model to read the same evidence.

## What B does better
B's wording was cleaner. The guard replaced 2 of 30 B rationales against 5 of 30 for A, and B added 2 model escalations against 6 for A (`evals/results/gemini_20261003_135950_model_contribution.md`). That is a real benefit for reviewers reading the text, but it is a writing benefit, bought with 4.03 calls per request instead of 2.

## Risks I accept, and what would change my mind
* Ten cases are a small set, and this is one small free-tier model. I would rerun both architectures on a larger set and a full Flash model before production.
* A's false injection flag will recur sometimes. It sends extra work to a reviewer, but it never removes a control. If the rate of false flags on real traffic became a burden for reviewers, I would first tighten A's injection prompt.
* If a larger evaluation showed B catching problems that A misses (a missed review, not just better wording), B's extra cost would be worth paying and I would switch.

Until then, the simpler system is the one I would ship.
