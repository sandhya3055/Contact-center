"""MCP-gateway Lambda target wrapping ``app/tools`` for Bedrock AgentCore Gateway.

One Lambda, action-routed by ``context.client_context.custom["bedrockAgentCoreToolName"]``
(Gateway's Lambda-target contract passes the tool name as ``<target>___<tool>``; the
event itself is the tool's raw input). Mirrors the existing single-Lambda pattern
(``otp_start.py``) reaching into ``app.data``/``app.tools`` directly. Falls back to
``event["tool"]`` when that field is absent, solely so the Lambda Console's Test
feature (which has no way to set ``client_context``) can invoke a specific tool
directly -- see ``_tool_name``.

Three tools, deliberately scoped down from the full BUILD_SPEC §8 tool set (Opus
review, D103):

  - ``get_policy``: read-only, wraps ``lookup_policy_by_number``. Requires both
    ``number`` and ``dob``; results are filtered to policies whose stored ``dob``
    matches the input, so a client can't enumerate every policy on a number without
    also knowing the holder's date of birth. Response whitelists ``Policy`` down to
    non-PII fields (``_POLICY_FIELDS``) -- ``dob``/``email``/``mobile_no``/
    ``sum_insured``/``premium`` never reach an external client (the input ``dob`` is
    used only to filter, never echoed back). Each policy also carries
    ``open_claims`` (via ``list_claims_for_vehicle``, filtered to non-``CLOSED``) so
    a caller/agent can see a claim already in flight before attempting
    ``register_claim``.
  - ``register_claim``: wraps ``create_claim_pending``, guarded by
    ``check_duplicate_claim`` first (vehicle_reg is the natural idempotency key here
    -- a vehicle can't hold two open claims anyway). Still has no consent gate or
    read-back the way the graph's ``confirm_claim_write``/``claim_readback`` do
    (rule #1/#8); accepted for this external surface now that a retry can no longer
    mint a duplicate PENDING claim.
  - ``escalate``: queues a ``QUEUED`` row directly via ``EscalationRepository`` --
    the same write ``NoopEscalator.escalate`` performs -- rather than calling the
    full ``Escalator`` interface. An MCP client has no live audio channel to bridge
    a human onto, so a live-transfer attempt (``BridgeEscalator``) would be
    meaningless here; a human still picks the row up via the existing
    assignment path (``services/lambdas/assign_escalation.py``). ``reason`` is
    stored as a free string (not validated against ``HandoffReason``) and always
    queued at the lowest priority bucket -- see ``_QUEUE_PRIORITY`` below for why
    ``app.flow.state``/``app.escalation.priority`` are deliberately not imported.

>>> UNVERIFIED CONTRACT <<<
The Gateway Lambda-target invocation shape above (custom field name, ``___``
delimiter, bare-dict return on success, an exception on failure) is built from
best recollection, not confirmed against a live Gateway or current AWS docs.
Verify with a real deployment (log ``context.client_context.__dict__`` plus one
real tool call) before trusting this in production -- see PROJECT_STATE.md D103.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import asdict
from typing import Any

from app.config import AppConfig, load_config
from app.data.escalations import EscalationRepository
from app.data.models import (
    ClaimStatus,
    ClaimType,
    EscalationRecord,
    EscalationStatus,
    epoch_to_au,
    epoch_to_sort_key,
)
from app.tools.claim_tools import (
    ClaimSlots,
    check_duplicate_claim,
    create_claim_pending,
    list_claims_for_vehicle,
)
from app.tools.policy_tools import lookup_policy_by_number

logger = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"^\+[0-9]{7,15}$")
_DOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Fixed at app/escalation/priority.py's own "no priority mapped" default bucket
# (`_DEFAULT_PRIORITY = 9`, "queued, never dropped, but behind everything triaged") --
# not imported from there, since that module pulls in `app.flow.state.HandoffReason`,
# which transitively imports `app.nlu.extract` -> `app.speech.stt` (the STT/Bedrock
# stack), far too heavy for this Lambda's zip. An MCP-triggered escalation has no live
# caller waiting on the line, so the least-urgent bucket is also the correct one.
_QUEUE_PRIORITY = 9

# Policy fields safe for an external MCP client: excludes `dob`, `email`,
# `mobile_no`, `sum_insured`, `premium` (PII/financial detail no MCP caller needs).
_POLICY_FIELDS = (
    "policy_id",
    "status",
    "vehicle_reg",
    "holder_name",
    "expiry_date",
    "start_date",
    "policy_type",
    "vehicle_make",
    "vehicle_model",
    "vehicle_year",
    "currency",
)

# Claim fields surfaced alongside a policy so a caller/agent can see a claim is
# already in flight before attempting to register a duplicate.
_CLAIM_FIELDS = (
    "claim_ref",
    "claim_type",
    "status",
    "incident_date",
    "incident_location",
    "description",
)


class ToolError(Exception):
    """Unknown tool name or invalid tool input from the Gateway."""


def _tool_name(event: dict, context: Any) -> str:
    """Extract the tool name to dispatch on.

    The real path is the Gateway's ``target___tool`` custom field on ``context``.
    Falls back to ``event["tool"]`` only when that field is absent -- the Lambda
    Console's own Test feature has no way to set ``client_context``, so this is
    what lets a plain ``{"tool": "get_policy", ...}`` payload be tested there
    directly. A real Gateway call always carries the custom field, so this never
    fires in production.
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full_name = custom.get("bedrockAgentCoreToolName", "")
    if full_name:
        return full_name.rsplit("___", 1)[-1]
    return event.get("tool", "")


def _validate_number(number: str | None) -> str:
    """Validate the untrusted ``number`` field; raise ``ValueError`` if not plausible."""
    if not number or not _NUMBER_RE.match(number):
        raise ValueError("invalid number")
    return number


def _validate_dob(dob: str | None) -> str:
    """Validate the untrusted ``dob`` field; raise ``ValueError`` if not ISO ``YYYY-MM-DD``."""
    if not dob or not _DOB_RE.match(dob):
        raise ValueError("invalid dob")
    return dob


def _policy_view(config: AppConfig, policy: Any) -> dict:
    """Whitelist a :class:`Policy` down to fields safe for an external MCP client.

    Attaches ``open_claims`` (non-``CLOSED`` claims on the policy's vehicle) so a
    caller/agent can see a claim is already in flight before attempting to register
    a duplicate -- mirrors the ``has_open_claim`` gate ``register_claim`` enforces.
    """
    full = asdict(policy)
    view = {field: full[field] for field in _POLICY_FIELDS}
    view["open_claims"] = [
        _claim_view(claim)
        for claim in list_claims_for_vehicle(config, policy.vehicle_reg)
        if claim.status is not ClaimStatus.CLOSED
    ]
    return view


def _claim_view(claim: Any) -> dict:
    """Whitelist a :class:`Claim` down to fields safe for an external MCP client."""
    full = asdict(claim)
    return {field: full[field] for field in _CLAIM_FIELDS}


def _get_policy(config: AppConfig, event: dict) -> dict:
    number = _validate_number(event.get("number"))
    dob = _validate_dob(event.get("dob"))
    result = lookup_policy_by_number(config, number)
    matched = [p for p in result.policies if p.dob == dob]
    return {
        "found": bool(matched),
        "has_active": any(p.is_active for p in matched),
        "policies": [_policy_view(config, p) for p in matched],
    }


def _register_claim(config: AppConfig, event: dict) -> dict:
    vehicle_reg = event["vehicle_reg"]
    if check_duplicate_claim(config, vehicle_reg):
        raise ValueError(f"a claim is already open for vehicle_reg {vehicle_reg}")
    slots = ClaimSlots(
        claim_type=ClaimType(event["claim_type"]),
        vehicle_reg=vehicle_reg,
        incident_date=event["incident_date"],
        incident_location=event["incident_location"],
        garage_name=event.get("garage_name", ""),
        tow_required=event.get("tow_required"),
        estimated_cost=event.get("estimated_cost"),
        description=event.get("description", ""),
    )
    claim_ref = create_claim_pending(
        config, event["policy_id"], slots, int(time.time())
    )
    return {"claim_ref": claim_ref}


def _escalate(config: AppConfig, event: dict) -> dict:
    reason = event.get("reason") or "caller_requested"
    now = int(time.time())
    # now stays a raw epoch int here -- ttl below requires it (DynamoDB TTL only
    # works with epoch seconds), and queue_key's sort order needs it too (the
    # AU-formatted created_at below isn't lexicographically sortable, see
    # epoch_to_sort_key).
    record = EscalationRecord(
        escalation_id=str(uuid.uuid4()),
        session_id=event["session_id"],
        reason=reason,
        status=EscalationStatus.QUEUED,
        queue_key=f"{_QUEUE_PRIORITY:02d}#{epoch_to_sort_key(now)}",
        created_at=epoch_to_au(now),
        caller_number=event.get("caller_number"),
        verified=bool(event.get("verified", False)),
        policy_id=event.get("policy_id"),
        holder_name=event.get("holder_name"),
        transcript_tail=[event["note"]] if event.get("note") else [],
        channel="mcp",
        ttl=now + config.escalation_ttl_seconds,
    )
    EscalationRepository(config).save(record)
    logger.warning(
        "escalation queued (mcp tool): id=%s reason=%s session=%s",
        record.escalation_id,
        reason,
        record.session_id,
    )
    return {"escalation_id": record.escalation_id, "status": "queued"}


_DISPATCH = {
    "get_policy": _get_policy,
    "register_claim": _register_claim,
    "escalate": _escalate,
}


def lambda_handler(event: dict, context: Any) -> dict:
    """Dispatch one Gateway MCP tool call to its ``app.tools``/``app.data`` function.

    Args:
        event: The tool's raw input parameters, as sent by the Gateway. May also
            carry ``event["tool"]`` for Console-only testing -- see ``_tool_name``.
        context: Lambda context; ``context.client_context.custom`` carries the
            Gateway's ``bedrockAgentCoreToolName``.

    Returns:
        A JSON-serializable dict with the tool's result.

    Raises:
        ToolError: Unknown tool name, or a required input field is missing/invalid.
    """
    tool = _tool_name(event, context)
    handler = _DISPATCH.get(tool)
    if handler is None:
        logger.warning("mcp_tools: unknown tool requested: %s", tool)
        raise ToolError(f"unknown tool: {tool}")
    try:
        result = handler(load_config(), event)
    except (KeyError, ValueError) as exc:
        logger.warning("mcp_tools: %s rejected invalid input: %s", tool, exc)
        raise ToolError(f"{tool}: invalid input: {exc}") from exc
    logger.info("mcp_tools: %s ok", tool)
    return result
