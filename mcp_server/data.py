"""In-memory mock data for the policy/claims MCP server.

Swap this module for a real database or API client when you point the
project at a production backend — nothing outside this file needs to change.
"""

POLICIES = {
    "POL-10234": {
        "policyholder_name": "Rahul Mehta",
        "policy_type": "Auto Insurance",
        "status": "Active",
        "premium_amount": 8400,
        "premium_currency": "INR",
        "renewal_date": "2027-02-14",
        "coverage_summary": (
            "Comprehensive coverage — own-damage and third-party, "
            "insured declared value 6,50,000 rupees."
        ),
    },
    "POL-55891": {
        "policyholder_name": "Anita Verma",
        "policy_type": "Health Insurance",
        "status": "Active",
        "premium_amount": 15200,
        "premium_currency": "INR",
        "renewal_date": "2026-11-30",
        "coverage_summary": (
            "Family floater plan — sum insured 10,00,000 rupees, covers 2 dependents."
        ),
    },
    "POL-77120": {
        "policyholder_name": "Suresh Nair",
        "policy_type": "Home Insurance",
        "status": "Lapsed",
        "premium_amount": 4300,
        "premium_currency": "INR",
        "renewal_date": "2026-06-01",
        "coverage_summary": "Structure and contents cover, fire and allied perils.",
    },
}

# Populated at runtime by register_claim.
CLAIMS: dict[str, dict] = {}

# Populated at runtime by escalate_to_human — a simple record of every handoff.
HANDOFFS: dict[str, dict] = {}
