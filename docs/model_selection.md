# Model selection (measured on 2026-10-03)

The goal was the newest stable Gemini Flash model on the free tier. The free tier turned out to be too small for that model to finish the evaluation, so the evaluated model is `gemini-3.5-flash-lite`. This file records what was measured and why.

## What the evaluation needs
One case costs about 2 to 3 model calls in A and about 4 in B (from the smoke run in `evals/results/smoke/`). Ten cases, both architectures, three repeats is roughly 200 to 250 calls on one model, plus a debugging pass. The comparison is only fair if both architectures use the same model, so the calls cannot be spread over several models.

## Free-tier limits seen on this API key
Limits are per project and per model. They are not published per model any more, so they were read from the `quotaId` and `quotaValue` fields of real 429 errors.

| Model | Requests per minute | Requests per day | How it was found |
|---|---|---|---|
| gemini-3.8-flash | 5 | 20 | burst of 14 calls, then daily 429 after the smoke test |
| gemini-3.7-flash | not measured | 20 | evaluation run stopped by daily 429 |
| gemini-3.6-flash | not measured | 20 | 22 spaced calls, 429 after 20 |
| gemini-3.5-flash | not measured | 20 | 22 spaced calls, 429 after 19 |
| gemini-3.5-flash-lite | 15 | more than 22 (not reached) | 22 spaced calls all succeeded; burst of 20 hit the 15 RPM limit |
| gemini-3.1-flash-lite | not measured | more than 22 (not reached) | 22 spaced calls all succeeded |
| gemini-2.5-flash, gemini-2.5-flash-lite | n/a | n/a | 404, "no longer available to new users" |

Every full Flash model is capped at 20 requests a day, which is about one tenth of one evaluation run. Paid billing would remove the cap; it was not used.

## Choice
`gemini-3.5-flash-lite`, the newest Flash-Lite model, with `GEMINI_RPM=12` (below its measured 15 RPM). It is a smaller model than 3.8 Flash, so the results here are a lower bound on what a full Flash model would do. The architectures, prompts, tools and cases do not depend on the model; switching is one line in `.env` (`MODEL_NAME`).

## Code changes this caused
* `GeminiLLM` now fails at once on a daily-quota 429 instead of retrying, and `evals/run_eval.py` stops without writing a result file in that case, so an outage is never scored as a model failure.
* The RPM throttle is shared across `GeminiLLM` instances, because the UI and the public runner create one per request.
