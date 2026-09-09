"""The durable records behind an escalated call: the escalations row and the
conversation as AgentCore Memory holds it.

Deliberately free of FastAPI, of LiveCall, and of module-level configuration,
because there are two callers with nothing else in common:

- handoff.py, inside the agent container, which has the live call in memory and
  reads these to enrich the portal.
- lambda/portal_context_handler.py, which has no container and no live call —
  only an escalation id and a token — and serves a portal page hosted on a
  static host (Amplify). That path is what lets the portal keep working after
  the call ends and the container is gone.

Both must parse AgentCore Memory identically; a divergence here would show up
as a transcript that renders in one deployment and not the other, so the
parsing lives in one place and the config arrives as arguments.
"""

from __future__ import annotations

import json
import time
from typing import Any

_boto3 = None  # imported lazily: a caller that never asks pays nothing

# Attributes handoff.py stamps on the escalations row so a portal outside the
# container can find and authorize a call. Plumbing, not queue data: they're
# stripped from the row the portal displays, both because the panel is meant to
# show what the contact centre acts on, and because one of them is a token hash
# that has no business being rendered in a browser.
PORTAL_ROW_KEYS = frozenset({
    "portalCallId", "portalTokenHash", "portalStartedAt", "portalEscalatedAt",
    "portalDetails", "portalRuntimeSessionId", "memoryActorId", "memorySessionId",
})


def client(service: str, region: str):
    global _boto3
    if _boto3 is None:
        import boto3 as _b
        _boto3 = _b
    return _boto3.client(service, region_name=region)


def plain(value: Any) -> Any:
    """DynamoDB numbers come back as Decimal, which json can't encode."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if type(value).__name__ == "Decimal":
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def fetch_escalation_row(table: str, region: str, escalation_id: str) -> dict[str, Any] | None:
    """Blocking GetItem on the escalations table — in async code, use a thread."""
    from boto3.dynamodb.types import TypeDeserializer

    response = client("dynamodb", region).get_item(
        TableName=table, Key={"escalationId": {"S": escalation_id}}
    )
    item = response.get("Item")
    if not item:
        return None
    deserialize = TypeDeserializer().deserialize
    return plain({key: deserialize(value) for key, value in item.items()})


def fetch_memory_transcript(
    memory_id: str, region: str, actor_id: str, session_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    """The conversation as AgentCore Memory holds it (blocking; use a thread).

    Events come back newest-first and mix two payload kinds: `conversational`
    (a turn) and `blob` (the agent's own serialized state, which is not
    conversation and is skipped). A turn's text is itself a JSON document
    wrapping the message, so the role comes from inside it rather than from
    the event envelope.
    """
    api = client("bedrock-agentcore", region)
    events: list[dict[str, Any]] = []
    token = None
    while len(events) < limit:
        kwargs = {"memoryId": memory_id, "actorId": actor_id, "sessionId": session_id,
                  "maxResults": min(100, limit - len(events))}
        if token:
            kwargs["nextToken"] = token
        page = api.list_events(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break

    lines: list[dict[str, Any]] = []
    for event in events:
        stamp = event.get("eventTimestamp")
        at = stamp.timestamp() if hasattr(stamp, "timestamp") else time.time()
        for item in event.get("payload", []):
            conversational = item.get("conversational")
            if not conversational:
                continue
            text = (conversational.get("content") or {}).get("text")
            if not isinstance(text, str):
                continue
            role = str(conversational.get("role") or "").lower()
            body = text
            try:
                message = json.loads(text).get("message", {})
                role = str(message.get("role") or role).lower()
                body = " ".join(
                    part.get("text", "") for part in message.get("content", [])
                    if isinstance(part, dict)
                ).strip()
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
            # The kickoff nudge strands_agent.py sends to make Ava speak first
            # is framed as a caller line but nobody said it.
            if not body or "the call has just connected" in body:
                continue
            lines.append({"role": "user" if role == "user" else "assistant",
                          "text": body, "at": at, "source": "memory"})
    lines.reverse()  # oldest first, as a transcript reads
    return lines


def build_context(
    *,
    record: dict[str, Any] | None,
    escalation_id: str | None,
    table_label: str,
    memory_actor_id: str | None,
    memory_session_id: str | None,
    memory_id: str,
    memory_region: str,
    record_error: str | None = None,
) -> dict[str, Any]:
    """Assemble what the portal renders, from a row the caller already fetched.

    The row is passed in rather than read here because both callers need it for
    something else first — handoff.py to report a read failure distinctly, the
    Lambda to authenticate against its token hash — and fetching it twice would
    be a second round trip for the same item.

    Every lookup is best-effort and independent: a missing memory id or an IAM
    gap on one of them must still leave the human with the other, rather than
    an error page. What failed is reported in `notes`, which the portal shows.
    """
    displayed = (
        {k: v for k, v in record.items() if k not in PORTAL_ROW_KEYS}
        if record is not None else None
    )
    context: dict[str, Any] = {"memory": [], "record": displayed, "notes": []}

    if record_error:
        context["notes"].append(record_error)
    elif escalation_id and record is None:
        context["notes"].append(f"No row for {escalation_id} in the {table_label} table.")

    if memory_id and memory_actor_id and memory_session_id:
        try:
            context["memory"] = fetch_memory_transcript(
                memory_id, memory_region, memory_actor_id, memory_session_id)
            if not context["memory"]:
                context["notes"].append(
                    "AgentCore Memory has no turns for this session yet — it's written "
                    "asynchronously during the call, so it can lag by a few seconds.")
        except Exception as exc:
            context["notes"].append(f"Couldn't read AgentCore Memory: {exc}")
            print(f"[portal_context] memory read failed: {exc}")
    elif not memory_id:
        context["notes"].append("MEMORY_ID isn't set, so this call isn't being persisted.")
    elif not (memory_actor_id and memory_session_id):
        context["notes"].append(
            "This call has no AgentCore Memory session recorded, so the conversation "
            "can only come from the live stream.")

    return context
