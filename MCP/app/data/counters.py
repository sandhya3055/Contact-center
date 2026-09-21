"""Per-day atomic sequence counters backing the claim/renewal token (D12). [M4 slice]

The ``counters`` table holds one row per day (``counterId`` = ``seq#YYYYMMDD``); each
call atomically increments it via DynamoDB's ``ADD`` update, so concurrent callers never
collide on the same sequence number.
"""

from __future__ import annotations

from app.config import AppConfig
from app.data.dynamo import get_dynamo_resource


class CounterRepository:
    """Atomic per-day counter access (``counters`` table)."""

    def __init__(self, config: AppConfig) -> None:
        """Bind to the configured counters table + data mode."""
        self._config = config

    @property
    def _table(self):
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.counters_table)

    def next_seq(self, counter_id: str) -> int:
        """Atomically increment ``counter_id`` and return the new value.

        Args:
            counter_id: Per-day counter key, e.g. ``"seq#20260731"``.

        Returns:
            The incremented sequence value (starts at 1 for a new counter).
        """
        response = self._table.update_item(
            Key={"counterId": counter_id},
            UpdateExpression="ADD #v :one",
            ExpressionAttributeNames={"#v": "value"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["value"])
