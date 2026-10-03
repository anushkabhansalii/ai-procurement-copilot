"""Gemini adapter checked offline with a fake client that returns real google-genai types.
This proves message conversion and the loop, NOT that the live API behaves the same."""
import unittest

import _mockapi
import warnings

from google.genai import types

from src.llm import GeminiLLM, LLMError
from src.solution import SUBMIT_ASSESSMENT, handle_request

warnings.filterwarnings("ignore")


def fc(name, args):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def setUpModule():
    _mockapi.ensure()


def tearDownModule():
    _mockapi.stop()


class FakeModels:
    def __init__(self, script):
        self.script, self.calls, self.seen = list(script), 0, []

    def generate_content(self, model, contents, config):
        self.seen.append((contents, config))
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return types.GenerateContentResponse(candidates=[types.Candidate(content=step)])


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def llm_with(script, **env):
    import os
    os.environ["GEMINI_RPM"] = "0"
    g = GeminiLLM(model="fake", client=FakeClient(script))
    g._sleep = lambda s: None
    return g


SUBMIT = {"rationale": "Because of the amount and data class.", "overlap_judgement": "none", "prompt_injection_suspected": False}


class GeminiAdapter(unittest.TestCase):
    def test_single_agent_end_to_end(self):
        script = [fc("lookup_budget", {"request_id": "REQ-1003"}), fc("search_software_catalog", {"request_id": "REQ-1003"}),
                  fc("get_vendor_status", {"request_id": "REQ-1003"}), fc("check_policy", {"request_id": "REQ-1003"}),
                  fc("submit_assessment", SUBMIT)]
        d = handle_request("REQ-1003", "single", llm=llm_with(script))
        self.assertEqual(d.telemetry.llm_calls, 5)
        self.assertIn("Security", d.required_approvals)
        self.assertIn("Because of the amount", d.recommendation)

    def test_function_result_goes_back_with_the_right_tool_name(self):
        g = llm_with([fc("lookup_budget", {"request_id": "REQ-1001"}), fc("submit_assessment", SUBMIT)])
        handle_request("REQ-1001", "single", llm=g)
        second_call_contents = g.client.models.seen[1][0]
        resp = second_call_contents[-1].parts[0].function_response
        self.assertEqual(resp.name, "lookup_budget")
        self.assertIn("data", resp.response)

    def test_tool_specs_convert_to_declarations(self):
        g = llm_with([fc("submit_assessment", SUBMIT)])
        g.complete("sys", [{"role": "user", "content": "REQ-1001"}], [SUBMIT_ASSESSMENT])
        cfg = g.client.models.seen[0][1]
        decl = cfg.tools[0].function_declarations[0]
        self.assertEqual(decl.name, "submit_assessment")
        self.assertIn("rationale", decl.parameters_json_schema["properties"])

    def test_api_error_becomes_deterministic_decision(self):
        d = handle_request("REQ-1005", "staged", llm=llm_with([RuntimeError("500 INTERNAL")] * 10))
        self.assertIn("Finance", d.required_approvals)
        self.assertTrue(any("AI summary unavailable" in r for r in d.escalation_reasons))

    def test_retries_transient_429_then_succeeds(self):
        g = llm_with([RuntimeError("429 RESOURCE_EXHAUSTED"), fc("submit_assessment", SUBMIT)])
        r = g.complete("s", [{"role": "user", "content": "x"}], [SUBMIT_ASSESSMENT])
        self.assertEqual(r.tool_calls[0]["name"], "submit_assessment")

    def test_non_transient_error_is_not_retried(self):
        g = llm_with([RuntimeError("400 INVALID_ARGUMENT"), fc("submit_assessment", SUBMIT)])
        with self.assertRaises(LLMError):
            g.complete("s", [{"role": "user", "content": "x"}], [SUBMIT_ASSESSMENT])
        self.assertEqual(g.client.models.calls, 1)

    def test_model_call_ids_are_echoed_in_function_responses(self):
        # Bug seen on gemini-3.8-flash: it returns ids ("call_1427529") for parallel calls, and the
        # responses went back without them. Two calls to the same tool must stay distinguishable.
        def with_id(name, cid):
            return types.Part(function_call=types.FunctionCall(name=name, args={"request_id": "REQ-1001"}, id=cid))
        turn = types.Content(role="model", parts=[with_id("lookup_budget", "call_a"), with_id("lookup_budget", "call_b")])
        g = llm_with([turn, fc("submit_assessment", SUBMIT)])
        handle_request("REQ-1001", "single", llm=g)
        parts = g.client.models.seen[1][0][-1].parts
        self.assertEqual([p.function_response.id for p in parts], ["call_a", "call_b"])

    def test_local_ids_are_not_sent_to_gemini(self):
        g = llm_with([fc("lookup_budget", {"request_id": "REQ-1001"}), fc("submit_assessment", SUBMIT)])
        handle_request("REQ-1001", "single", llm=g)
        self.assertIsNone(g.client.models.seen[1][0][-1].parts[0].function_response.id)

    def test_daily_quota_fails_fast_without_retries(self):
        err = RuntimeError("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        g = llm_with([err, fc("submit_assessment", SUBMIT)])
        with self.assertRaisesRegex(LLMError, "daily quota exhausted"):
            g.complete("s", [{"role": "user", "content": "x"}], [SUBMIT_ASSESSMENT])
        self.assertEqual(g.client.models.calls, 1)

    def test_staged_architecture(self):
        script = [fc("lookup_budget", {"request_id": "REQ-1001"}), fc("search_software_catalog", {"request_id": "REQ-1001"}),
                  fc("get_vendor_status", {"request_id": "REQ-1001"}),
                  fc("submit_evidence_summary", {"summary": "ok"}),
                  fc("check_policy", {"request_id": "REQ-1001"}), fc("submit_assessment", SUBMIT)]
        d = handle_request("REQ-1001", "staged", llm=llm_with(script))
        self.assertEqual(d.telemetry.llm_calls, 6)


if __name__ == "__main__":
    unittest.main()
