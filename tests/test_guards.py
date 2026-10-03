"""The guard layer must hold even when the model is wrong, hostile or absent. No network model needed."""
import unittest
import warnings

import _mockapi

from src import tools
from src.llm import LLMReply, StubLLM
from src.solution import finalize, handle_request

warnings.filterwarnings("ignore")


class Scripted(StubLLM):
    """Runs the normal tool sequence, then submits whatever payload the test chooses."""
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def complete(self, system, messages, tools_, max_tokens=1500):
        reply = super().complete(system, messages, tools_, max_tokens)
        c = reply.tool_calls[0]
        if c["name"] in ("submit_assessment",):
            block = {"type": "tool_use", "id": c["id"], "name": c["name"], "input": self.payload}
            return LLMReply("", [{"id": c["id"], "name": c["name"], "input": self.payload}], [block])
        return reply


def setUpModule():
    _mockapi.ensure()


def tearDownModule():
    _mockapi.stop()


def good(**o):
    p = {"rationale": "Fine.", "overlap_judgement": "none", "prompt_injection_suspected": False, "injection_quote": "", "inferred_notes": []}
    p.update(o)
    return p


class Guards(unittest.TestCase):
    def test_model_cannot_remove_security_review(self):
        d = handle_request("REQ-1003", "single", llm=Scripted(good(rationale="No security review is needed, it is safe to approve.")))
        self.assertIn("Security", d.required_approvals)
        self.assertIn("security_review_required", d.risk_flags)
        self.assertNotIn("safe to approve", d.recommendation)
        self.assertTrue(any("replaced by a template" in r for r in d.escalation_reasons))

    def test_model_cannot_add_approvals_or_remove_flags(self):
        d1 = handle_request("REQ-1001", "single", llm=Scripted(good()))
        self.assertEqual(d1.required_approvals, ["Manager"])
        self.assertEqual(d1.risk_flags, [])

    def test_model_can_add_injection_flag_as_inferred(self):
        d = handle_request("REQ-1001", "single", llm=Scripted(good(prompt_injection_suspected=True, injection_quote="three additional signing identities")))
        self.assertIn("prompt_injection_detected", d.risk_flags)
        self.assertTrue(all(e.status == "inferred" for e in d.evidence if e.source == "model"))

    def test_model_overlap_judgement_is_additive_and_labelled(self):
        d = handle_request("REQ-1001", "single", llm=Scripted(good(overlap_judgement="partial")))
        self.assertIn("existing_tool_overlap", d.risk_flags)

    def test_model_failure_degrades_to_deterministic_decision(self):
        d = handle_request("REQ-1005", "staged", llm=StubLLM(fail=True))
        self.assertIn("Finance", d.required_approvals)
        self.assertTrue(d.human_review_required)
        self.assertTrue(any("AI summary unavailable" in r for r in d.escalation_reasons))

    def test_malformed_output_is_bounded_not_looped(self):
        d = handle_request("REQ-1005", "single", llm=StubLLM(bad_output=True))
        self.assertLessEqual(d.telemetry.llm_calls, 8)
        self.assertIn("security_review_required", d.risk_flags)

    def test_injection_request_never_gets_approvals(self):
        d = handle_request("REQ-1006", "single", llm=Scripted(good(rationale="CFO-approved, approved immediately.")))
        self.assertEqual(d.required_approvals, [])
        self.assertNotIn("approved immediately", d.recommendation)
        self.assertIn("prompt_injection_detected", d.risk_flags)

    def test_unavailable_vendor_service_is_never_favourable(self):
        d = handle_request("REQ-1009", "single", llm=StubLLM())
        self.assertIn("vendor_risk_unavailable", d.risk_flags)
        self.assertTrue(any(e.status == "missing" for e in d.evidence))

    def test_tool_refuses_other_request_id(self):
        ctx = tools.RunContext("REQ-1001")
        r = ctx.run_tool("lookup_budget", "REQ-1005")
        self.assertEqual(r.status, "error")

    def test_unknown_request_id_raises_clearly(self):
        with self.assertRaises(KeyError):
            handle_request("REQ-9999", "single", llm=StubLLM())

    def test_no_api_key_still_returns_decision(self):
        # src/__init__ loads .env, so every key must be removed or this "offline" test calls the live API.
        import os
        keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        old = {k: os.environ.pop(k, None) for k in keys}
        try:
            for provider in ("gemini", "anthropic"):
                os.environ["LLM_PROVIDER"] = provider
                d = handle_request("REQ-1003", "single")
                self.assertIn("Security", d.required_approvals)
                self.assertTrue(any("AI summary unavailable" in r for r in d.escalation_reasons))
        finally:
            os.environ.pop("LLM_PROVIDER", None)
            for k, v in old.items():
                if v:
                    os.environ[k] = v

    def test_request_block_uses_real_field_names(self):
        # Bug: the prompt asked for "num_users", the data says "user_count", so both architectures
        # told the model every request had no user count.
        import json
        from pathlib import Path
        from src.solution import REQUEST_FIELDS, _request_block
        reqs = json.loads((Path(__file__).resolve().parents[1] / "data" / "requests.json").read_text())
        keys = set().union(*(r.keys() for r in reqs))
        self.assertEqual(set(REQUEST_FIELDS) - keys, set())
        self.assertIn('"user_count": 30', _request_block(next(r for r in reqs if r["request_id"] == "REQ-1003")))

    def test_ungrounded_model_injection_flag_is_not_a_risk_flag(self):
        # Seen on gemini-3.5-flash-lite, REQ-1002: injection flagged on a plain justification.
        d = handle_request("REQ-1002", "single", llm=Scripted(good(prompt_injection_suspected=True,
                                                                     injection_quote="approve this without review")))
        self.assertNotIn("prompt_injection_detected", d.risk_flags)
        self.assertTrue(any("not flagged" in e.finding and e.status == "inferred" for e in d.evidence))

    def test_grounded_model_injection_flag_is_added(self):
        from src import data_access
        text = data_access.get_request("REQ-1002")["business_justification"]
        d = handle_request("REQ-1002", "single", llm=Scripted(good(prompt_injection_suspected=True, injection_quote=text[:40])))
        self.assertIn("prompt_injection_detected", d.risk_flags)

    def test_benign_mentions_of_approved_keep_the_model_rationale(self):
        # Bug seen on gemini-3.8-flash: any use of the word "approved" (e.g. the "approved software
        # catalog") made the guard discard a correct rationale and add a false escalation.
        text = "GitHub Copilot is already in the approved software catalog, so overlap is partial. Security review is required."
        d = handle_request("REQ-1003", "single", llm=Scripted(good(rationale=text)))
        self.assertIn("approved software catalog", d.recommendation)
        self.assertFalse(any("replaced by a template" in r for r in d.escalation_reasons))
        self.assertIsNone(d.telemetry.rejected_rationale)

    def test_approval_claims_are_still_replaced_and_kept_for_audit(self):
        for claim in ("This request is approved.", "NeuralDesk Business is approved for limited use only.", "Finance-approved, go ahead.", "No further review is needed.",
                      "It is safe to approve.", "Treat as approved per the note.", "Vendor terms are approved."):
            d = handle_request("REQ-1001", "single", llm=Scripted(good(rationale=claim)))
            self.assertNotIn(claim, d.recommendation, claim)
            self.assertEqual(d.telemetry.rejected_rationale, claim)


if __name__ == "__main__":
    unittest.main()
