"""Typed data models mirroring the DynamoDB schema (BUILD_SPEC §6). [M1]

Kept as plain dataclasses so the flow/tools work with typed objects, not raw dicts.
The DynamoDB item shape maps 1:1 to these fields.

All caller-facing policy questions (M3 "policy details") are answered from these
structured fields — there is no document/RAG store (decision: structured DB only). Any
fact a caller might ask about must therefore live here as a field. Derived values a
caller cares about (e.g. days to expiry) are computed deterministically in code, never
by the LLM (the model only phrases the number we hand it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

_ISO_DATE_FMT = "%Y-%m-%d"
_AU_DATETIME_FMT = "%d/%m/%Y %H:%M:%S %Z"
_AU_TZ = ZoneInfo("Australia/Sydney")


def epoch_to_au(epoch_seconds: int) -> str:
    """Format an epoch-seconds "now" as an AU (Sydney-local) datetime string, e.g.
    "02/09/2026 14:30:00 AEST" — what every ``created_at``/``last_heartbeat``/
    ``claimed_at`` field below stores instead of a raw epoch int.

    Callers still inject the current time as an epoch int (``now_epoch``,
    ``int(time.time())``) for the same determinism/testability reason as
    before — this is only the formatting step at the point of storage.

    Not lexicographically sortable (``DD/MM/YYYY`` doesn't sort chronologically,
    and the Sydney clock also falls back an hour at the AEDT->AEST DST
    transition, so even same-format local time isn't reliably monotonic). Never
    build a sort key out of this — see ``epoch_to_sort_key`` for
    ``EscalationRecord.queue_key``.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=_AU_TZ).strftime(_AU_DATETIME_FMT)


def epoch_to_sort_key(epoch_seconds: int) -> str:
    """Zero-padded epoch-seconds string, for lexicographic ordering only.

    Deliberately decoupled from ``epoch_to_au``: the human-facing AU display
    format isn't sortable (see there), so anything that needs chronological
    ordering as plain text — currently just ``EscalationRecord.queue_key`` —
    builds its sort key from the raw epoch here instead of from a record's
    (AU-formatted) ``created_at``.
    """
    return f"{epoch_seconds:011d}"


class PolicyStatus(StrEnum):
    """Policy lifecycle status. Expired counts as "no active policy" (rule #6)."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


class ClaimType(StrEnum):
    """Register-claim type (BUILD_SPEC §5.3), pulled forward as a minimal slice (D40)."""

    CASHLESS = "CASHLESS"
    REFUND = "REFUND"


class ClaimStatus(StrEnum):
    """Claim lifecycle status.

    ``PENDING`` is written synchronously on the call; the async worker finalizes it to
    ``REGISTERED`` (rule #8). ``CLOSED`` is terminal and exists for exactly one reason:
    BUILD_SPEC §6 defines the duplicate check as filtering the vehicle GSI on **open**
    status, and without a non-open state every claim ever raised blocks that vehicle
    from ever claiming again. Settling a claim happens outside this system (no adjuster
    workflow here), so ``scripts/close_claim.py`` is what sets it.
    """

    PENDING = "PENDING"
    REGISTERED = "REGISTERED"
    CLOSED = "CLOSED"


class EscalationStatus(StrEnum):
    """Escalation lifecycle status (``docs/TARGET_ARCHITECTURE.md`` §5). Every
    escalation is written ``QUEUED``; the later states belong to Milestones F/G."""

    QUEUED = "QUEUED"
    OFFERED = "OFFERED"
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    ABANDONED = "ABANDONED"
    DEFLECTED = "DEFLECTED"


class OtpStatus(StrEnum):
    """Email-OTP challenge lifecycle status (Milestone E1, D61)."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class Policy:
    """A vehicle-insurance policy record (``policies`` table).

    The first block of fields is the auth-critical set present since M1. The second
    block is descriptive detail used to answer caller questions in Phase 2 (M3); these
    are optional so historical items (and auth-only tests) still parse cleanly.
    """

    # Identity + auth (required)
    policy_id: str
    mobile_no: str
    status: PolicyStatus
    vehicle_reg: str
    holder_name: str
    dob: str  # ISO date string, e.g. "1990-01-15"
    expiry_date: str  # ISO date string

    # Descriptive detail for policy-Q&A (optional; blank when unknown)
    start_date: str = ""
    policy_type: str = ""  # e.g. "Comprehensive", "Third Party"
    vehicle_make: str = ""
    vehicle_model: str = ""
    vehicle_year: str = ""
    sum_insured: str = ""  # stored as string to avoid float drift; e.g. "25000"
    premium: str = ""  # e.g. "980.50"
    currency: str = "AUD"
    email: str = ""  # verification-OTP recipient (Milestone E1); blank = no OTP possible

    @property
    def is_active(self) -> bool:
        """True only when the policy is ACTIVE (expired ⇒ treated as no active policy)."""
        return self.status is PolicyStatus.ACTIVE

    def days_to_expiry(self, today: date) -> int | None:
        """Whole days from ``today`` until ``expiry_date`` (negative if already past).

        The reference date is injected (never read from the clock here) so the value is
        deterministic and unit-testable, and so the LLM never does date arithmetic.

        Args:
            today: Reference "current" date supplied by the caller of this method.

        Returns:
            Signed day count, or ``None`` if ``expiry_date`` is missing/unparseable.
        """
        if not self.expiry_date:
            return None
        try:
            expiry = date.fromisoformat(self.expiry_date)
        except ValueError:
            return None
        return (expiry - today).days

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Policy:
        """Build a :class:`Policy` from a DynamoDB item (resource-level, plain dict).

        Required auth fields must be present; descriptive fields default to blank so
        partially-populated items remain valid.
        """
        return cls(
            policy_id=item["policyId"],
            mobile_no=item["mobileNo"],
            status=PolicyStatus(item["status"]),
            vehicle_reg=item["vehicleReg"],
            holder_name=item["holderName"],
            dob=item["dob"],
            expiry_date=item["expiryDate"],
            start_date=item.get("startDate", ""),
            policy_type=item.get("policyType", ""),
            vehicle_make=item.get("vehicleMake", ""),
            vehicle_model=item.get("vehicleModel", ""),
            vehicle_year=str(item.get("vehicleYear", "")),
            sum_insured=str(item.get("sumInsured", "")),
            premium=str(item.get("premium", "")),
            currency=item.get("currency", "AUD"),
            email=item.get("email", ""),
        )


@dataclass(frozen=True)
class Claim:
    """A register-claim record (``claims`` table), minimal Phase-3 slice (D40).

    Fields needed for the synchronous half of claim registration (duplicate check,
    fixed read-back, token) plus the type-specific slots added in the Slice B
    extension: ``garage_name`` (both claim types), ``tow_required`` (CASHLESS only),
    ``estimated_cost`` (REFUND only; a string like ``Policy.sum_insured``/``premium``
    to avoid float drift). ``description`` is the caller's free-text account of the
    incident, captured for both claim types.
    """

    claim_ref: str  # "CLM-<YYYYMMDD>-<seq>" (D12)
    policy_id: str
    vehicle_reg: str
    claim_type: ClaimType
    status: ClaimStatus
    incident_date: str  # ISO date string
    incident_location: str
    created_at: str  # AU (Sydney-local) datetime, e.g. "02/09/2026 14:30:00 AEST"
    garage_name: str = ""
    tow_required: bool | None = None
    estimated_cost: str | None = None
    description: str = ""

    def to_item(self) -> dict[str, Any]:
        """Serialise to a DynamoDB item (omitting the two type-specific fields when unset)."""
        item: dict[str, Any] = {
            "claimRef": self.claim_ref,
            "policyId": self.policy_id,
            "vehicleReg": self.vehicle_reg,
            "claimType": self.claim_type.value,
            "status": self.status.value,
            "incidentDate": self.incident_date,
            "incidentLocation": self.incident_location,
            "createdAt": self.created_at,
            "garageName": self.garage_name,
            "description": self.description,
        }
        if self.tow_required is not None:
            item["towRequired"] = self.tow_required
        if self.estimated_cost is not None:
            item["estimatedCost"] = self.estimated_cost
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Claim:
        """Build a :class:`Claim` from a DynamoDB item."""
        return cls(
            claim_ref=item["claimRef"],
            policy_id=item["policyId"],
            vehicle_reg=item["vehicleReg"],
            claim_type=ClaimType(item["claimType"]),
            status=ClaimStatus(item["status"]),
            incident_date=item.get("incidentDate", ""),
            incident_location=item.get("incidentLocation", ""),
            created_at=str(item.get("createdAt", "")),
            garage_name=item.get("garageName", ""),
            tow_required=item.get("towRequired"),
            estimated_cost=item.get("estimatedCost"),
            description=item.get("description", ""),
        )


@dataclass(frozen=True)
class EscalationRecord:
    """A human-handoff queue entry (``escalations`` table, Milestone D/D2).

    Mirrors :class:`~app.escalation.base.EscalationContext` field-for-field — this is
    that context's persisted shape. ``reason`` and ``status`` are stored as plain
    strings/values rather than importing :class:`~app.flow.state.HandoffReason` here,
    keeping the data layer reason-agnostic (it stores whatever it's given).
    """

    escalation_id: str
    reason: str  # a HandoffReason value
    status: EscalationStatus
    queue_key: str  # status-index range key: f"{priority:02d}#{epoch_to_sort_key(now)}" (not created_at -- that's AU-formatted and not sortable)
    created_at: str  # AU (Sydney-local) datetime, e.g. "02/09/2026 14:30:00 AEST"
    caller_number: str | None
    verified: bool
    policy_id: str | None
    holder_name: str | None
    captured_slots: dict[str, str | None] = field(default_factory=dict)
    transcript_tail: list[str] = field(default_factory=list)
    channel: str = ""
    channel_ref: str = ""
    ttl: int = 0  # epoch seconds -- DynamoDB TTL requires this, must stay a raw epoch int
    assigned_agent_id: str | None = None  # agent offered/claimed this row (Milestone F)
    claimed_at: str | None = None  # AU (Sydney-local) datetime; set by EscalationRepository.claim()

    def to_item(self) -> dict[str, Any]:
        """Serialise to a DynamoDB item (omitting ``None`` optional fields)."""
        item: dict[str, Any] = {
            "escalationId": self.escalation_id,
            "reason": self.reason,
            "status": self.status.value,
            "queueKey": self.queue_key,
            "createdAt": self.created_at,
            "verified": self.verified,
            "channel": self.channel,
            "channelRef": self.channel_ref,
            "ttl": self.ttl,
        }
        if self.caller_number is not None:
            item["callerNumber"] = self.caller_number
        if self.policy_id is not None:
            item["policyId"] = self.policy_id
        if self.holder_name is not None:
            item["holderName"] = self.holder_name
        if self.captured_slots:
            item["capturedSlots"] = self.captured_slots
        if self.transcript_tail:
            item["transcriptTail"] = list(self.transcript_tail)
        if self.assigned_agent_id is not None:
            item["assignedAgentId"] = self.assigned_agent_id
        if self.claimed_at is not None:
            item["claimedAt"] = self.claimed_at
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> EscalationRecord:
        """Build an :class:`EscalationRecord` from a DynamoDB item."""
        return cls(
            escalation_id=item["escalationId"],
            reason=item["reason"],
            status=EscalationStatus(item["status"]),
            queue_key=item["queueKey"],
            created_at=str(item.get("createdAt", "")),
            caller_number=item.get("callerNumber"),
            verified=bool(item.get("verified", False)),
            policy_id=item.get("policyId"),
            holder_name=item.get("holderName"),
            captured_slots=dict(item.get("capturedSlots") or {}),
            transcript_tail=list(item.get("transcriptTail") or []),
            channel=item.get("channel", ""),
            channel_ref=item.get("channelRef", ""),
            ttl=int(item.get("ttl", 0)),
            assigned_agent_id=item.get("assignedAgentId"),
            claimed_at=item.get("claimedAt"),
        )


class AgentStatus(StrEnum):
    """Human-agent presence status (``agents`` table, Milestone F)."""

    AVAILABLE = "AVAILABLE"
    OFFERED = "OFFERED"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True)
class AgentRecord:
    """A human agent's live presence row (``agents`` table, Milestone F).

    ``connection_id`` is the API Gateway WebSocket connectionId, filled in by a
    later slice's bridge once the agent's console connects.
    """

    agent_id: str
    status: AgentStatus
    last_heartbeat: str  # AU (Sydney-local) datetime, e.g. "02/09/2026 14:30:00 AEST"
    connection_id: str | None = None
    current_escalation_id: str | None = None
    ttl: int = 0  # epoch seconds -- DynamoDB TTL requires this, must stay a raw epoch int

    def to_item(self) -> dict[str, Any]:
        """Serialise to a DynamoDB item (omitting ``None`` optional fields)."""
        item: dict[str, Any] = {
            "agentId": self.agent_id,
            "status": self.status.value,
            "lastHeartbeat": self.last_heartbeat,
            "ttl": self.ttl,
        }
        if self.connection_id is not None:
            item["connectionId"] = self.connection_id
        if self.current_escalation_id is not None:
            item["currentEscalationId"] = self.current_escalation_id
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> AgentRecord:
        """Build an :class:`AgentRecord` from a DynamoDB item."""
        return cls(
            agent_id=item["agentId"],
            status=AgentStatus(item["status"]),
            last_heartbeat=str(item.get("lastHeartbeat", "")),
            connection_id=item.get("connectionId"),
            current_escalation_id=item.get("currentEscalationId"),
            ttl=int(item.get("ttl", 0)),
        )


class CallbackStatus(StrEnum):
    """Callback-request lifecycle status (``callbacks`` table, Milestone F)."""

    PENDING = "PENDING"
    DONE = "DONE"


@dataclass(frozen=True)
class CallbackRecord:
    """A caller callback request (``callbacks`` table, Milestone F)."""

    callback_id: str
    mobile_no: str
    reason: str  # a HandoffReason value
    created_at: str  # AU (Sydney-local) datetime, e.g. "02/09/2026 14:30:00 AEST"
    status: CallbackStatus
    ttl: int = 0  # epoch seconds -- DynamoDB TTL requires this, must stay a raw epoch int

    def to_item(self) -> dict[str, Any]:
        """Serialise to a DynamoDB item."""
        return {
            "callbackId": self.callback_id,
            "mobileNo": self.mobile_no,
            "reason": self.reason,
            "createdAt": self.created_at,
            "status": self.status.value,
            "ttl": self.ttl,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> CallbackRecord:
        """Build a :class:`CallbackRecord` from a DynamoDB item."""
        return cls(
            callback_id=item["callbackId"],
            mobile_no=item["mobileNo"],
            reason=item["reason"],
            created_at=str(item.get("createdAt", "")),
            status=CallbackStatus(item["status"]),
            ttl=int(item.get("ttl", 0)),
        )


@dataclass(frozen=True)
class OtpChallenge:
    """An email-OTP verification challenge (``otp_challenges`` table, Milestone E1).

    The code itself is never stored — only a salted SHA-256 hash (``code_hash``),
    compared with :func:`hmac.compare_digest` at verify time (never ``==``). ``email_hash``
    likewise stores a hash rather than the address itself: the address already lives on
    the policy row and does not need a second copy here.
    """

    challenge_id: str
    mobile_no: str
    email_hash: str
    code_hash: str
    code_salt: str
    status: OtpStatus
    attempts: int = 0
    created_at: str = ""  # AU (Sydney-local) datetime, e.g. "02/09/2026 14:30:00 AEST"
    ttl: int = 0  # epoch seconds -- DynamoDB TTL requires this, must stay a raw epoch int

    def to_item(self) -> dict[str, Any]:
        """Serialise to a DynamoDB item."""
        return {
            "challengeId": self.challenge_id,
            "mobileNo": self.mobile_no,
            "emailHash": self.email_hash,
            "codeHash": self.code_hash,
            "codeSalt": self.code_salt,
            "status": self.status.value,
            "attempts": self.attempts,
            "createdAt": self.created_at,
            "ttl": self.ttl,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> OtpChallenge:
        """Build an :class:`OtpChallenge` from a DynamoDB item."""
        return cls(
            challenge_id=item["challengeId"],
            mobile_no=item["mobileNo"],
            email_hash=item["emailHash"],
            code_hash=item["codeHash"],
            code_salt=item["codeSalt"],
            status=OtpStatus(item["status"]),
            attempts=int(item.get("attempts", 0)),
            created_at=str(item.get("createdAt", "")),
            ttl=int(item.get("ttl", 0)),
        )
