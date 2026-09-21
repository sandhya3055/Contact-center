"""Which tools a call has already run, for the post-check flags on every response.

A Gateway tool call is one Lambda invocation and knows only about itself, so
"has ``get_policy`` run yet on this call?" cannot be answered from memory —
the execution environment that served the earlier call may not even be the one
serving this. This is the small piece of durable state that answers it, keyed
by the per-call ``session_id`` the agent injects into every tool
(``_FixedArgumentTool`` in ``strands_agent.py``).

Rows live in the **sessions** table under a prefixed key rather than in a table
of their own. The prefix namespaces them away from the resume-by-mobile rows
that already live there, so the two shapes can never collide on a read, and
nothing new has to be provisioned — the table already has TTL enabled on
``ttl``, which is what stops these rows outliving the call they describe.

One ``update_item`` does the whole job: ``ADD`` on a string set both records
this tool and returns the full set back, so recording a call and reading what
came before it is a single round trip rather than a read followed by a write.
It is also atomic, which matters — two tools running concurrently on the same
call would lose one of the two flags under read-modify-write.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import AppConfig
from app.data.dynamo import get_dynamo_resource

# Namespaces these rows away from the resume-by-mobile sessions sharing the
# table. A session id is a uuid4, so a collision was never likely -- the prefix
# is there so a row's *shape* is predictable from its key, and so one of these
# can never be mistaken for a real session by anything that reads the table.
_KEY_PREFIX = "toolchecks#"

# Long enough to outlast any call, short enough that these never accumulate.
# The flags describe one call and are meaningless once it has ended.
_TTL_SECONDS = 24 * 60 * 60


class ToolCheckRepository:
    """Records, per call, which tools have run against it."""

    def __init__(self, config: AppConfig) -> None:
        """Bind to the configured sessions table + data mode."""
        self._config = config

    @property
    def _table(self) -> Any:
        """Resolve the table fresh each access, off this thread's own resource."""
        return get_dynamo_resource(self._config).Table(self._config.sessions_table)

    def record(self, session_id: str, tool: str) -> set[str]:
        """Record ``tool`` as run on this call; return every tool run so far.

        The returned set includes ``tool`` itself, so a caller never has to add
        it back. ``ADD`` creates the item on first use, which is why there is no
        separate insert path.
        """
        response = self._table.update_item(
            Key={"sessionId": f"{_KEY_PREFIX}{session_id}"},
            UpdateExpression="ADD tools :tool SET #ttl = :ttl",
            # `ttl` is aliased rather than written literally: it is short, lower
            # case and exactly the kind of name DynamoDB reserves, and a
            # reserved word here fails the whole update at runtime.
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":tool": {tool},
                ":ttl": int(time.time()) + _TTL_SECONDS,
            },
            ReturnValues="ALL_NEW",
        )
        return set(response.get("Attributes", {}).get("tools") or {tool})
