"""Lambda: the agent portal's read-only view of an escalated call.

Why this exists. The portal needs two unrelated things, and only one of them
needs the agent container:

    the escalation reason, the queue row, the conversation  -> DynamoDB + AgentCore Memory
    joining the live call (audio both ways)                 -> the container holding the caller's socket

The container is reachable over a signed WebSocket invocation and nothing else,
so a portal page hosted on a static host (Amplify) cannot call it for the first
group. This Lambda serves that group directly from the authoritative records,
which has a second benefit worth more than the deployment convenience: it keeps
answering after the call ends and the container is gone, where the container's
own in-memory registry only holds a call for HANDOFF_RETENTION_SECONDS.

    browser (portal page on Amplify) --fetch--> this Lambda --> escalations table
                                                            --> AgentCore Memory

Authorization is the per-escalation portal token from the Slack link. This never
sees the token itself: handoff.py stamps sha256(token) onto the escalations row
when it escalates, and this compares against that hash. So the row is useless
to anyone who reads the table, and this Lambda needs no shared secret with the
container.

Packaging. portal_context.py is shared with the container verbatim; this
directory holds a symlink to the one in agentcore_app/ so there is only ever
one copy to keep correct. `zip -r` follows it and stores the real file, so no
build step is needed — but if your packaging tool preserves symlinks, replace
it with a copy of agentcore_app/portal_context.py.

Required execution role permissions:
    dynamodb:GetItem            on the escalations table
    bedrock-agentcore:ListEvents  on the memory resource

Required environment variables:
    ESCALATIONS_TABLE   — the escalations table name (default "escalations")
    MEMORY_ID           — the AgentCore Memory id (same value the agent uses)

Optional environment variables:
    AWS_REGION          — defaults to the Lambda's own region
    ESCALATIONS_REGION  — if the table isn't in the Lambda's region
    MEMORY_REGION       — if memory isn't in the Lambda's region
    ALLOWED_ORIGIN      — the portal origin allowed to call this (default "*").
                          Set it to your Amplify domain: the token travels in
                          the query string, so "*" lets any page that has a
                          forwarded link read the conversation.

boto3 must be recent enough to know the bedrock-agentcore client — the runtime's
bundled copy generally isn't. See portal_context_requirements.txt.
"""

import hashlib
import json
import os
import secrets

import portal_context

ESCALATIONS_TABLE = os.environ.get("ESCALATIONS_TABLE", "escalations")
ESCALATIONS_REGION = os.environ.get("ESCALATIONS_REGION") or os.environ.get(
    "AWS_REGION", "us-west-2")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
MEMORY_REGION = os.environ.get("MEMORY_REGION") or os.environ.get("AWS_REGION", "us-west-2")

_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    # The answer depends on a query-string token and changes during a live
    # call; a cached one would show the human a stale conversation.
    "Cache-Control": "no-store",
}


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": _CORS_HEADERS, "body": json.dumps(body)}


def _query(event: dict) -> dict:
    """Query parameters, whichever front door this is behind.

    Function URLs and API Gateway v2 use queryStringParameters; REST APIs use
    the same key but can send multiValueQueryStringParameters instead.
    """
    params = event.get("queryStringParameters") or {}
    if params:
        return params
    multi = event.get("multiValueQueryStringParameters") or {}
    return {key: values[-1] for key, values in multi.items() if values}


def handler(event, context):
    """Lambda entry point. Works behind a Function URL or API Gateway (HTTP or REST)."""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS_HEADERS, "body": ""}

    params = _query(event)
    escalation_id = (params.get("esc") or "").strip()
    token = params.get("token") or ""
    if not escalation_id or not token:
        # Explicit rather than an empty 200: a service that answers every
        # request with a plausible object is how a misconfigured portal ends up
        # rendering a call that doesn't exist.
        return _reply(400, {"error": "esc and token are required"})

    try:
        row = portal_context.fetch_escalation_row(
            ESCALATIONS_TABLE, ESCALATIONS_REGION, escalation_id)
    except Exception as exc:
        print(f"[portal_context] escalations read failed for {escalation_id}: {exc}")
        return _reply(502, {"error": "couldn't read the escalations table"})

    expected = (row or {}).get("portalTokenHash") or ""
    offered = hashlib.sha256(token.encode()).hexdigest()
    # One 403 for every way this can fail — unknown escalation, a row the agent
    # never stamped, wrong token. Distinguishing them would turn this into an
    # oracle for which escalation ids exist.
    if not expected or not secrets.compare_digest(offered, str(expected)):
        return _reply(403, {"error": "unknown escalation, or bad token"})

    call_context = portal_context.build_context(
        record=row,
        escalation_id=escalation_id,
        table_label=ESCALATIONS_TABLE,
        memory_actor_id=row.get("memoryActorId"),
        memory_session_id=row.get("memorySessionId"),
        memory_id=MEMORY_ID,
        memory_region=MEMORY_REGION,
    )

    return _reply(200, {**call_context, "call": _call_shape(row, escalation_id)})


def _call_shape(row: dict, escalation_id: str) -> dict:
    """The row rendered in the same shape as LiveCall.snapshot().

    Same shape on purpose: the portal renders a call from either source with
    one code path, and the only difference it has to reason about is `live`.

    started_at comes from portalStartedAt, not the row's own createdAt — that
    field is a Sydney-local display string (see MCP/app/data/models.py), not an
    epoch, and the portal does arithmetic on this.
    """
    escalated_at = _number(row.get("portalEscalatedAt"))
    try:
        details = json.loads(row.get("portalDetails") or "{}")
    except (json.JSONDecodeError, TypeError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    # The row's own reason is what the queue acts on, so it wins over the tool
    # argument if the two ever disagree.
    if row.get("reason"):
        details.setdefault("reason", row["reason"])

    return {
        "call_id": row.get("portalCallId") or "",
        # This is the durable record's view: it cannot know whether the caller
        # is still connected, so it reports the state that got them here and
        # lets `live` say that audio isn't on offer from this source.
        "state": "escalated",
        "live": False,
        "mobile_number": row.get("callerNumber"),
        "started_at": _number(row.get("portalStartedAt")) or escalated_at,
        "ended_at": None,
        "escalation": {
            "escalation_id": escalation_id,
            "at": escalated_at,
            "details": details,
        },
        "transcript": [],
        # Nobody is on the line as far as this source can tell. assignedAgentId
        # means the queue offered the row to someone, which is not the same as
        # a human being on the call, and it shows in the record panel anyway.
        "human_label": None,
        "watchers": 0,
        "memory": {
            "actor_id": row.get("memoryActorId"),
            "session_id": row.get("memorySessionId"),
        },
    }


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
