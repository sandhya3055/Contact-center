"""Escalation queue persistence: the human-handoff queue (Milestone D). [D2]

The ``status-index`` GSI (HASH ``status``, RANGE ``queueKey``) is what lets
Milestone F's assignment Lambda pull the queue in priority order without ever
scanning the table (D15). ``queueKey = f"{priority:02d}#{epoch_to_sort_key(now)}"``
— a zero-padded epoch string (see ``app.data.models.epoch_to_sort_key``), fixed-width
and monotonic, so it still sorts correctly as plain text — an ascending sort naturally
yields highest priority first, oldest first within a priority — the FIFO-within-priority
ordering an ACD needs. Deliberately *not* built from ``created_at`` itself: that field is
now an AU (Sydney-local) display string (``epoch_to_au``), which is neither
lexicographically sortable (``DD/MM/YYYY``) nor guaranteed monotonic (Sydney's clock
falls back an hour at the AEDT->AEST transition).

The reason→priority mapping that builds that key lives in
:mod:`app.escalation.priority`, not here: it is escalation policy rather than table
schema, and ``app/data/`` is the bottom layer — importing ``HandoffReason`` here would
make ``app.data`` depend on ``app.flow`` → ``app.tools`` → back on ``app.data``.
"""

from __future__ import annotations

from boto3.dynamodb.conditions import Key

from app.config import AppConfig
from app.data.dynamo import get_dynamo_resource
from app.data.models import EscalationRecord, EscalationStatus, epoch_to_au

_STATUS_INDEX = "status-index"


class EscalationRepository:
    """Read/write access to the ``escalations`` table."""

    def __init__(self, config: AppConfig) -> None:
        """Bind to the configured escalations table + data mode."""
        self._config = config

    @property
    def _table(self):
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.escalations_table)

    def save(self, record: EscalationRecord) -> None:
        """Upsert an escalation record (idempotent by ``escalationId``)."""
        self._table.put_item(Item=record.to_item())

    def get(self, escalation_id: str) -> EscalationRecord | None:
        """Return the escalation row, or ``None`` if it doesn't exist.

        Added in Milestone F: ``agent_ws.py``'s ``accept`` route needs the row's
        ``channelRef`` to presign the agent's session URL, and ``requeue_escalation.py``
        needs ``assignedAgentId`` before its own ``requeue()`` call clears it. Mirrors
        :meth:`app.data.agents.AgentRepository.get` — a plain ``GetItem``, no condition.
        """
        response = self._table.get_item(Key={"escalationId": escalation_id})
        item = response.get("Item")
        return EscalationRecord.from_item(item) if item else None

    def find_by_status(self, status: EscalationStatus) -> list[EscalationRecord]:
        """Return escalations in ``status``, highest priority then oldest first.

        Uses the ``status-index`` GSI, never a scan (D15) — Milestone F's assignment
        Lambda queries this directly.
        """
        response = self._table.query(
            IndexName=_STATUS_INDEX,
            KeyConditionExpression=Key("status").eq(status.value),
        )
        return [EscalationRecord.from_item(item) for item in response.get("Items", [])]

    def offer(self, escalation_id: str, agent_id: str) -> bool:
        """Conditionally offer a ``QUEUED`` escalation to ``agent_id`` (-> ``OFFERED``).

        Args:
            escalation_id: Row to offer.
            agent_id: Agent the row is being offered to; recorded as ``assignedAgentId``.

        Returns:
            ``True`` if this call performed the transition, ``False`` if the row was
            not ``QUEUED`` (already offered/claimed by someone else).
        """
        try:
            self._table.update_item(
                Key={"escalationId": escalation_id},
                UpdateExpression="SET #s = :offered, assignedAgentId = :agent",
                ConditionExpression="#s = :queued",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":offered": EscalationStatus.OFFERED.value,
                    ":queued": EscalationStatus.QUEUED.value,
                    ":agent": agent_id,
                },
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def claim(self, escalation_id: str, agent_id: str, now: int) -> bool:
        """Conditionally claim a ``QUEUED``/``OFFERED`` escalation (-> ``ACTIVE``).

        Race-free: this is the primitive that stops two agents taking the same
        caller — only one ``claim()`` call for a given row can ever succeed.

        Args:
            escalation_id: Row to claim.
            agent_id: Claiming agent; recorded as ``assignedAgentId``.
            now: Epoch seconds (injected for determinism/testability, same pattern as
                ``now_epoch`` elsewhere); formatted to an AU (Sydney-local) string before
                being recorded as ``claimedAt`` — the epoch int itself is never
                persisted.

        Returns:
            ``True`` if this call performed the transition, ``False`` if the row was
            not ``QUEUED``/``OFFERED`` (already claimed by someone else).
        """
        try:
            self._table.update_item(
                Key={"escalationId": escalation_id},
                UpdateExpression=("SET #s = :active, assignedAgentId = :agent, claimedAt = :now"),
                ConditionExpression="#s = :queued OR #s = :offered",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":active": EscalationStatus.ACTIVE.value,
                    ":queued": EscalationStatus.QUEUED.value,
                    ":offered": EscalationStatus.OFFERED.value,
                    ":agent": agent_id,
                    ":now": epoch_to_au(now),
                },
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def requeue(self, escalation_id: str) -> bool:
        """Conditionally return an ``OFFERED`` escalation to ``QUEUED``.

        Used when a reservation times out with no answer; clears the stale
        ``assignedAgentId`` since no agent owns the row anymore.

        Returns:
            ``True`` if this call performed the transition, ``False`` if the row was
            not ``OFFERED`` (e.g. already claimed).
        """
        try:
            self._table.update_item(
                Key={"escalationId": escalation_id},
                UpdateExpression="SET #s = :queued REMOVE assignedAgentId",
                ConditionExpression="#s = :offered",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":queued": EscalationStatus.QUEUED.value,
                    ":offered": EscalationStatus.OFFERED.value,
                },
            )
            return True
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def set_status(self, escalation_id: str, status: EscalationStatus) -> None:
        """Unconditionally set ``status`` (terminal transitions: no race to guard)."""
        self._table.update_item(
            Key={"escalationId": escalation_id},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": status.value},
        )

    def count_queued(self) -> int:
        """Return the number of ``QUEUED`` rows (v1 depth check, not a live counter).

        ponytail: reuses ``find_by_status``, which itself does not paginate — a
        pre-existing limit this method inherits, not this slice's job to fix.
        """
        return len(self.find_by_status(EscalationStatus.QUEUED))
