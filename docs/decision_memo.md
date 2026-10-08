# Architecture decision memo

**Question:** which architecture would I ship, and why?

**Answer:** Architecture A, the single tool-calling agent.

## Evidence
Same data, tools, rules, guard and model (`gemini-3.5-flash-lite`), run 2026-10-08.

| | A single | B staged |
|---|---|---|
| 10 provided cases, 3 repeats | 30/30 | 30/30 |
| 15 held-out cases, 2 repeats | 30/30 | 28/30 |
| median latency, provided cases | 2.64 s | 4.83 s |
| LLM calls per request | 2 | 4 |
| tokens per request, in / out | 3541 / 223 | 4762 / 407 |
| outputs that changed across repeats | 0 of 10 | 0 of 10 |

Sources: `evals/results/gemini_20261008_135249.md` and `evals/results/holdout/gemini_20261008_140752.md`.

## Why A
1. **A is at least as accurate.** Both were perfect on the provided cases. On the held-out cases B twice called a same-vendor training pack an overlap; A got it right both times.
2. **A is cheaper and faster.** It makes 2 model calls per request instead of 4, uses fewer tokens, and has a median latency of 2.64 s against 4.83 s. A's mean (5.26 s) is higher than B's (5.0 s) only because of one request where the provider took 78.541 s to answer two calls; a 30 s timeout with retry now caps that.
3. **The second agent adds no control.** Approvals, thresholds, review expiry and data-class reviews are code that both architectures share, and so is the guard that stops the model from removing a flag or claiming approval. With no model at all, the rules alone pass every provided case. The second stage gives the same model a second pass over the same evidence, at twice the calls.
4. **The model's real value is available in A.** The one case the rules cannot catch, an injection worded to avoid the known phrases, was caught by the model in all four runs, two in each architecture. A keeps that benefit without the extra stage.

## What changed since the first comparison
On 3 October A passed 29/30 against B's 30/30, and the guard discarded more of A's rationales (5 against 2). Both gaps traced to the shared prompt: it allowed "approved" in rationales and never defined an instruction. After one shared fix both scored 30/30 and the guard discarded none. The gap was the prompt, not the architecture.

## Risks I accept, and what would change my mind
* 25 cases and one small free-tier model are not proof; I would rerun both on a larger set and a full Flash model first.
* Both architectures can over-flag overlap. That sends work to a reviewer but never removes a control.
* If a larger evaluation showed B catching a missed review that A misses, not just wording differences, B's extra cost would be worth paying and I would switch.

Until then the simpler system performs as well or better, so it is the one I would ship.
