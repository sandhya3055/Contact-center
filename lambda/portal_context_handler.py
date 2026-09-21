"""Lambda: what the agent portal needs about an escalated call, and the signed
URL that lets it join.

Why this exists. The portal needs two unrelated things:

    the escalation reason, the queue row, the conversation  -> DynamoDB + AgentCore Memory
    joining the live call (audio both ways)                 -> the container holding the caller's socket

The container is reachable over a SigV4-signed WebSocket invocation and nothing
else, so a portal page hosted on a static host (Amplify) can do neither on its
own — it has no AWS credentials, and no HTTP surface to fetch.

The first group this serves directly from the authoritative records, which has a
benefit worth more than the deployment convenience: it keeps answering after the
call ends and the container is gone, where the container's own in-memory
registry only holds a call for HANDOFF_RETENTION_SECONDS.

The second group it can't serve — only the container holding the caller's socket
can bridge audio into that call — but it can hand the browser the one thing that
reaches that container: a presigned WebSocket URL for the caller's own Runtime
session. Session id is what AgentCore routes on, so signing for the caller's
session is what puts the human agent's browser in the same instance; the portal
then names the call in its first frame (see handoff.serve_portal_socket). The id
comes off the escalations row, where the container stamped it at escalation time
— nothing else in the system knows it.

    browser (portal page on Amplify) --fetch--> this Lambda --> escalations table
                                                            --> AgentCore Memory
                                                            --> presigned wss:// for the call
                     |
                     +---- wss ----> AgentCore Runtime --> the container holding the call

Authorization for all of it is the per-escalation portal token from the Slack
link. This never sees the token itself: handoff.py stamps sha256(token) onto the
escalations row when it escalates, and this compares against that hash. So the
row is useless to anyone who reads the table, and this Lambda needs no shared
secret with the container. The join URL is minted only after that check passes,
is scoped to one session id, and expires (JOIN_URL_TTL_SECONDS) — but it is a
credential while it lives, so it is returned in the response body and never
logged.

Packaging. portal_context.py is shared with the container verbatim; this
directory holds a symlink to the one in agentcore_app/ so there is only ever
one copy to keep correct. `zip -r` follows it and stores the real file, so no
build step is needed — but if your packaging tool preserves symlinks, replace
it with a copy of agentcore_app/portal_context.py.

Required execution role permissions:
    dynamodb:GetItem              on the escalations table
    bedrock-agentcore:ListEvents  on the memory resource
    bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream
                                  on the agent runtime — for the join URL only;
                                  without it everything else still works and the
                                  portal is read-only. Note it is *not*
                                  InvokeAgentRuntime: that action covers
                                  /invocations, and a presigned /ws signed with
                                  only that grant is refused with a bare HTTP
                                  403 at the handshake, which looks like a bad
                                  signature rather than a missing permission.

Required environment variables:
    ESCALATIONS_TABLE   — the escalations table name (default "escalations")
    MEMORY_ID           — the AgentCore Memory id (same value the agent uses)

Optional environment variables:
    AWS_REGION          — defaults to the Lambda's own region
    ESCALATIONS_REGION  — if the table isn't in the Lambda's region
    MEMORY_REGION       — if memory isn't in the Lambda's region
    AGENT_RUNTIME_ARN   — the agent runtime to presign join URLs for. Unset,
                          the portal is read-only and says so.
    AGENT_RUNTIME_REGION— if the runtime isn't in the Lambda's region
    JOIN_URL_TTL_SECONDS— how long a join URL stays usable (default 900). It's
                          spent within seconds of being issued, so this only
                          needs to cover the page load.
    ALLOWED_ORIGIN      — the portal origin allowed to call this (default "*").
                          Set it to your Amplify domain: the token travels in
                          the query string, so "*" lets any page that has a
                          forwarded link read the conversation — and, now, join
                          the live call.

boto3 must be recent enough to know the bedrock-agentcore client — the runtime's
bundled copy generally isn't. See portal_context_requirements.txt.
"""

import hashlib
import json
import os
import secrets
from urllib.parse import quote

import portal_context

ESCALATIONS_TABLE = os.environ.get("ESCALATIONS_TABLE", "escalations")
ESCALATIONS_REGION = os.environ.get("ESCALATIONS_REGION") or os.environ.get(
    "AWS_REGION", "us-west-2")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
MEMORY_REGION = os.environ.get("MEMORY_REGION") or os.environ.get("AWS_REGION", "us-west-2")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENT_RUNTIME_REGION = os.environ.get("AGENT_RUNTIME_REGION") or os.environ.get(
    "AWS_REGION", "us-west-2")
JOIN_URL_TTL_SECONDS = int(os.environ.get("JOIN_URL_TTL_SECONDS", "900"))

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

    return _reply(200, {
        **call_context,
        "call": _call_shape(row, escalation_id),
        "join": _join(row),
    })


def _join(row: dict) -> dict:
    """How the portal reaches the live call, or why it can't.

    `{"ws_url": ..., "expires_in": ...}` is a presigned WebSocket URL onto the
    agent runtime, signed for the Runtime session the caller's own connection
    arrived on. That session id is the whole point: AgentCore picks the
    instance from it, so this is what lands the human agent's browser in the
    container holding the call rather than in a fresh one. What the portal does
    with the socket is handoff.serve_portal_socket's business.

    `{"note": ...}` instead when no URL can be made. Always one or the other,
    never neither — the portal renders the note where the Join button would
    have been, and "nothing happened" is the one outcome nobody can act on.

    The URL is not evidence the call is still live: the row is stamped at
    escalation and outlives the container. A URL onto a call that has since
    ended connects fine and is then told so by the container, which is the
    honest place for that answer — only the instance holding the call knows.
    """
    if not AGENT_RUNTIME_ARN:
        return {"note": "This portal is read-only: AGENT_RUNTIME_ARN isn't set on the "
                        "portal-context Lambda, so it can't sign a way into the call."}
    session_id = str(row.get("portalRuntimeSessionId") or "").strip()
    if not session_id:
        return {"note": "This escalation didn't record a Runtime session, so there's no "
                        "route into the live call — the caller's page or the agent "
                        "container predates join support. What was said is below."}
    try:
        return {"ws_url": _presign_runtime_ws(session_id), "expires_in": JOIN_URL_TTL_SECONDS}
    except Exception as exc:
        # Deliberately not the exception's text in the note: signing failures
        # here are IAM or configuration problems, and the message can name the
        # role and the runtime ARN.
        print(f"[portal_context] couldn't presign a join URL for {_escalation_id_of(row)}: {exc}")
        return {"note": "Couldn't sign a way into the live call. The portal-context Lambda's "
                        "role may be missing bedrock-agentcore:InvokeAgentRuntime — check its "
                        "CloudWatch logs. What was said is below."}


def _escalation_id_of(row: dict) -> str:
    return str(row.get("escalationId") or "unknown")


def _presign_runtime_ws(session_id: str) -> str:
    """A SigV4-presigned wss:// URL onto the agent runtime's /ws.

    Same shape the caller's own presigner produces, and the same shape the
    bedrock-agentcore SDK builds internally: the path carries the URL-encoded
    runtime ARN, the session id travels as a query parameter, and the signature
    is a query-string one (SigV4QueryAuth) because a browser cannot add an
    Authorization header to a WebSocket handshake.

    Signed with this Lambda's own execution-role credentials, which is what
    makes the whole design work — the browser needs no AWS identity of its own,
    and gets one narrow, expiring capability instead: connect to this runtime,
    on this session.
    """
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    import boto3

    host = f"bedrock-agentcore.{AGENT_RUNTIME_REGION}.amazonaws.com"
    url = (f"https://{host}/runtimes/{quote(AGENT_RUNTIME_ARN, safe='')}/ws"
           f"?X-Amzn-Bedrock-AgentCore-Runtime-Session-Id={quote(session_id, safe='')}")
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method="GET", url=url, headers={"host": host})
    SigV4QueryAuth(credentials, "bedrock-agentcore", AGENT_RUNTIME_REGION,
                   expires=JOIN_URL_TTL_SECONDS).add_auth(request)
    # wss, not https: the signature covers the method, host, path and query,
    # none of which the scheme swap touches.
    return request.url.replace("https://", "wss://", 1)


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
