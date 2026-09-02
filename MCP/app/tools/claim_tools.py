"""Phase-3 claim tools (BUILD_SPEC §8) — minimal slice pulled forward (D40).

Typed, idempotent functions the register-claim flow calls. They contain no branching
themselves — they return facts or write records; LangGraph decides what happens with
them (rule #1). Only the synchronous half of Phase 3 is implemented here: duplicate
check + a PENDING write + the ``CLM-<date>-<seq>`` token (rule #9). The async
close-out — SQS enqueue (``app/agent/wiring.py``'s ``enqueue_claim``), the
``services/lambdas/claim_finalize.py`` worker, and SES/SNS confirmation
(``app/notify/``, not WhatsApp) — is built and lives elsewhere (D78/D79).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.config import AppConfig
from app.data.claims import ClaimRepository
from app.data.counters import CounterRepository
from app.data.models import Claim, ClaimStatus, ClaimType


@dataclass(frozen=True)
class ClaimSlots:
    """Slots captured for a new claim, ready for read-back + persistence.

    ``garage_name`` and ``description`` are captured for both claim types (D40
    extension); ``tow_required`` is CASHLESS-only and ``estimated_cost`` is
    REFUND-only — the other is always ``None`` on a given claim.

    Deliberately not validated here: ``garage_name`` (the literal ``"not yet chosen"`` is
    a legitimate value written by ``offer_options``' ``carry_on_without`` branch), and
    non-answer text (the read-back guard in ``claim_readback`` is the real defence —
    raising from here after the caller has answered six questions and confirmed the
    read-back would end the call with "something went wrong", a worse outcome than the
    bug).

    Raises:
        ValueError: If ``vehicle_reg``, ``incident_date``, or ``incident_location`` is
            blank.
    """

    claim_type: ClaimType
    vehicle_reg: str
    incident_date: str
    incident_location: str
    garage_name: str = ""
    tow_required: bool | None = None
    estimated_cost: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        """Validate required claim slot values."""
        if not self.vehicle_reg or not self.vehicle_reg.strip():
            raise ValueError("vehicle_reg cannot be blank")
        if not self.incident_date or not self.incident_date.strip():
            raise ValueError("incident_date cannot be blank")
        if not self.incident_location or not self.incident_location.strip():
            raise ValueError("incident_location cannot be blank")


def check_duplicate_claim(config: AppConfig, vehicle_reg: str) -> bool:
    """Return True if ``vehicle_reg`` already has an open claim (BUILD_SPEC §8).

    A duplicate open claim is one of the standing human-handoff triggers (rule #3).
    """
    return ClaimRepository(config).has_open_claim(vehicle_reg)


def create_claim_pending(
    config: AppConfig, policy_id: str, slots: ClaimSlots, now_epoch: int
) -> str:
    """Mint a ``CLM-<date>-<seq>`` token (D12) and write the claim as PENDING (rule #8).

    Args:
        config: Application configuration.
        policy_id: The verified caller's policy id.
        slots: Captured + read-back-confirmed claim details.
        now_epoch: Current time (epoch seconds) — injected so the token's date segment
            and the claim's ``created_at`` are deterministic and unit-testable.

    Returns:
        The minted claim reference, read back to the caller as "logged" (rule #8) —
        finalization to REGISTERED is async future work.
    """
    today = date.fromtimestamp(now_epoch)
    counter_id = f"seq#{today.strftime('%Y%m%d')}"
    seq = CounterRepository(config).next_seq(counter_id)
    claim_ref = f"CLM-{today.strftime('%Y%m%d')}-{seq}"
    claim = Claim(
        claim_ref=claim_ref,
        policy_id=policy_id,
        vehicle_reg=slots.vehicle_reg,
        claim_type=slots.claim_type,
        status=ClaimStatus.PENDING,
        incident_date=slots.incident_date,
        incident_location=slots.incident_location,
        created_at=now_epoch,
        garage_name=slots.garage_name,
        tow_required=slots.tow_required,
        estimated_cost=slots.estimated_cost,
        description=slots.description,
    )
    ClaimRepository(config).save_pending(claim)
    return claim_ref


def list_claims_for_vehicle(config: AppConfig, vehicle_reg: str) -> list[Claim]:
    """Return every claim on ``vehicle_reg`` (menu's "claim status" option, BUILD_SPEC §8).

    In practice there is at most one: the duplicate-claim gate blocks a second claim
    while one is open, and there is no closed/settled status yet.
    """
    return ClaimRepository(config).find_by_vehicle(vehicle_reg)
