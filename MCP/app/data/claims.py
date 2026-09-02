"""Claim persistence: duplicate check, PENDING writes, and finalize (BUILD_SPEC §6/§7).

Pulled forward (D40) alongside the menu's "register a claim" option. Covers the full
claim lifecycle now: duplicate check, a PENDING write, a lookup by vehicle
registration, a lookup by token, and the conditional PENDING→REGISTERED flip the
async finalize worker calls after SQS delivery (D78/D79) — confirmation is SES
email + SNS SMS, not WhatsApp.
"""

from __future__ import annotations

from boto3.dynamodb.conditions import Attr, Key

from app.config import AppConfig
from app.data.dynamo import get_dynamo_resource
from app.data.models import Claim, ClaimStatus

_VEHICLE_INDEX = "vehicleReg-index"


class ClaimRepository:
    """Read/write access to the ``claims`` table."""

    def __init__(self, config: AppConfig) -> None:
        """Bind to the configured claims table + data mode."""
        self._config = config

    @property
    def _table(self):
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.claims_table)

    def has_open_claim(self, vehicle_reg: str) -> bool:
        """True if an **open** claim exists for ``vehicle_reg`` (duplicate check).

        Uses the ``vehicleReg-index`` GSI, never a scan (decision D15). Open means
        ``PENDING`` or ``REGISTERED`` — a claim being processed rightly blocks a second
        one (rule #3's duplicate handoff). ``CLOSED`` does not: BUILD_SPEC §6 specifies
        this check "filters ``vehicleReg-index`` on open status", and matching any row
        regardless of status locked a vehicle out of ever claiming again after its first
        claim — which is what it actually did to the seeded demo policy.

        Args:
            vehicle_reg: The policy's registered vehicle.

        Returns:
            True if at least one non-``CLOSED`` claim exists for that vehicle.
        """
        if not vehicle_reg:
            return False
        response = self._table.query(
            IndexName=_VEHICLE_INDEX,
            KeyConditionExpression=Key("vehicleReg").eq(vehicle_reg),
            FilterExpression=Attr("status").ne(ClaimStatus.CLOSED.value),
        )
        return bool(response.get("Items"))

    def save_pending(self, claim: Claim) -> None:
        """Write a new claim item (idempotent upsert by ``claimRef``)."""
        self._table.put_item(Item=claim.to_item())

    def find_by_vehicle(self, vehicle_reg: str) -> list[Claim]:
        """Return every claim for ``vehicle_reg`` via the ``vehicleReg-index`` GSI."""
        if not vehicle_reg:
            return []
        response = self._table.query(
            IndexName=_VEHICLE_INDEX,
            KeyConditionExpression=Key("vehicleReg").eq(vehicle_reg),
        )
        return [Claim.from_item(item) for item in response.get("Items", [])]

    def get(self, claim_ref: str) -> Claim | None:
        """Return the claim row, or ``None`` if it doesn't exist."""
        response = self._table.get_item(Key={"claimRef": claim_ref})
        item = response.get("Item")
        return Claim.from_item(item) if item else None

    def finalize(self, claim_ref: str) -> bool:
        """Conditionally flip ``PENDING`` -> ``REGISTERED`` (rule #8's async close-out).

        Mirrors ``AgentRepository.try_claim_agent``'s conditional-write shape — the
        primitive that makes the finalize worker idempotent: an SQS redelivery of an
        already-``REGISTERED`` claim is a clean no-op, not an error.

        Returns:
            ``True`` if this call performed the transition, ``False`` if the claim was
            not ``PENDING`` (already finalized).
        """
        try:
            self._table.update_item(
                Key={"claimRef": claim_ref},
                UpdateExpression="SET #s = :registered",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":registered": ClaimStatus.REGISTERED.value,
                    ":pending": ClaimStatus.PENDING.value,
                },
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def close(self, claim_ref: str) -> bool:
        """Mark a claim ``CLOSED`` so it stops blocking new claims on that vehicle.

        Terminal and unconditional-by-status (unlike :meth:`finalize`): a claim can be
        settled from either ``PENDING`` or ``REGISTERED``. Conditional on the row
        *existing*, so closing an unknown ``claimRef`` reports failure instead of
        silently creating a status-only item.

        Returns:
            ``True`` if the row existed and is now ``CLOSED``, ``False`` if there is no
            such claim.
        """
        try:
            self._table.update_item(
                Key={"claimRef": claim_ref},
                UpdateExpression="SET #s = :closed",
                ConditionExpression="attribute_exists(claimRef)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":closed": ClaimStatus.CLOSED.value},
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
