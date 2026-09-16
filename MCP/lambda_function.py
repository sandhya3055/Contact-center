"""MCP-gateway Lambda target wrapping ``app/tools`` for Bedrock AgentCore Gateway.

One Lambda, action-routed by ``context.client_context.custom["bedrockAgentCoreToolName"]``
(Gateway's Lambda-target contract passes the tool name as ``<target>___<tool>``; the
event itself is the tool's raw input). Mirrors the existing single-Lambda pattern
(``otp_start.py``) reaching into ``app.data``/``app.tools`` directly. Falls back to
``event["tool"]`` when that field is absent, solely so the Lambda Console's Test
feature (which has no way to set ``client_context``) can invoke a specific tool
directly -- see ``_tool_name``.

Four tools, deliberately scoped down from the full BUILD_SPEC §8 tool set (Opus
review, D103):

  - ``get_policy``: read-only, wraps ``lookup_policy_by_identity``. Requires all
    three of ``number``, ``dob`` and ``full_name``, and returns only the policies
    on that number whose stored date of birth and holder name both match, so a
    client can't enumerate the policies on a number it merely knows. A mismatch
    is reported identically to an unknown number (``found: false``, no policies),
    which is what stops the tool confirming whose a number is. Response whitelists
    ``Policy`` down to non-PII fields (``_POLICY_FIELDS``) -- ``dob``/``email``/
    ``mobile_no``/``sum_insured``/``premium`` never reach an external client (the
    input ``dob`` and ``full_name`` are used only to match, never echoed back).
    Each policy also carries ``open_claims`` (via ``list_claims_for_vehicle``,
    filtered to non-``CLOSED``) so a caller/agent can see a claim already in
    flight before attempting ``register_claim``. Expired policies are still
    returned (``has_active`` reports the distinction) because a renewal
    conversation is exactly about one.

    The docstring previously claimed the ``dob`` filter above while the code
    applied none -- ``number`` alone returned every policy on it. Requiring the
    name as well is the fix, and it is a **breaking input change**: the
    Gateway's declared ``inputSchema`` for this tool must add ``dob`` and
    ``full_name`` to ``properties``/``required`` in the same deployment, or the
    model will keep sending ``number`` alone and every lookup will fail as
    invalid input.
  - ``verify_dob``: read-only, the same DOB match ``get_policy`` performs, but
    answered on its own -- a caller/agent that has a number and a spoken date of
    birth can check whether the two belong together before asking ``get_policy``
    for any policy detail. Optionally also matches ``full_name``, in which case it
    delegates to ``verify_dob_name`` (ACTIVE policies only, name compared
    case-insensitively/trimmed -- rule #6/#7); DOB alone is matched across all
    policies on the number, exactly as ``get_policy`` filters. The response carries
    only ``verified``/``has_active``/``policy_ids`` -- never a name, DOB or any
    other policy field -- so a wrong guess confirms nothing beyond its own
    wrongness, and the tool can't be used to read a holder's details out of a
    number.
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

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict
from typing import Any

from app.config import AppConfig, load_config
from app.data.escalations import EscalationRepository
from app.data.sessions import SessionRepository
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
from app.tools.policy_tools import (
    lookup_policy_by_identity,
    lookup_policy_by_number,
    verify_dob_name,
)

logger = logging.getLogger(__name__)


# --- Structured logging -----------------------------------------------------
# This Lambda's log group (/aws/lambda/insurarncae-voice-agent-mcp-tool) is a
# different group from the agent's, and always will be — they're different
# services. What makes that acceptable rather than a blind spot is that both
# sides emit the same JSON shape with the same `session_id`, so one Logs
# Insights query across both groups reconstructs a call end to end: what Ava
# asked for, what the gate allowed, and what this Lambda actually did.
#
# `session_id` is the per-call uuid strands_agent.py injects into any tool
# whose schema requires one (_FixedArgumentTool), and it arrives here as an
# ordinary field of `event`. Nothing new has to be plumbed for this to work.
#
# Deliberately a module-level dict rather than a ContextVar: a Lambda
# execution environment handles exactly one invocation at a time, so there is
# no concurrency for a ContextVar to protect against, and this survives into
# the logging of an exception raised out of the handler.
_LOG_CONTEXT: dict[str, Any] = {}


class _JsonFormatter(logging.Formatter):
    """Same field names the agent's observability.py emits, minus the parts
    that only make sense in a long-lived process."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": "ava-mcp-tools",
            **_LOG_CONTEXT,
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in payload.items() if v is not None}, default=str)


def _configure_logging() -> None:
    """Reformat whatever handler the Lambda runtime already installed.

    The runtime adds its own handler to the root logger before this module is
    imported, and adding a second one would double every line. Replacing the
    formatter on the existing handlers keeps exactly one line per log call.
    """
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    for handler in root.handlers or ():
        handler.setFormatter(_JsonFormatter())
    if not root.handlers:  # local test runs, no runtime handler to reuse
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)


_configure_logging()


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


def _validate_full_name(full_name: Any) -> str | None:
    """Validate the optional ``full_name`` field; ``None`` when not supplied.

    An empty/whitespace-only string is treated as "not supplied" rather than an
    error -- an STT transcript can easily yield one, and the DOB-only check is a
    valid answer to that -- but a non-string is a malformed call.
    """
    if full_name is None:
        return None
    if not isinstance(full_name, str):
        raise ValueError("invalid full_name")
    return full_name.strip() or None


def _require_full_name(full_name: Any) -> str:
    """Validate a ``full_name`` that the tool cannot proceed without.

    The lenient reading above -- blank means "not supplied" -- is right for
    ``verify_dob``, which has a narrower check to fall back on. ``get_policy``
    has none: a blank name would match no stored holder, so accepting it would
    turn a malformed call into a "no policy found", and the caller would be
    asked for a different mobile number to fix a problem the number never had.
    """
    name = _validate_full_name(full_name)
    if name is None:
        raise ValueError("invalid full_name")
    return name


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
    """Look up the policies a number, date of birth and full name all agree on.

    All three are required. A number on its own reaches every policy stored
    against it, which made this readable by anyone who knew (or guessed) the
    number; requiring the holder's date of birth and name is what makes the
    lookup identify the caller rather than merely find them.

    Nothing distinguishes "no such number" from "that number's holder has a
    different date of birth or name" in the response: both are ``found: false``
    with no policies. That is the point -- a mismatch must not confirm that the
    number itself is real, or whose it is.
    """
    number = _validate_number(event.get("number"))
    dob = _validate_dob(event.get("dob"))
    full_name = _require_full_name(event.get("full_name"))
    result = lookup_policy_by_identity(config, number, dob, full_name)
    return {
        "found": result.found,
        "has_active": result.has_active,
        "policies": [_policy_view(config, p) for p in result.policies],
    }


def _verify_dob(config: AppConfig, event: dict) -> dict:
    """Check a number + date of birth (optionally + full name) belong together.

    Returns identifiers only -- no policy field is echoed back, so a failed guess
    learns nothing about the real holder (see the module docstring). ``policy_ids``
    is empty whenever ``verified`` is False.
    """
    number = _validate_number(event.get("number"))
    dob = _validate_dob(event.get("dob"))
    full_name = _validate_full_name(event.get("full_name"))

    if full_name is not None:
        # Name supplied: the stricter ACTIVE-only, name-matched check, which
        # already returns at most one policy.
        result = verify_dob_name(config, number, dob, full_name)
        return {
            "verified": result.verified,
            # verify_dob_name only ever matches an ACTIVE policy, so a match here
            # is by definition active.
            "has_active": result.verified,
            "policy_ids": [result.policy_id] if result.policy_id else [],
        }

    matched = [p for p in lookup_policy_by_number(config, number).policies if p.dob == dob]
    return {
        "verified": bool(matched),
        "has_active": any(p.is_active for p in matched),
        "policy_ids": [p.policy_id for p in matched],
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
        config,
        event["policy_id"],
        slots,
        int(time.time()),
        session_id=event.get("session_id"),
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
        reason=reason,
        status=EscalationStatus.QUEUED,
        queue_key=f"{_QUEUE_PRIORITY:02d}#{epoch_to_sort_key(now)}",
        created_at=epoch_to_au(now),
        caller_number=event.get("caller_number"),
        verified=bool(event.get("verified", False)),
        policy_id=event.get("policy_id"),
        holder_name=event.get("holder_name"),
        transcript_tail=[event["note"]] if event.get("note") else [],
        channel="voice",
        ttl=now + config.escalation_ttl_seconds,
        session_id=event.get("session_id"),
    )
    EscalationRepository(config).save(record)
    logger.warning(
        "escalation queued (mcp tool): id=%s reason=%s",
        record.escalation_id,
        reason,
    )
    return {"escalation_id": record.escalation_id, "status": "queued"}


_DISPATCH = {
    "get_policy": _get_policy,
    "verify_dob": _verify_dob,
    "register_claim": _register_claim,
    "escalate": _escalate,
}


# --- Pre-check --------------------------------------------------------------
# Every field a tool cannot run without. Checked ahead of dispatch rather than
# inside each tool for two reasons: a malformed call is refused before it can
# do half its work (register_claim's duplicate check used to run against a
# vehicle_reg before anything had established the rest of the call was sound),
# and the error names *every* missing field at once instead of whichever one
# the tool body happened to touch first — the model gets one round trip to fix
# the call rather than one per field.
#
# Derived from what each tool actually subscripts, not from the Gateway's
# declared schema: the schema is what the model is told, this is what the code
# requires, and the point of the check is to catch the two drifting apart.
#
# `escalate` is deliberately absent. Its `reason` has a documented default
# ("caller_requested") and a handoff must never fail because a model omitted a
# field — a caller asking for a person gets one either way.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "get_policy": ("number", "dob", "full_name"),
    "verify_dob": ("number", "dob"),
    "register_claim": (
        "policy_id",
        "vehicle_reg",
        "claim_type",
        "incident_date",
        "incident_location",
        "description"
    ),
}


# What a model sends for a field it has not collected yet. A JSON `null` is
# only the most obvious form: asked for a string, a model that has nothing to
# put there will as readily send "null", "N/A", "unknown", or the schema's own
# type name back as a literal. All of them mean the same thing — no value — and
# all of them would otherwise be matched against stored data as if they were
# one, turning "I never asked the caller" into "no policy found".
#
# Matched case-insensitively against the stripped value, after punctuation that
# carries no meaning on its own is discarded.
_PLACEHOLDER_VALUES = frozenset(
    {
        "null",
        "none",
        "nil",
        "nan",
        "undefined",
        "unknown",
        "n/a",
        "na",
        "not provided",
        "not given",
        "not available",
        "not applicable",
        "notprovided",
        "missing",
        "empty",
        "blank",
        "tbd",
        "tba",
        "pending",
        "string",
        "value",
        "placeholder",
        "example",
        "xxx",
        "xxxx",
        "test",
    }
)

# Punctuation-only answers ("-", "?", "--") mean the same thing and are not
# worth enumerating individually.
_PUNCTUATION_ONLY = frozenset("-_.?*/\\ ")


def _is_blank(value: Any) -> bool:
    """Whether a supplied field carries no real value.

    Four shapes, all meaning "not collected": absent or ``null``; a string of
    nothing but whitespace; an empty collection; and a placeholder a model
    substitutes for a value it does not have — a literal ``"null"``, ``"N/A"``,
    ``"unknown"``, the schema's own type name, or punctuation on its own.

    Deliberately **not** falsiness: ``0`` and ``False`` are real answers, and
    ``tow_required: false`` must never be read as a missing field.

    The placeholder list is a floor, not a guarantee — it cannot catch an
    invented value that merely looks real, which is what the per-field
    validators (``_validate_number``, ``_validate_dob``) and the agent-side
    grounding gate are for.
    """
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        if all(character in _PUNCTUATION_ONLY for character in stripped):
            return True
        return stripped.casefold() in _PLACEHOLDER_VALUES
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _precheck(tool: str, event: dict) -> None:
    """Refuse a call whose required fields are missing or empty.

    Raises:
        ValueError: naming every field that is absent or carries no value.
    """
    missing = [
        field for field in _REQUIRED_FIELDS.get(tool, ()) if _is_blank(event.get(field))
    ]
    if missing:
        raise ValueError(f"missing or empty required field(s): {', '.join(missing)}")


# --- Post-check -------------------------------------------------------------
# Every tool's result carries the same `checks` block: which tools have run on
# this call so far, this one included. It is what lets the agent answer from
# what actually happened rather than from what it believes happened — the model
# cannot decide it already looked a policy up when it did not.
#
# Keyed by tool name, valued by the flag each one sets.
_CHECK_FLAGS = {
    "get_policy": "isGetPolicyCalled",
    "verify_dob": "isVerifyDobCalled",
    "register_claim": "isRegisterClaimCalled",
    "escalate": "isEscalateCalled",
}


def _postcheck(config: AppConfig, tool: str, event: dict, result: dict) -> dict:
    """Attach this call's tool-call flags to a result that already succeeded.

    Never raises. The tool has done its work by the time this runs — a claim is
    registered, an escalation is queued — and failing the call now would report
    that work as not having happened, which is the one outcome worse than
    flags that are merely incomplete. A bookkeeping failure degrades to "only
    this tool ran", logged, and the result still goes back.
    """
    session_id = event.get("session_id")
    called = {tool}
    if session_id:
        try:
            called = SessionRepository(config).record_tool(str(session_id), tool)
        except Exception as exc:  # noqa: BLE001 — see docstring
            logger.warning(
                "tool-check bookkeeping failed",
                extra={
                    "fields": {
                        "event_name": "tool.checks_unavailable",
                        "reason": str(exc),
                    }
                },
            )
    else:
        # No session id: a Console test call, or a tool whose declared schema
        # doesn't carry one. The flags still go back, scoped to this call
        # alone, so the response shape never depends on how it was invoked.
        logger.info(
            "no session id; tool checks are this call only",
            extra={"fields": {"event_name": "tool.checks_unscoped"}},
        )
    return {**result, "checks": {flag: name in called for name, flag in _CHECK_FLAGS.items()}}


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

    # Set before the first log line of the invocation and rebuilt (not
    # updated) each time, so a reused execution environment can't leak the
    # previous call's session id onto this one's logs.
    _LOG_CONTEXT.clear()
    _LOG_CONTEXT.update(
        {
            "tool": tool,
            # The agent's per-call id, arriving as an ordinary tool argument.
            # Absent for a Console test call or a tool whose schema doesn't
            # declare it, which is why it's read defensively.
            "session_id": event.get("session_id") if isinstance(event, dict) else None,
            "aws_request_id": getattr(context, "aws_request_id", None),
        }
    )

    started = time.monotonic()
    handler = _DISPATCH.get(tool)
    if handler is None:
        logger.warning("unknown tool requested", extra={"fields": {"event_name": "tool.unknown"}})
        raise ToolError(f"unknown tool: {tool}")
    config = load_config()
    try:
        _precheck(tool, event)
        result = handler(config, event)
    except (KeyError, ValueError) as exc:
        logger.warning(
            "rejected invalid input",
            extra={"fields": {"event_name": "tool.invalid_input", "reason": str(exc)}},
        )
        raise ToolError(f"{tool}: invalid input: {exc}") from exc
    # After the handler, never around it: the flags describe tools that *ran*,
    # so a call rejected above must not leave one set.
    result = _postcheck(config, tool, event, result)
    logger.info(
        "tool ok",
        extra={
            "fields": {
                "event_name": "tool.ok",
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        },
    )
    return result
