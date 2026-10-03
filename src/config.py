"""Policy constants. Every number here comes from data/procurement_policy.md (v2026.09).

Keeping them in one file means a policy change is a one-line edit, and the unit tests
in tests/test_policy.py pin the boundary behaviour.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

# Policy section header: "Data snapshot / evaluation reference date: 2026-09-30".
# Date-based checks must use this, never date.today().
REFERENCE_DATE = date(2026, 9, 30)

# Policy 5: a vendor security assessment is current for 365 days from its review date.
REVIEW_VALID_DAYS = 365

# Policy 7: legal review when the vendor is new and annual spend is $10,000 or more.
LEGAL_NEW_VENDOR_MIN_SPEND = Decimal("10000")

# Policy 4: (upper bound inclusive, minimum business approvals). Order matters.
APPROVAL_TIERS: list[tuple[Decimal | None, list[str]]] = [
    (Decimal("1000"), ["Manager"]),
    (Decimal("10000"), ["Department Head", "Procurement"]),
    (Decimal("25000"), ["Department Head", "Finance", "Procurement"]),
    (None, ["Department Head", "Finance", "CFO", "Procurement"]),
]

# Policy 5 / 6: data classes seen in requests.json, mapped to the review they trigger.
# ENGINEERING ASSUMPTION: the policy lists data classes in prose; this mapping is how
# we read the values that actually appear in the data. Unknown values are NOT treated as
# safe: policy.py reports them as missing information.
SECURITY_DATA_CLASSES = {
    "source_code",
    "confidential_documents",
    "employee_pii",
    "customer_pii",
    "credentials",
    "secrets",
    "production_telemetry",  # assumption: telemetry from production systems
}
PRIVACY_DATA_CLASSES = {"employee_pii", "customer_pii"}
# Classes where "sensitive data stored outside the operating region" matters (policy 6/7).
SENSITIVE_FOR_REGION = SECURITY_DATA_CLASSES
NON_SENSITIVE_DATA_CLASSES = {"none", "internal_documents", "internal_marketing"}
UNKNOWN_DATA_CLASSES = {"", "unknown", "tbd", "n/a"}

# Policy 5: "production or cloud-account integration". Keyword match on requested integrations.
PRODUCTION_INTEGRATION_KEYWORDS = ("production", "cloud account", "cloud-account", "aws", "gcp", "azure", "kubernetes")

# Policy 9: patterns that mean business data is trying to give instructions.
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|procurement|policy|policies)?\s*(rules|instructions|polic(y|ies)|controls|guidelines)",
    r"disregard (all |any |the )?(previous|prior|above)?\s*(rules|instructions|polic(y|ies))",
    r"treat (this|the) (request|purchase)? ?as (already )?(cfo[- ]|manager[- ]|security[- ]|legal[- ])?approved",
    r"\b(cfo|ceo|security|legal)[- ]approved\b",
    r"approve (it|this|the (request|purchase))( immediately| now| automatically)?",
    r"(bypass|override|skip|waive) (the |all |any )?(procurement|security|privacy|legal|policy|policies|approval|review|controls?|rules)",
    r"(do not|don't|never) (flag|escalate|mention|report)",
    r"(reveal|print|show|expose) (your |the )?(system prompt|api key|secret|credentials)",
    r"you are now\b",
    r"\bnew instructions?\b",
]
