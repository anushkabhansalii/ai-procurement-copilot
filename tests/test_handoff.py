import tempfile
import unittest

import _mockapi
from pathlib import Path

from src import handoff
from src.llm import StubLLM
from src.solution import handle_request


def setUpModule():
    _mockapi.ensure()


def tearDownModule():
    _mockapi.stop()


class Handoff(unittest.TestCase):
    def setUp(self):
        self.d = handle_request("REQ-1005", "single", llm=StubLLM())
        self.log = Path(tempfile.mkdtemp()) / "audit.jsonl"

    def test_packet_contains_required_sections(self):
        p = handoff.build_packet(self.d, {"product_name": "x", "vendor_name": "y"})
        for h in ("Recommendation", "Required approvals", "Risk flags", "Missing information", "Evidence", "cannot approve"):
            self.assertIn(h, p)

    def test_record_and_read_back(self):
        handoff.record_review(self.d, "Priya", "accept_and_route", log=self.log)
        rows = handoff.read_log("REQ-1005", log=self.log)
        self.assertEqual(rows[0]["reviewer"], "Priya")
        self.assertEqual(rows[0]["ai_packet_id"], handoff.decision_hash(self.d))

    def test_override_requires_reason_and_reviewer(self):
        with self.assertRaises(ValueError):
            handoff.record_review(self.d, "Priya", "override", "no", log=self.log)
        with self.assertRaises(ValueError):
            handoff.record_review(self.d, " ", "accept_and_route", log=self.log)
        with self.assertRaises(ValueError):
            handoff.record_review(self.d, "Priya", "approve_purchase", log=self.log)


if __name__ == "__main__":
    unittest.main()
