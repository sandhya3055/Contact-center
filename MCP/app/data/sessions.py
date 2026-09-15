"""The per-call session row: one item in ``sessions``, keyed by ``sessionId``.

Every table this Lambda writes during a call carries that same ``sessionId`` —
``claims`` and ``escalations`` both stamp it (see ``Claim.session_id`` and
``EscalationRecord.session_id``) — so one value reconstructs a whole call across
every table, and across the agent's own logs, which already tag each line with
it. There is no join table and nothing to keep in step: the id is minted once
per call by the agent (``_FixedArgumentTool`` in ``strands_agent.py``) and
travels as an ordinary tool argument.

This row is the part that cannot be derived from the others: which tools have
run. A Gateway tool call is one Lambda invocation and knows only about itself,
so "has ``get_policy`` run yet on this call?" has no answer in memory — the
execution environment that served the earlier call may not even be the one
serving this. Hence a durable row, and hence a single ``update_item`` rather
than a read followed by a write: ``ADD`` on a string set records this tool and
returns the full set in one round trip, and does it atomically, so two tools
running concurrently on one call cannot lose a flag between them.

``createdAt`` is written with ``if_not_exists`` so it keeps the first tool
call's timestamp rather than being overwritten by every subsequent one.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import AppConfig
from app.data.dynamo import get_dynamo_resource
from app.data.models import epoch_to_au

# Long enough to outlast any call and any reasonable look at it afterwards,
# short enough that these never accumulate. The table has TTL enabled on `ttl`.
_TTL_SECONDS = 7 * 24 * 60 * 60


class SessionRepository:
    """Read/write access to the ``sessions`` table — one row per call."""

    def __init__(self, config: AppConfig) -> None:
        """Bind to the configured sessions table + data mode."""
        self._config = config

    @property
    def _table(self) -> Any:
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.sessions_table)

    def record_tool(self, session_id: str, tool: str) -> set[str]:
        """Record ``tool`` as run on this call; return every tool run so far.

        Creates the session row on first use — there is no separate insert
        path, because the first tool call of a call is what a session *is*.
        The returned set includes ``tool`` itself, so a caller never has to add
        it back.
        """
        now = int(time.time())
        response = self._table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression=(
                "ADD tools :tool "
                "SET #ttl = :ttl, updatedAt = :now, "
                "createdAt = if_not_exists(createdAt, :now)"
            ),
            # `ttl` is aliased rather than written literally: it is short, lower
            # case and exactly the kind of name DynamoDB reserves, and a
            # reserved word here fails the whole update at runtime.
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":tool": {tool},
                ":ttl": now + _TTL_SECONDS,
                ":now": epoch_to_au(now),
            },
            ReturnValues="ALL_NEW",
        )
        return set(response.get("Attributes", {}).get("tools") or {tool})

    def get(self, session_id: str) -> dict[str, Any] | None:
        """The session row, or ``None`` if this call has run no tools yet."""
        item = self._table.get_item(Key={"sessionId": session_id}).get("Item")
        if item is None:
            return None
        return {**item, "tools": set(item.get("tools") or ())}
