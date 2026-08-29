"""In-memory mock data for the policy/claims MCP server.

Swap this module for a real database or API client when you point the
project at a production backend — nothing outside this file needs to change.
"""

POLICIES = {
    "POL-10234": {
        "policyholder_name": "Rahul Mehta",
        "phone_number": "+919876543210",
        "date_of_birth": "1988-04-12",
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
        "phone_number": "+919123456780",
        "date_of_birth": "1990-09-23",
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
        "phone_number": "+919988776655",
        "date_of_birth": "1975-01-30",
        "policy_type": "Home Insurance",
        "status": "Lapsed",
        "premium_amount": 4300,
        "premium_currency": "INR",
        "renewal_date": "2026-06-01",
        "coverage_summary": "Structure and contents cover, fire and allied perils.",
    },
}

# Populated by register_claim at runtime. Pre-seeded with one open claim on
# POL-10234 so verify_identity's "remind the caller about an active claim"
# behavior has something to show out of the box.
CLAIMS: dict[str, dict] = {
    "CLM-00001": {
        "claim_id": "CLM-00001",
        "policy_number": "POL-10234",
        "incident_date": "2026-08-15",
        "incident_description": "Minor collision in a parking lot.",
        "contact_phone": "+919876543210",
        "status": "Submitted",
        "filed_at": "2026-08-15T09:30:00+00:00",
    },
}

# Claim statuses that no longer count as "active" for the identity-check reminder.
CLOSED_CLAIM_STATUSES = {"Closed", "Resolved", "Rejected"}

# Populated at runtime by escalate_to_human — a simple record of every handoff.
HANDOFFS: dict[str, dict] = {}
