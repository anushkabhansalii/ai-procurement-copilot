# What the model added (gemini-3.5-flash-lite, repeats 3, run 2026-10-03T08:29:50+00:00)

Compared with the no-model floor `stub_20261003_134455.json` (scripted stub: rules only, no model judgement).

| metric | A single | B staged |
|---|---|---|
| runs | 30 | 30 |
| rationale_kept | 25 | 28 |
| rejected_rationale | 5 | 2 |
| model_added_flags | 4 | 3 |
| model_added_flag_types | existing_tool_overlap x3, prompt_injection_detected x1 | existing_tool_overlap x3 |
| escalated_by_model | 6 | 2 |

## Rationales the guard rejected

- single REQ-1001: The request is within the available Finance budget and the vendor is fully approved with low risk. However, SignFlow is already approved as a company-wide tool, so there is partial overlap with existing catalog products. Policy requires manager approval for this request.
- single REQ-1007: Although the annual cost is within the available department budget, policy requires approval due to existing tool overlap with the same vendor, conflicting vendor security evidence, and production telemetry access. The vendor-risk service reports an expired security review while the internal registry lists it as approved.
- single REQ-1008: The request requires Department Head and Procurement approvals and flags an existing catalog overlap since TaskFlow is already an approved company-wide tool. The vendor is approved and the $8,000 annual cost is within the Marketing department's available budget.
- single REQ-1008: The request requires Department Head and Procurement approvals and flags an existing tool overlap since TaskFlow is already an approved company-wide tool in the catalog. The $8,000 annual cost is within the department's available budget of $15,000. Vendor status and security reviews are fully approved.
- single REQ-1010: The request is within the department's available budget and requires only manager approval. While the vendor SignFlow is already approved for e-signature software, this request is for a training pack (professional services) which does not overlap with existing catalog products. Vendor procurement and security status are fully approved.
- staged REQ-1001: The request requires Manager approval based on the annual cost of $800.00. There is a full overlap since SignFlow is already approved as a company-wide catalog product with existing seats. The cost is within the available budget of $29,000.00.
- staged REQ-1008: The policy engine requires approvals from Department Head and Procurement, and flags an existing tool overlap with SW003. An existing company-wide catalog product from TaskFlow already covers this exact use case. Cost is within available budget, and vendor status is approved.
