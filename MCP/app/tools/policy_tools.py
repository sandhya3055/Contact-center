"""Phase-1 policy tools (BUILD_SPEC §8). [M1]

Typed, idempotent read tools the auth flow calls. They contain no branching decisions
themselves — they return facts; LangGraph decides what to do with them (rule #1).

The functions are synchronous (pure logic + a GSI read); the realtime pipeline must call
them off the audio loop (e.g. ``asyncio.to_thread``) so nothing blocks (CK §1.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.config import AppConfig
from app.data.counters import CounterRepository
from app.data.dynamo import PolicyRepository
from app.data.models import Policy


@dataclass(frozen=True)
class PolicyLookupResult:
    """Result of :func:`lookup_policy_by_number`.

    Attributes:
        found: Whether any policy exists for the number.
        has_active: Whether at least one policy is ACTIVE (expired ⇒ not active, rule #6).
        policies: All policies for the number (active and expired).
    """

    found: bool
    has_active: bool
    policies: list[Policy]

    @property
    def active_policies(self) -> list[Policy]:
        """The subset of policies that are currently active."""
        return [p for p in self.policies if p.is_active]


@dataclass(frozen=True)
class DobNameVerifyResult:
    """Result of :func:`verify_dob_name`.

    Attributes:
        verified: True only when an active policy matches both DOB and full name.
        policy_id: The matched policy id when verified, else None.
    """

    verified: bool
    policy_id: str | None = None


def lookup_policy_by_number(config: AppConfig, number: str) -> PolicyLookupResult:
    """Look up policies for a mobile number (BUILD_SPEC §8; uses ``mobileNo-index``).

    Args:
        config: Application configuration.
        number: Caller/alternate mobile number in stored format.

    Returns:
        A :class:`PolicyLookupResult`. Expired policies count as "no active policy".
    """
    policies = PolicyRepository(config).find_by_mobile(number)
    has_active = any(p.is_active for p in policies)
    return PolicyLookupResult(found=bool(policies), has_active=has_active, policies=policies)


def verify_dob_name(
    config: AppConfig, number: str, dob: str, full_name: str
) -> DobNameVerifyResult:
    """Verify a caller against an active policy by DOB + full name (alternate-number path).

    Matching is exact on DOB and case-insensitive/trimmed on name, against ACTIVE
    policies only (rule #6). Used solely on the alternate-number path (rule #7).

    Args:
        config: Application configuration.
        number: The mobile number whose active policy is being verified.
        dob: Caller-provided date of birth (ISO ``YYYY-MM-DD``).
        full_name: Caller-provided full name.

    Returns:
        A :class:`DobNameVerifyResult`.
    """
    want_dob = dob.strip()
    want_name = full_name.strip().casefold()
    for policy in PolicyRepository(config).find_by_mobile(number):
        if not policy.is_active:
            continue
        if policy.dob == want_dob and policy.holder_name.strip().casefold() == want_name:
            return DobNameVerifyResult(verified=True, policy_id=policy.policy_id)
    return DobNameVerifyResult(verified=False)


def mint_renewal_token(config: AppConfig, now_epoch: int) -> str:
    """Mint a `CLM-<date>-<seq>` token for a renewal (D12/rule #9).

    Shares the exact per-day counter with `claim_tools.create_claim_pending` (same
    `seq#YYYYMMDD` key) so claim and renewal references never collide on the same day —
    rule #9 specifies one token scheme for both.

    Args:
        config: Application configuration.
        now_epoch: Current time (epoch seconds) — deterministic in tests.

    Returns:
        The minted renewal reference, read back to the caller.
    """
    today = date.fromtimestamp(now_epoch)
    counter_id = f"seq#{today.strftime('%Y%m%d')}"
    seq = CounterRepository(config).next_seq(counter_id)
    return f"CLM-{today.strftime('%Y%m%d')}-{seq}"
