"""Mock MCP server exposing the three tools Ava uses.

This is a *mock* backend — data lives in memory (see data.py) and resets
every time the process restarts. It exists so the whole voice agent runs
end-to-end today. To go live, point bot.py's MCP_SERVER_URL at your real
MCP server instead (stdio, SSE, or streamable HTTP are all supported by
Pipecat's MCPClient) — nothing in bot.py or the system prompt needs to change,
as long as the real server exposes tools with the same names and arguments.

Run directly:
    python mcp_server/server.py

Serves streamable HTTP MCP at http://127.0.0.1:8787/mcp by default.

Where the "deterministic checks" live
--------------------------------------
The voice agent (bot.py) is deliberately agentic: the LLM decides which tool
to call, when, and how to phrase things — there's no hardcoded conversation
state machine. The things that genuinely must be deterministic (does this
policy exist, is it active enough to claim against, is the urgency value
valid) live here, inside the tools, as plain data validation. That keeps
"judgment" (how to talk to the caller) agentic and "facts" (is this claim
allowed) deterministic, without the two getting tangled together.
"""

import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from data import CLAIMS, HANDOFFS, POLICIES

mcp = FastMCP("Ava Policy & Claims Tools", host="127.0.0.1", port=8787)

VALID_URGENCY_LEVELS = {"low", "normal", "high", "urgent"}


@mcp.tool()
def get_policy_details(policy_number: str) -> dict:
    """Look up an insurance policy by its policy number.

    Args:
        policy_number: The policy number as given by the caller, e.g. "POL-10234".

    Returns:
        The policy's holder name, type, status, premium, renewal date, and
        coverage summary if found; otherwise a not-found message.
    """
    policy = POLICIES.get(policy_number.strip().upper())
    if not policy:
        return {
            "found": False,
            "policy_number": policy_number,
            "message": (
                f"No policy found with number '{policy_number}'. "
                "Ask the caller to double-check the number, or read it back to confirm."
            ),
        }
    return {"found": True, "policy_number": policy_number.strip().upper(), **policy}


@mcp.tool()
def register_claim(
    policy_number: str,
    incident_date: str,
    incident_description: str,
    contact_phone: str,
) -> dict:
    """File a new insurance claim against an existing, active policy.

    Args:
        policy_number: The policy the claim is filed against, e.g. "POL-10234".
        incident_date: Date the incident happened, in YYYY-MM-DD format.
        incident_description: What happened, in the caller's own words.
        contact_phone: A phone number to reach the policyholder about the claim.

    Returns:
        On success, the new claim_id and status. On failure, a reason code
        and a message explaining what's missing or wrong, so the caller can
        be told clearly what to do next.
    """
    policy_number = policy_number.strip().upper()
    policy = POLICIES.get(policy_number)

    if not policy:
        return {
            "success": False,
            "reason": "policy_not_found",
            "message": f"No policy found with number '{policy_number}'. Can't file a claim against it.",
        }

    if policy["status"] != "Active":
        return {
            "success": False,
            "reason": "policy_not_active",
            "policy_status": policy["status"],
            "message": (
                f"Policy '{policy_number}' is {policy['status'].lower()}, not active, "
                "so a claim can't be registered against it right now."
            ),
        }

    missing = [
        field
        for field, value in {
            "incident_date": incident_date,
            "incident_description": incident_description,
            "contact_phone": contact_phone,
        }.items()
        if not value or not value.strip()
    ]
    if missing:
        return {
            "success": False,
            "reason": "missing_information",
            "missing_fields": missing,
            "message": f"Missing required details: {', '.join(missing)}.",
        }

    claim_id = f"CLM-{len(CLAIMS) + 1:05d}"
    CLAIMS[claim_id] = {
        "claim_id": claim_id,
        "policy_number": policy_number,
        "incident_date": incident_date,
        "incident_description": incident_description,
        "contact_phone": contact_phone,
        "status": "Submitted",
        "filed_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "success": True,
        "claim_id": claim_id,
        "status": "Submitted",
        "message": f"Claim {claim_id} has been filed and is now under review.",
    }


@mcp.tool()
def escalate_to_human(
    reason: str,
    conversation_summary: str,
    urgency: str = "normal",
    customer_name: str = "",
    policy_number: str = "",
    claim_id: str = "",
) -> dict:
    """Hand the conversation off to a human agent with full context.

    Call this whenever the caller needs a human — ask directly, a request
    outside policy lookups and claims, a tool that keeps failing, or a
    caller who sounds frustrated or unsatisfied. Judging *when* to escalate
    is left entirely to you; this tool just records and confirms the handoff.

    Args:
        reason: Short reason for the escalation, e.g. "caller asked for a human agent".
        conversation_summary: A few sentences covering what the caller wants,
            what's already been found out (policy number, claim ID, key facts),
            and why you're escalating — written so a human agent has full
            context without asking the caller to repeat themselves.
        urgency: One of "low", "normal", "high", "urgent".
        customer_name: The caller's name, if known.
        policy_number: The relevant policy number, if known.
        claim_id: The relevant claim ID, if one was already filed this call.

    Returns:
        Confirmation with a handoff_id the caller can be told about.
    """
    urgency = urgency.strip().lower() if urgency else "normal"
    if urgency not in VALID_URGENCY_LEVELS:
        urgency = "normal"

    handoff_id = f"HANDOFF-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "handoff_id": handoff_id,
        "reason": reason,
        "conversation_summary": conversation_summary,
        "urgency": urgency,
        "customer_name": customer_name or None,
        "policy_number": policy_number or None,
        "claim_id": claim_id or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    HANDOFFS[handoff_id] = record

    print("\n" + "=" * 60)
    print(f"HUMAN HANDOFF  [{handoff_id}]  urgency={urgency}")
    print("-" * 60)
    print(f"Reason:            {reason}")
    print(f"Customer name:     {record['customer_name'] or 'unknown'}")
    print(f"Policy number:     {record['policy_number'] or 'n/a'}")
    print(f"Claim ID:          {record['claim_id'] or 'n/a'}")
    print(f"Conversation summary:\n  {conversation_summary}")
    print("=" * 60 + "\n")

    return {
        "success": True,
        "handoff_id": handoff_id,
        "message": "A human agent has been notified with full context and will join shortly.",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
