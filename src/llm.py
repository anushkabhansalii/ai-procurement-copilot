"""Model access, kept behind one small interface so the architectures do not care who answers.

* GeminiLLM     - real provider (function calling). Needs GEMINI_API_KEY. The reported results use this.
* AnthropicLLM  - real provider (tool use). Needs ANTHROPIC_API_KEY. Supported, not used for the reported run.
* StubLLM       - scripted, no network. TEST ONLY: it proves plumbing and guard behaviour offline.
                  Every result produced with it is labelled `provider=stub` and must not be quoted
                  as a model result.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","name","input"}]
    raw_content: Any = None  # provider-native blocks, echoed back into the history
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(RuntimeError):
    pass


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, model: str | None = None, timeout: float = 60.0):
        import anthropic

        key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.model = model or os.getenv("MODEL_NAME")
        if not self.model:
            raise LLMError("MODEL_NAME must be set when LLM_PROVIDER=anthropic")
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=1)

    def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 1500) -> LLMReply:
        try:
            r = self.client.messages.create(
                model=self.model, system=system, messages=messages, tools=tools,
                max_tokens=max_tokens, temperature=0,
            )
        except Exception as exc:  # network, auth, rate limit: surfaced as one error type
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "input": dict(b.input)} for b in r.content if b.type == "tool_use"]
        return LLMReply(text, calls, r.content, r.usage.input_tokens, r.usage.output_tokens)

    def assistant_message(self, reply: LLMReply) -> dict:
        return {"role": "assistant", "content": reply.raw_content}

    def tool_results_message(self, results: list[tuple[str, str]]) -> dict:
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": body} for tid, body in results]}


class GeminiLLM:
    """Google Gemini via google-genai (generateContent + function calling).

    Same interface as AnthropicLLM. Differences worth knowing:
    * The model's own reply (types.Content) is echoed back unchanged, which keeps Gemini's thought
      signatures intact.
    * When Gemini issues call ids they are echoed back in each function response, so parallel calls to
      the same tool are matched correctly. If it issues none, we assign local ids (never sent back).
    * A daily-quota 429 fails at once with a clear message instead of retrying.
    * Free-tier rate limits: calls are spaced by GEMINI_RPM and 429s are retried. The seconds spent
      waiting are recorded in `wait_seconds` so the evaluation can report latency without them.
    * Temperature is left at the provider default (Google recommends this for Gemini 3).
    """
    provider = "gemini"
    # Shared by all instances: the quota is per project, and the UI and public runner create one
    # instance per request, so a per-instance timestamp would let back-to-back requests burst.
    _last_call = 0.0

    def __init__(self, model: str | None = None, client=None):
        import time as _t
        self._time = _t
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if client is None:
            if not key:
                raise LLMError("GEMINI_API_KEY is not set")
            from google import genai
            from google.genai import types
            # Without a timeout one slow provider response blocked a request for 75 s in the evaluation
            # (evals/results/gemini_20261008_135249.json, A REQ-1002). A timed-out call is retried like a 503.
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))))
        self.client = client
        self.model = model or os.getenv("MODEL_NAME") or "gemini-flash-latest"
        rpm = float(os.getenv("GEMINI_RPM", "8"))
        self.min_gap = 60.0 / rpm if rpm > 0 else 0.0
        self.max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "5"))
        self._ids: dict[str, str] = {}
        self._model_ids: set[str] = set()  # ids Gemini issued; these must be echoed in the function response
        self.wait_seconds = 0.0

    def _sleep(self, secs: float) -> None:
        if secs > 0:
            self._time.sleep(secs)
            self.wait_seconds += secs

    @staticmethod
    def _to_content(m):
        from google.genai import types
        if isinstance(m, types.Content):
            return m
        c = m["content"]
        if isinstance(c, str):
            return types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=c)])
        raise LLMError("unsupported message shape for Gemini")

    def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 4096) -> LLMReply:
        from google.genai import types
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                           parameters_json_schema=t["input_schema"]) for t in tools]
        cfg = types.GenerateContentConfig(
            system_instruction=system, tools=[types.Tool(function_declarations=decls)], max_output_tokens=max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        contents = [self._to_content(m) for m in messages]
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._sleep(self.min_gap - (self._time.monotonic() - GeminiLLM._last_call))
            GeminiLLM._last_call = self._time.monotonic()
            try:
                r = self.client.models.generate_content(model=self.model, contents=contents, config=cfg)
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "PerDay" in msg:  # daily quota: retrying for minutes cannot help, so fail at once and say why
                    raise LLMError(f"daily quota exhausted for {self.model}: {msg[:200]}") from exc
                transient = any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "504")) \
                    or "timeout" in f"{type(exc).__name__} {msg}".lower() or "timed out" in msg.lower()
                if not transient or attempt == self.max_retries:
                    raise LLMError(f"{type(exc).__name__}: {msg[:300]}") from exc
                self._sleep(min(60.0, 5.0 * (2 ** attempt)))
        else:  # pragma: no cover
            raise LLMError(str(last_exc))
        if not getattr(r, "candidates", None) or r.candidates[0].content is None:
            raise LLMError("Gemini returned no content (blocked or empty)")
        content = r.candidates[0].content
        calls, text = [], ""
        for i, part in enumerate(content.parts or []):
            if part.function_call is not None:
                cid = part.function_call.id or f"local{len(self._ids)}_{i}"
                self._ids[cid] = part.function_call.name
                if part.function_call.id:
                    self._model_ids.add(cid)
                calls.append({"id": cid, "name": part.function_call.name, "input": dict(part.function_call.args or {})})
            elif part.text and not part.thought:
                text += part.text
        u = getattr(r, "usage_metadata", None)
        return LLMReply(text, calls, content, getattr(u, "prompt_token_count", 0) or 0,
                        (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0))

    def assistant_message(self, reply: LLMReply):
        return reply.raw_content

    def tool_results_message(self, results: list[tuple[str, str]]):
        import json
        from google.genai import types
        parts = []
        for tid, body in results:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"text": body}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=tid if tid in self._model_ids else None, name=self._ids.get(tid, "unknown"), response=payload)))
        return types.Content(role="user", parts=parts)


class StubLLM:
    """Deterministic scripted 'model'. Calls the tools it is offered, then submits a canned assessment."""
    provider = "stub"
    model = "stub-scripted"

    def __init__(self, fail: bool = False, bad_output: bool = False):
        self.fail, self.bad_output = fail, bad_output

    def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 1500) -> LLMReply:
        if self.fail:
            raise LLMError("stub configured to fail")
        names = [t["name"] for t in tools]
        used = [b["name"] for m in messages if m["role"] == "assistant" and isinstance(m["content"], list)
                for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        done = set(used)
        rid = _first_request_id(messages)
        todo = [n for n in names if n not in ("submit_assessment", "submit_evidence_summary") and n not in done]
        if todo:
            n = todo[0]
            return self._call(n, {"request_id": rid}, len(used))
        submit = "submit_assessment" if "submit_assessment" in names else "submit_evidence_summary"
        if self.bad_output:
            return self._call(submit, {"rationale": 123}, len(used))
        if submit == "submit_assessment":
            payload = {"rationale": "Scripted rationale (stub).", "overlap_judgement": "none",
                       "prompt_injection_suspected": False, "injection_quote": "", "inferred_notes": []}
        else:
            payload = {"summary": "Scripted evidence summary (stub).", "notable_free_text": []}
        return self._call(submit, payload, len(used))

    @staticmethod
    def _call(name: str, inp: dict, n: int) -> LLMReply:
        block = {"type": "tool_use", "id": f"stub{n}", "name": name, "input": inp}
        return LLMReply("", [{"id": block["id"], "name": name, "input": inp}], [block])

    def assistant_message(self, reply: LLMReply) -> dict:
        return {"role": "assistant", "content": reply.raw_content}

    def tool_results_message(self, results: list[tuple[str, str]]) -> dict:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": t, "content": b} for t, b in results]}


def _first_request_id(messages: list[dict]) -> str:
    import re
    m = re.search(r"REQ-\d+", str(messages[0]["content"]))
    return m.group(0) if m else ""


def get_llm(provider: str | None = None):
    provider = (provider or os.getenv("LLM_PROVIDER") or "anthropic").lower()
    if provider == "stub":
        return StubLLM()
    if provider == "gemini":
        return GeminiLLM()
    return AnthropicLLM()
