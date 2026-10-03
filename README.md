# AI Procurement Request Copilot (FDE Assessment 3)

A copilot that reads a software purchase request, gathers evidence with tools, and hands a human reviewer a recommendation, evidence, required approvals, missing information, risk flags and a next step. It never approves anything.

## 1. Problem
Procurement reviewers re-check the same things by hand: budget, existing tools, vendor security status, approval tier. The risk is not slowness, it is a missed review (sensitive data, expired vendor review, a request that tries to talk the process into approving it).

## 2. Users and workflow
Requester submits; the copilot prepares a packet; a reviewer (Manager, Department Head, Procurement, Finance, Security, Privacy, Legal, CFO) decides. The reviewer records Accept / Return / Override with a reason in an audit log.

## 3. Quick start (one command)
```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env        # add GEMINI_API_KEY (free tier works; see docs/model_selection.md) or ANTHROPIC_API_KEY
python run_local.py         # mock vendor API on :8001, UI on :8501
```
![UI, REQ-1007 with gemini-3.5-flash-lite](docs/ui_screenshot_REQ-1007.jpg)

No key? The UI has an "offline stub" model option, and the decision still shows all deterministic checks. Stub wording is labelled and is never used for reported results.

## 4. Architecture
See [docs/architecture.md](docs/architecture.md) for diagrams. Four tools, shared by both architectures:
`lookup_budget`, `search_software_catalog`, `get_vendor_status` (internal registry plus HTTP risk API), `check_policy` (deterministic). Three of the four are deterministic; only the vendor API is an external call.

## 5. Architecture A: single agent
One tool-calling agent, up to 8 turns, then a shared guard layer.

## 6. Architecture B: staged
Agent 1 (evidence analyst, retrieval tools) hands notes to Agent 2 (policy reviewer, `check_policy`), then the same guard. Two agents, the maximum allowed.

## 7. What the model is and is not allowed to decide
Rules (tiers, budget, 365-day expiry, data-class triggers, missing fields, action) are code. The model judges overlap and instruction-like text, writes the rationale, and can only add flags and `inferred` evidence. See the table in docs/architecture.md.

## 8. Evidence model
Each evidence item has `source`, `finding`, `reference` and a `status`: verified, missing, conflicting, stale, inferred.

## 9. Human handoff
`src/handoff.py`: a review packet (markdown download) and an append-only audit log (`data/audit_log.jsonl`, git-ignored) with reviewer, action, note, and a hash of what the AI recommended. An override needs a written reason. Recording a review does not approve a purchase.

## 10. Prompt injection defence
Free text is passed inside `<untrusted>` tags and the prompt says never to obey it. Independently of the model, regex detection in `policy.py` flags `prompt_injection_detected` (policy 9) and the request gets no approvals and a clarification action. The model can also raise the flag, but only if its quote is actually in the request text; an ungrounded suspicion is shown to the reviewer as an `inferred` note without a flag. Model output that claims approval (for example "is approved", "no review needed") is replaced by a template, and the original text is kept in telemetry for audit. Tested in `tests/test_guards.py`.

## 11. Failure handling
Vendor API down: `vendor_risk_unavailable`, Security review, evidence marked missing, never a favourable guess. Model down, no key, or malformed output: deterministic decision returned with an escalation reason. Provider rate limits: calls are spaced by `GEMINI_RPM`, per-minute 429s are retried with backoff, and a daily-quota 429 fails at once; the evaluation then stops without writing a result file, so an outage is never scored as a model failure. Tools refuse any `request_id` other than the one under review.

## 12. Evaluation design
`evals/expected_cases.json` was written before any architecture ran. It covers all six required edge cases over REQ-1001..1010. Both architectures use the same cases, data, policy and tools. Checks per run: correct action, exact approvals, required and forbidden flags, missing information, grounding (every item cites a record, model items labelled inferred), no approval claim, human review flag, AI summary present. Also measured: latency, LLM calls, tool calls, output stability over repeats. The starter `evals/run_public_evals.py` also still passes.

## 13. Reproduce
```bash
python evals/run_eval.py --repeats 3          # real model (LLM_PROVIDER in .env), both architectures; free-tier throttle waits are excluded from latency and logged separately
python evals/run_eval.py --provider stub --out evals/results/no_model_floor   # rules only, no model: the floor
python evals/analyze_results.py <run>.json <floor>.json                       # what the model added on top of the rules
python evals/run_public_evals.py --architecture single                        # starter checks (and --architecture staged)
python -m unittest discover -s tests          # 73 offline tests, no network
```
Results are written to `evals/results/` (JSON and markdown).

## 14. Results

Model `gemini-3.5-flash-lite` (Gemini API free tier), provider default temperature, 10 cases, 3 repeats per case per architecture, run on 2026-10-03 (08:29:50 UTC). Source: [evals/results/gemini_20261003_135950.md](evals/results/gemini_20261003_135950.md). Why this model and not a full Flash model: every full Flash model on the free tier allows 20 requests a day, see [docs/model_selection.md](docs/model_selection.md).

| metric (30 runs each) | A single agent | B staged, two agents |
|---|---|---|
| all checks pass | 29/30 (97%) | 30/30 (100%) |
| correct action | 100% | 100% |
| exact approvals | 100% | 100% |
| required and forbidden flags | 97% | 100% |
| missing information | 100% | 100% |
| grounding (every item cites a record, model items labelled inferred) | 100% | 100% |
| no approval claim | 100% | 100% |
| human review flag | 100% | 100% |
| AI summary present | 100% | 100% |
| latency mean / p50 / max (s, throttle waits excluded) | 2.66 / 2.6 / 3.289 | 4.92 / 4.79 / 7.255 |
| LLM calls per request (mean) | 2 | 4.03 |
| tool calls per request (mean) | 4 | 4 |
| cases whose output changed across the 3 repeats | 1 of 10 | 0 of 10 |

Tokens per request, from a separate 1-repeat run with token logging ([evals/results/token_cost_run/gemini_20261003_140550.md](evals/results/token_cost_run/gemini_20261003_140550.md), all checks passed for both): A 3261 input and 227 output, B 4556 input and 440 output.

**The one failure.** A, REQ-1002, repeat 3: the model marked the plain justification "PixelCraft is powerful but too specialist for non-designers" as a prompt injection, so a forbidden `prompt_injection_detected` flag was added. The other two repeats were correct. The quote was real text, so the grounding rule could not catch it; this is a judgement error by the model, not a code bug, and it is the one unstable case.

**What the checks do and do not measure.** The rules decide approvals, flags and actions, so the same checks also pass at 100% with no model at all ([evals/results/no_model_floor/](evals/results/no_model_floor/), scripted stub, not a model result). The checks therefore measure whether the model *breaks* the deterministic core, not how useful its writing is. What the model added, from [the contribution report](evals/results/gemini_20261003_135950_model_contribution.md):

| out of 30 runs | A | B |
|---|---|---|
| model rationale used | 25 | 28 |
| rationale replaced by the guard (approval-like wording) | 5 | 2 |
| flags added by the model | 4 | 3 |
| escalations raised by the model | 6 | 2 |

Every replaced rationale is listed in that report. Most say something true about the vendor or another tool ("vendor status is approved"), which the guard rejects on purpose because a skimming reviewer could misread it.

**Before the fixes.** The first full run (1 repeat, [evals/results/iterations/gemini_20261003_133750.md](evals/results/iterations/gemini_20261003_133750.md)) passed A 7/10 and B 9/10. The four failures had code causes, fixed for both architectures (section 21). The rerun after the fixes passed 10/10 for both ([iterations/gemini_20261003_134436.md](evals/results/iterations/gemini_20261003_134436.md)). The expected outcomes and the checks were not changed.

**Starter public evals.** 6/6 minimum checks for both architectures ([evals/results/public/](evals/results/public/)). Their latency column is wall-clock time and includes free-tier throttle waits, so it is not comparable with the table above.

**Limits of these numbers.** 10 cases, 3 repeats: one failure moves a pass rate by 1 in 30, so A and B are not separated with any statistical confidence. One small free-tier model; a larger model may behave differently. Temperature is left at the provider default (Google's guidance for Gemini 3), so runs are not deterministic. Latency depends on Google's free-tier servers at the time of the run.

## 15. Architecture decision
See [docs/decision_memo.md](docs/decision_memo.md). Written after the run.

## 16. Assumptions
Listed in docs/architecture.md (data-class mapping, day-365 boundary, add-on extension rule, unknown vendor is not current).

## 17. Known limitations
- Ten requests is a small set; passing it is not proof for unseen data.
- Overlap and add-on detection use a narrow rule plus model judgement; real catalogs need richer matching.
- Injection regexes catch known phrasings; the model and the "no approvals on injected requests" rule are the second line.
- Audit log is a local file, not tamper-proof.
- The vendor API is a mock, case-exact on names; the tool uses the registry's spelling.
- Latency is dominated by model round trips and depends on the provider.
- The pass/fail checks pass with no model too (section 14); rationale quality is not scored, only counted and listed.
- The guard replaces 5 of 30 (A) and 2 of 30 (B) model rationales because they contain wording like "is approved" about something else. Conservative on purpose, but it throws away some good text.
- The model's injection judgement can be wrong on real, harmless text (one case in 30 for A).
- Evaluated on `gemini-3.5-flash-lite` because full Flash models allow 20 free requests a day.

## 18. Security and secrets
No keys in the repo. `.env` is git-ignored; `.env.example` has empty placeholders.

## 19. Repo map
`src/policy.py` rules, `src/tools.py` tools, `src/solution.py` architectures and guard, `src/llm.py` provider, `src/handoff.py`, `app.py` UI, `evals/` cases, runners and `analyze_results.py`, `evals/results/` every result file (final run at the top level; `iterations/`, `smoke/`, `no_model_floor/`, `token_cost_run/`, `public/` in subfolders), `tests/`, `docs/` (architecture, model selection, decision memo).

## 20. Time and cost
Measured (section 14): A makes 2 model calls per request (the model calls all four tools in one turn, then submits), B makes 4. Per request, A used 3261 input and 227 output tokens; B used 4556 and 440. On the free tier the run costs nothing; at paid prices B costs more because it sends more tokens and makes twice the calls.

## 21. Failures found during evaluation, and fixes
Each fix applies to both architectures and has a regression test.

| # | Failure | Root cause | Fix |
|---|---|---|---|
| 1 | Model was told every request had no user count | Prompt asked for `num_users`; the data field is `user_count` | `REQUEST_FIELDS` in `src/solution.py`, test checks every field exists in the data |
| 2 | Correct rationales discarded, false escalations | Guard rejected any use of the word "approved", including "approved software catalog" | Guard now matches approval claims, not the word |
| 3 | A, REQ-1004: "NeuralDesk Business is approved for limited use" failed the no-approval-claim check | My narrower guard (fix 2) let "is approved" through; the scorer does not | Guard always rejects "is approved"; the policy's own escalation text reworded |
| 4 | A, REQ-1002: injection flag on harmless text | Model flag was accepted without evidence | Model flag needs a quote found in the request; otherwise an `inferred` note only |
| 5 | A and B, REQ-1010: overlap flagged for a same-vendor training pack | The submit schema never defined overlap; the rule in `policy.py` does | Schema now states the policy 3 definition used by the rule |
| 6 | "Offline" test called the live API | `src/__init__.py` loads `.env`; the test removed only the Anthropic key | Test removes all keys and checks both providers |
| 7 | Function responses did not carry Gemini's call ids | Adapter assigned ids but never sent them back | Model-issued ids are echoed; local ids are not sent |
| 8 | Daily quota retried for minutes, then scored as a model failure | All 429s treated as transient | Daily-quota 429 fails at once; the eval stops and writes no result |
| 9 | Rate-limit spacing reset per request | Throttle timestamp was per instance; UI and public runner create one per request | Timestamp shared across instances |

