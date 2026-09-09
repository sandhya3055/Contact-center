"""Human handoff: escalation -> Slack -> agent portal -> live audio bridge.

What this adds to a call that strands_agent.py is already running:

1. When Ava escalates, EscalationNotifierTool notices (generically -- any tool
   result carrying an escalation id, no tool name hardcoded, same as every
   other wrapper in strands_agent.py) and posts one Slack message with the
   reason, the caller, the last few lines of the conversation, and a link.
2. That link opens the agent portal (portal.html, served from /portal) on
   exactly this call: the full transcript, the escalation reason, and a
   **Join call** button. The portal is live -- new transcript lines arrive as
   they're spoken, and the human hears the call from the moment they open it.
3. Joining bridges audio both ways and takes Ava off the call entirely: her
   Nova Sonic session is torn down (strands_agent.py does it, on the signal
   this module sends), the caller's mic is relayed to the human's browser
   instead of to the model, and the human's mic is relayed to the caller. From
   that point the human is the only voice on the line — there is no model left
   listening to what the caller tells a person, and none that can start
   speaking over them. Leaving therefore doesn't hand the call back to Ava;
   it puts the caller back in the queue for another human.

    caller browser <-- audio --> /ws  <-- audio -->  /portal/ws  <-- --> human browser
                                  |
                                  +-- Nova Sonic (disconnected once a human joins)

Two doors, one bridge. `/portal/ws/{call_id}` above is the direct one, used
whenever the human's browser can reach this process as an ordinary origin — a
local run, or a deployment behind an ALB. On AgentCore Runtime it cannot: the
platform publishes exactly one WebSocket path per container (`/ws`) behind a
SigV4-signed endpoint and routes a connection to an instance by session id, so
there is no URL a browser could point at `/portal/ws/...` at all. `serve_portal_socket`
below is that second door: the human presigns `/ws` with the *caller's* session
id — which is what lands them in the container holding the call — and names the
call in their first frame instead of in the path. Past that handshake both doors
run the same code (`_serve_portal`), so the bridge itself has one implementation.

Everything lives in this process's memory (LIVE_CALLS), which is the honest
scope of this implementation: one running container. Two consequences worth
knowing before deploying more than one instance --

- A portal link only resolves on the instance holding that call, so with
  several instances behind one endpoint the human needs sticky routing to it
  (or this state needs to move to something shared, plus a relay both
  instances can reach).
- Calls are kept for RETENTION_SECONDS after they end so the transcript is
  still readable from the Slack link, then dropped. Nothing is persisted, so
  a restart loses them; AgentCore Memory (already wired up in
  strands_agent.py) remains the durable record of the conversation itself.

Access control is a single per-escalation token: it's minted when the call is
escalated, only ever sent to Slack, and both portal endpoints require it. So
reaching a live call means having the Slack message -- but equally, anyone who
has that message (or the URL after it's been forwarded) can listen and speak
to the caller. If this ever fronts real callers, put a real identity check in
front of the portal routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import portal_context

# Where the Slack message's button points. Must be reachable by whoever clicks
# it in Slack -- http://127.0.0.1:8080 while developing locally, your public
# origin once deployed.
PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_ESCALATION_CHANNEL = os.getenv("SLACK_ESCALATION_CHANNEL")

# How long an ended call stays readable in the portal. A human who opens the
# Slack link a few minutes after the caller hung up should still see what was
# said, rather than a dead link.
RETENTION_SECONDS = int(os.getenv("HANDOFF_RETENTION_SECONDS", "1800"))

# Set this to any secret string to enable /portal/queue, a list of this
# instance's calls with their portal links — the answer to "Slack didn't post
# and I've lost the link from the log". Unset (the default) leaves that route
# refusing every request: each call's link is a bearer token, so a page
# listing all of them is strictly more dangerous than one link.
ADMIN_TOKEN = os.getenv("HANDOFF_ADMIN_TOKEN")

# The two authoritative records the portal reads, rather than trusting only
# what this process happens to be holding in memory:
#
# - AgentCore Memory (short-term/session): the conversation as the agent
#   itself persisted it, turn by turn. strands_agent.py already writes this
#   via AgentCoreMemorySessionManager; this reads the same events back.
# - The escalations DynamoDB table: the queue row the gateway's escalate tool
#   wrote — status, priority key, TTL, captured slots — which is what the rest
#   of the contact centre acts on, not the tool arguments Ava passed.
# Origins allowed to reach the portal API and socket from a browser, when the
# portal page is hosted somewhere other than this process (a static host like
# Amplify) — comma-separated, e.g.
# "https://main.d123.amplifyapp.com,https://agents.example.com".
#
# Unset (the default) means the page is served by this app on the same origin,
# where neither CORS nor an origin check is involved at all. Once set, both
# are enforced: CORS for the fetch, and an explicit Origin check on the
# WebSocket — browsers do not apply the same-origin policy to WebSockets, so
# without that check any page on the internet holding a leaked token could
# open a live caller's audio bridge.
PORTAL_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("PORTAL_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

MEMORY_ID = os.getenv("MEMORY_ID", "")
MEMORY_REGION = os.getenv("MEMORY_REGION") or os.getenv("AWS_REGION", "us-west-2")
ESCALATIONS_TABLE = os.getenv("ESCALATIONS_TABLE", "escalations")
ESCALATIONS_REGION = os.getenv("ESCALATIONS_REGION") or os.getenv("AWS_REGION", "us-west-2")

# The first frame a human agent's portal sends on the agent's own /ws, saying
# which call it wants to bridge into. Named as a type rather than inferred from
# the message's shape so a caller page can never accidentally be read as one:
# strands_agent.py branches on exactly this value and nothing else.
PORTAL_ATTACH_TYPE = "portal_attach"

STATE_AI = "ai"                # Ava has the call
STATE_ESCALATED = "escalated"  # a human was asked for; nobody has joined yet
STATE_HUMAN = "human"          # a human is on the line and Ava is off it
STATE_ENDED = "ended"

_PORTAL_HTML = Path(__file__).resolve().parent / "portal.html"

# Every live (and recently ended) call, keyed by the id that appears in the
# portal link. Single-process registry -- see the module docstring.
LIVE_CALLS: dict[str, LiveCall] = {}


def _now() -> float:
    return time.time()


def _prune_calls() -> None:
    for call_id, call in list(LIVE_CALLS.items()):
        if call.state == STATE_ENDED and _now() - (call.ended_at or 0) > RETENTION_SECONDS:
            LIVE_CALLS.pop(call_id, None)


class LiveCall:
    """One caller's call, as the handoff machinery needs to see it.

    strands_agent.py owns the call itself; this holds the parts a human agent
    needs later (what was said, why it escalated) plus the plumbing to move
    audio between the two browsers.
    """

    def __init__(self, mobile_number: str | None = None) -> None:
        # Short id because it ends up in a URL a human clicks; random rather
        # than sequential so a valid id can't be guessed from another one.
        self.call_id = uuid.uuid4().hex[:12]
        self.started_at = _now()
        self.ended_at: float | None = None
        self.mobile_number = mobile_number
        self.state = STATE_AI
        self.transcript: list[dict[str, Any]] = []
        self.escalation: dict[str, Any] | None = None
        self.join_token: str | None = None
        self.human_label: str | None = None

        # Who the caller says they are, from the pre-call form in
        # frontend/index.html (validated there and again in strands_agent.py
        # before either ever reaches here). Shown to whoever picks the call up
        # so they open with the caller's name instead of asking for it again.
        self.caller_name: str | None = None
        self.caller_dob: str | None = None

        # How this call is keyed in AgentCore Memory, so the portal can read
        # the conversation back from the durable record rather than only from
        # this process's transcript list. Set by strands_agent.py once the
        # caller's number (and so the actor id) is known.
        self.memory_actor_id: str | None = None
        self.memory_session_id: str | None = None

        # The AgentCore Runtime session this call's socket arrived on, when
        # there is one. This is the only thing that routes a second connection
        # to this same container, so it's what a human agent has to presign
        # with to join — see serve_portal_socket. Set by strands_agent.py from
        # the connection itself; None on a local run, where the portal reaches
        # this process directly and needs no routing hint.
        self.runtime_session_id: str | None = None

        # Set by strands_agent.py once the caller's socket is up: how to send
        # an event to the caller's browser.
        self.customer_send: Callable[[dict[str, Any]], Awaitable[None]] | None = None

        # Portal sockets watching this call. Everyone attached hears the call;
        # only `human_socket` (the one that pressed Join) can speak into it.
        self.portals: set[WebSocket] = set()
        self.human_socket: WebSocket | None = None

        # How to take the AI off this call when a human takes it over, set by
        # strands_agent.py — the only place that holds the model session. A
        # plain synchronous signal rather than a coroutine on purpose: the
        # teardown belongs to the task that owns that session, and the human
        # who just pressed Join must not be left waiting on it.
        self.request_ai_disconnect: Callable[[], None] | None = None
        # Flipped once that session is really gone, by mark_ai_disconnected.
        # What it changes is where the call goes if the human then leaves:
        # there is no AI left to hand it back to.
        self.ai_disconnected = False

        # The in-flight Slack notification, parked here so asyncio doesn't
        # garbage-collect a task nothing else holds a reference to.
        self.slack_task: asyncio.Task | None = None
        # Likewise the escalations-row stamp, which notify_slack waits on.
        self.portal_row_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- state

    @property
    def human_on_call(self) -> bool:
        return self.state == STATE_HUMAN and self.human_socket is not None

    def token_matches(self, token: str) -> bool:
        # compare_digest, not ==: this is a bearer token, and a plain string
        # compare leaks its length/prefix through timing.
        return bool(self.join_token) and secrets.compare_digest(token or "", self.join_token)

    def snapshot(self) -> dict[str, Any]:
        """Everything the portal renders for this call."""
        return {
            "call_id": self.call_id,
            "state": self.state,
            "mobile_number": self.mobile_number,
            "caller_name": self.caller_name,
            "caller_dob": self.caller_dob,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "escalation": self.escalation,
            "transcript": self.transcript,
            "human_label": self.human_label,
            "ai_disconnected": self.ai_disconnected,
            "watchers": len(self.portals),
            "memory": {"actor_id": self.memory_actor_id, "session_id": self.memory_session_id},
        }

    # ------------------------------------------------------------ transcript

    async def record_transcript(self, role: str, text: str) -> None:
        """Append one finished utterance and push it to any open portal."""
        line = {"role": role, "text": text, "at": _now()}
        self.transcript.append(line)
        await self.broadcast({"type": "transcript", "line": line})

    # --------------------------------------------------------------- sending

    async def _send(self, socket: WebSocket, event: dict[str, Any]) -> None:
        try:
            await socket.send_json(event)
        except Exception:
            # A portal that vanished mid-send is dropped rather than allowed to
            # take down the call it was watching.
            self.portals.discard(socket)
            if self.human_socket is socket:
                self.human_socket = None
                self.state = STATE_ESCALATED

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send to every attached portal."""
        for socket in list(self.portals):
            await self._send(socket, event)

    async def to_customer(self, event: dict[str, Any]) -> None:
        if self.customer_send is None:
            return
        try:
            await self.customer_send(event)
        except Exception:
            pass

    async def push_state(self) -> None:
        await self.broadcast({"type": "call_state", "call": self.snapshot()})

    # ----------------------------------------------------------------- audio

    async def relay_caller_audio(self, event: dict[str, Any]) -> None:
        """Caller's mic -> every open portal, tagged so the UI can show who's
        talking. Sent whether or not a human has joined: hearing the call
        before taking it is the whole point of opening the portal."""
        if not self.portals:
            return
        await self.broadcast({
            "type": "bidi_audio_stream",
            "source": "caller",
            "audio": event.get("audio"),
            "sample_rate": event.get("sample_rate", 16000),
        })

    async def relay_ava_audio(self, event: dict[str, Any]) -> None:
        """Ava's speech -> portals, so a listening human hears both sides."""
        if not self.portals:
            return
        await self.broadcast({**event, "source": "ava"})

    async def relay_human_audio(self, socket: WebSocket, event: dict[str, Any]) -> None:
        """Human's mic -> the caller. Only from the socket that joined: an
        extra portal listening in must not be able to speak to the caller."""
        if socket is not self.human_socket or not self.human_on_call:
            return
        await self.to_customer({
            "type": "bidi_audio_stream",
            "source": "human",
            "audio": event.get("audio"),
            "sample_rate": event.get("sample_rate", 16000),
        })

    # ------------------------------------------------------------ escalation

    async def mark_escalated(self, details: dict[str, Any], escalation_id: str | None) -> str:
        """Record why this call needs a human and mint the portal token.

        Returns the portal URL to put in Slack.
        """
        self.join_token = secrets.token_urlsafe(24)
        self.escalation = {
            "escalation_id": escalation_id,
            "at": _now(),
            # The tool's own arguments, verbatim: reason and note are what the
            # gateway's escalate tool takes today, but anything else it grows
            # shows up in the portal too without a change here.
            "details": {k: v for k, v in details.items() if k != "session_id"},
        }
        self.state = STATE_ESCALATED
        # Not awaited: this is on the caller's critical path — the tool result
        # Ava is waiting on to say "let me put you through" — and a DynamoDB
        # round trip that goes wrong is seconds of dead air. notify_slack waits
        # for it instead, which is the only ordering that actually matters: a
        # human must not click a link before the row that authorizes them.
        self.portal_row_task = asyncio.create_task(self._persist_portal_keys(escalation_id))
        await self.push_state()
        await self.to_customer({"type": "bidi_handoff", "state": STATE_ESCALATED})
        return self.portal_url()

    async def _persist_portal_keys(self, escalation_id: str | None) -> None:
        """Put this call's portal keys on the escalations row.

        Best-effort by design: this only enables a portal hosted off this
        container (see _write_portal_row). Failing it must not fail the
        escalation, which is a caller waiting for a human — the portal served
        from this process needs none of it.
        """
        if not escalation_id or not self.join_token:
            return
        fields: dict[str, Any] = {
            "portalCallId": {"S": self.call_id},
            "portalTokenHash": {"S": hashlib.sha256(self.join_token.encode()).hexdigest()},
            "portalStartedAt": {"N": f"{self.started_at:.3f}"},
            "portalEscalatedAt": {"N": f"{(self.escalation or {}).get('at', _now()):.3f}"},
            # The tool's arguments verbatim, so both deployments of the portal
            # render the same facts. callerNumber is deliberately absent: the
            # escalate tool owns that field and this must not clobber it.
            "portalDetails": {"S": json.dumps((self.escalation or {}).get("details") or {})},
        }
        if self.memory_actor_id:
            fields["memoryActorId"] = {"S": self.memory_actor_id}
        if self.memory_session_id:
            fields["memorySessionId"] = {"S": self.memory_session_id}
        # What lets a portal hosted off this container actually *join*, not just
        # read: the presigner has no other way to learn which Runtime session
        # to sign for, and signing for any other one reaches a different
        # container where this call does not exist.
        if self.runtime_session_id:
            fields["portalRuntimeSessionId"] = {"S": self.runtime_session_id}
        try:
            # An in-region UpdateItem is tens of milliseconds; this cap is for
            # the pathological case, and it's short because notify_slack waits
            # on it and someone is waiting on that.
            await asyncio.wait_for(
                asyncio.to_thread(_write_portal_row, escalation_id, fields), timeout=4)
        except Exception as exc:
            print(f"[handoff] couldn't stamp portal keys on {escalation_id}: {exc} "
                  "— a portal hosted off this container won't resolve this call")

    def portal_url(self) -> str:
        query = f"call={self.call_id}&token={self.join_token}"
        # The escalation id is what a portal reading DynamoDB directly looks
        # the call up by; the in-container portal ignores it and uses call+token.
        escalation_id = (self.escalation or {}).get("escalation_id")
        if escalation_id:
            query += f"&esc={urllib.parse.quote(str(escalation_id), safe='')}"
        return f"{PORTAL_BASE_URL}/portal?{query}"

    # ------------------------------------------------------- portal lifecycle

    def attach_portal(self, socket: WebSocket) -> None:
        self.portals.add(socket)

    async def detach_portal(self, socket: WebSocket) -> None:
        self.portals.discard(socket)
        if self.human_socket is socket:
            # The human closed the tab without pressing Leave — same outcome,
            # the caller goes back to Ava rather than to silence.
            await self.release_human(socket, reason="disconnected")
        else:
            await self.push_state()

    async def join_human(self, socket: WebSocket, label: str | None) -> None:
        """Put a human on the line and take Ava off the call for good."""
        if self.state == STATE_ENDED:
            await self._send(socket, {"type": "error", "message": "This call has already ended."})
            return
        if self.human_socket is not None and self.human_socket is not socket:
            await self._send(socket, {"type": "error", "message": "Another agent is already on this call."})
            return

        self.human_socket = socket
        self.human_label = (label or "").strip()[:60] or "a specialist"
        self.state = STATE_HUMAN

        # Ava doesn't just go quiet — she comes off the call. Flipping the
        # state above is what stops her audio reaching the caller (the gates
        # in strands_agent.py read it on every frame), and this asks the task
        # that owns her Nova Sonic session to end it, which is what makes the
        # human the only voice on the line rather than merely the louder one.
        #
        # Signalled, not awaited: that teardown is the call task's to do, and
        # a human who has just pressed Join shouldn't be held up by it. It
        # reports back through mark_ai_disconnected when it's done.
        if self.request_ai_disconnect is not None:
            try:
                self.request_ai_disconnect()
            except Exception as exc:
                print(f"[handoff] couldn't ask the AI to leave {self.call_id}: {exc}")

        await self.push_state()
        await self.to_customer({
            "type": "bidi_handoff",
            "state": STATE_HUMAN,
            "agent_name": self.human_label,
        })
        print(f"[handoff] {self.human_label} joined call {self.call_id}")

    async def mark_ai_disconnected(self) -> None:
        """Ava's session is really gone — called by strands_agent.py once the
        teardown join_human asked for has actually finished."""
        self.ai_disconnected = True
        await self.push_state()

    async def release_human(self, socket: WebSocket, reason: str = "left") -> None:
        """Take the human off the line — back to the queue, or back to Ava if
        she was never disconnected (see the branch below)."""
        if self.human_socket is not socket:
            return
        self.human_socket = None
        label = self.human_label
        self.human_label = None
        if self.state != STATE_ENDED:
            self.state = STATE_ESCALATED
            # Where the caller goes depends on whether there's still an AI to
            # go back to. Once a human has joined there isn't: that join tore
            # the model session down, so the honest thing to tell the caller
            # is that they're waiting for another person — not that Ava is
            # back, which would leave them talking to nobody.
            await self.to_customer({
                "type": "bidi_handoff",
                "state": STATE_ESCALATED if self.ai_disconnected else STATE_AI,
                "ai_available": not self.ai_disconnected,
            })
        await self.push_state()
        print(f"[handoff] {label} {reason} call {self.call_id}")

    async def mark_ended(self) -> None:
        self.state = STATE_ENDED
        self.ended_at = _now()
        self.human_socket = None
        self.customer_send = None
        await self.push_state()
        _prune_calls()


# ------------------------------------------------- authoritative records
#
# The reading itself lives in portal_context.py, shared verbatim with
# lambda/portal_context_handler.py so a portal hosted off this container parses
# AgentCore Memory the same way this one does.


async def load_context(call: LiveCall) -> dict[str, Any]:
    """The portal's view of the durable records behind this call."""
    escalation_id = (call.escalation or {}).get("escalation_id")
    row: dict[str, Any] | None = None
    row_error: str | None = None
    if escalation_id:
        try:
            row = await asyncio.to_thread(
                portal_context.fetch_escalation_row,
                ESCALATIONS_TABLE, ESCALATIONS_REGION, escalation_id)
        except Exception as exc:
            row_error = f"Couldn't read the escalations table: {exc}"
            print(f"[handoff] escalations table read failed: {exc}")

    return await asyncio.to_thread(
        portal_context.build_context,
        record=row,
        escalation_id=escalation_id,
        table_label=ESCALATIONS_TABLE,
        memory_actor_id=call.memory_actor_id,
        memory_session_id=call.memory_session_id,
        memory_id=MEMORY_ID,
        memory_region=MEMORY_REGION,
        record_error=row_error,
    )


def _write_portal_row(escalation_id: str, fields: dict[str, Any]) -> None:
    """Stamp the escalations row with what an off-container portal needs.

    Blocking; call via asyncio.to_thread.

    Everything the portal shows before anyone joins — the reason, the caller,
    the conversation — is derivable from DynamoDB and AgentCore Memory, but
    only if the reader knows *which* memory session to look in and is allowed
    to look. Those two facts live in this process's memory and nowhere else, so
    they get written here, alongside a hash of the portal token.

    A hash, not the token: the token is 24 random bytes with no dictionary to
    attack, so sha256 with no salt is enough to make a leaked table row useless
    for reaching a live caller.

    The condition matters. UpdateItem creates the item when it's missing, and a
    row holding only these fields — no status, no queueKey — would be a phantom
    escalation the queue's GSI can't order and the ACD would trip over. If the
    escalate tool's own write hasn't landed, this must fail instead.
    """
    expression = ", ".join(f"#{key} = :{key}" for key in fields)
    portal_context.client("dynamodb", ESCALATIONS_REGION).update_item(
        TableName=ESCALATIONS_TABLE,
        Key={"escalationId": {"S": escalation_id}},
        UpdateExpression=f"SET {expression}",
        ExpressionAttributeNames={f"#{key}": key for key in fields},
        ExpressionAttributeValues={f":{key}": value for key, value in fields.items()},
        ConditionExpression="attribute_exists(escalationId)",
    )


def register_call(mobile_number: str | None = None) -> LiveCall:
    _prune_calls()
    call = LiveCall(mobile_number)
    LIVE_CALLS[call.call_id] = call
    return call


def unregister_call(call: LiveCall) -> None:
    """Drop a call that turned out never to be one.

    strands_agent.py registers a call as soon as a socket is accepted, before
    the first frame says whether the client is a caller or a human agent
    joining an existing call. A portal connection would otherwise leave a
    caller-less entry behind, which /portal/queue lists as a real call and
    _prune_calls never collects (it only expires ENDED ones).
    """
    LIVE_CALLS.pop(call.call_id, None)


# ---------------------------------------------------------------- Slack

def _transcript_tail(call: LiveCall, lines: int = 8) -> str:
    tail = call.transcript[-lines:]
    if not tail:
        return "_Nothing transcribed yet._"
    rendered = "\n".join(
        f"{'Caller' if line['role'] == 'user' else 'Ava'}: {line['text']}" for line in tail
    )
    # Slack rejects a block over 3000 chars; the tail is context, not the
    # record — the portal has the whole thing.
    if len(rendered) > 2600:
        rendered = "…" + rendered[-2600:]
    return f"```{rendered}```"


def _slack_blocks(call: LiveCall, portal_url: str) -> list[dict[str, Any]]:
    details = (call.escalation or {}).get("details", {}) or {}
    reason = str(details.get("reason") or "not given")
    note = str(details.get("note") or "").strip()
    fields = [
        {"type": "mrkdwn", "text": f"*Reason*\n{reason}"},
        {"type": "mrkdwn", "text": f"*Caller*\n{call.caller_name or 'name not provided'}"},
        {"type": "mrkdwn", "text": f"*Number*\n{call.mobile_number or 'not provided'}"},
    ]
    for key, value in details.items():
        if key in {"reason", "note"} or value in (None, ""):
            continue
        fields.append({"type": "mrkdwn", "text": f"*{key.replace('_', ' ').title()}*\n{value}"})

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "☎️ Caller needs a human", "emoji": True}},
        # Slack renders at most 10 fields per section.
        {"type": "section", "fields": fields[:10]},
    ]
    if note:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Ava's summary*\n{note}"}})
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Last few lines*\n{_transcript_tail(call)}"}},
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Open agent portal", "emoji": True},
                "url": portal_url,
            }],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"The caller is still on the line · call `{call.call_id}` · "
                        "opening the portal lets you listen before you join, and "
                        "joining disconnects Ava",
            }],
        },
    ]
    return blocks


def _post_to_slack(payload: dict[str, Any]) -> None:
    """Blocking chat.postMessage — always called via asyncio.to_thread, since
    a live voice call is on the other side of this event loop."""
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode())
    # Slack answers 200 with {"ok": false, "error": ...} for auth/channel
    # problems, so the HTTP status alone says nothing.
    if not body.get("ok"):
        raise RuntimeError(f"slack error: {body.get('error')}")


async def notify_slack(call: LiveCall, portal_url: str) -> None:
    """Post the escalation to Slack. Never raises: a failed notification must
    not break the call the caller is still on — the link is printed either
    way, so a local run without Slack configured is still usable."""
    print(f"[handoff] escalation on call {call.call_id} — portal: {portal_url}")
    # The link is about to become clickable, so let the row that authorizes it
    # land first. Off the caller's critical path — they're hearing Ava say a
    # colleague is coming — and bounded, because a stuck write must not swallow
    # the notification that a person is waiting.
    if call.portal_row_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(call.portal_row_task), timeout=10)
        except Exception:
            pass
    if not (SLACK_BOT_TOKEN and SLACK_ESCALATION_CHANNEL):
        print("[handoff] SLACK_BOT_TOKEN/SLACK_ESCALATION_CHANNEL not set — "
              "not posting to Slack; use the portal link above")
        return

    details = (call.escalation or {}).get("details", {}) or {}
    payload = {
        "channel": SLACK_ESCALATION_CHANNEL,
        # Fallback text for notifications and clients that can't render blocks.
        "text": f"Caller needs a human — {details.get('reason') or 'escalation'} — {portal_url}",
        "blocks": _slack_blocks(call, portal_url),
        "unfurl_links": False,
    }
    try:
        await asyncio.to_thread(_post_to_slack, payload)
        print(f"[handoff] posted escalation for call {call.call_id} to {SLACK_ESCALATION_CHANNEL}")
    except Exception as exc:
        print(f"[handoff] failed to post to Slack: {exc}")


# ---------------------------------------------------------------- portal API

router = APIRouter()


def install_portal_cors(app: Any) -> None:
    """Allow the configured origins to call the portal API cross-origin.

    Lives here rather than beside the app's construction so that mounting
    `router` and applying its access policy are one decision — forgetting the
    second half yields a portal that loads and then silently can't fetch.
    Scoped to GET, no credentials: the portal authenticates with the
    per-escalation token in the query string, not a cookie.
    """
    if not PORTAL_ALLOWED_ORIGINS:
        return
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=PORTAL_ALLOWED_ORIGINS,
        allow_methods=["GET"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    print(f"[handoff] portal CORS enabled for {PORTAL_ALLOWED_ORIGINS}")


def _authorized(call_id: str, token: str) -> LiveCall | None:
    call = LIVE_CALLS.get(call_id)
    return call if call is not None and call.token_matches(token) else None


def origin_allowed(origin: str | None) -> bool:
    """Whether a browser at `origin` may open the portal socket.

    No allowlist configured means the portal is served by this app and reached
    on its own origin, so there is nothing to check. With one configured, the
    check is mandatory: CORS would cover the fetch, but a WebSocket handshake
    is exempt from the same-origin policy, so this is the only thing standing
    between a leaked token on some other site and a live caller's audio.
    """
    if not PORTAL_ALLOWED_ORIGINS:
        return True
    return bool(origin) and origin.rstrip("/") in PORTAL_ALLOWED_ORIGINS


@router.get("/portal")
async def portal_page() -> HTMLResponse:
    """The agent portal. Which call it shows comes from the query string
    (?call=&token=), read by the page itself — so this is a static document
    and the token never has to be templated into it server-side."""
    if not _PORTAL_HTML.exists():
        return HTMLResponse("<h1>portal.html is missing from the deployment</h1>", status_code=500)
    # Read per request rather than cached at import: editing portal.html
    # during local development then just needs a browser refresh.
    return HTMLResponse(_PORTAL_HTML.read_text())


_QUEUE_PAGE = """<!doctype html><title>Handoff queue</title>
<style>
 body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;
      background:#f4f6fb;color:#0f1219}}
 @media (prefers-color-scheme:dark){{body{{background:#090b11;color:#eceef5}}
      table{{background:#13161e!important}} th{{color:#a0a7ba!important}}
      td{{border-color:#252935!important}} a{{color:#7793ff!important}}}}
 main{{max-width:1000px;margin:0 auto;padding:28px 20px}}
 h1{{font-size:20px;margin:0 0 4px}} p.sub{{margin:0 0 22px;color:#586073}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden}}
 th,td{{text-align:left;padding:11px 14px;border-bottom:1px solid #e2e5ee;font-size:13.5px}}
 th{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8a92a6}}
 tr:last-child td{{border-bottom:none}}
 .pill{{display:inline-block;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:999px}}
 .ai{{background:#1b3bd614;color:#1b3bd6}} .escalated{{background:#f59e0b26;color:#b45309}}
 .human{{background:#16a34a26;color:#16a34a}} .ended{{background:#8a92a626;color:#8a92a6}}
 code{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
 .empty{{padding:40px;text-align:center;color:#8a92a6}}
</style>
<main><h1>Handoff queue</h1>
<p class="sub">Calls this instance is holding. Escalated calls link straight into the agent
portal — the link carries that call's own access token.</p>
{rows}</main>
"""


@router.get("/portal/queue")
async def portal_queue(token: str = "") -> HTMLResponse:
    """Every call this process is holding, with its portal link.

    Exists because the link is otherwise only in the Slack message and one
    line of the server log — lose both and a live escalation is unreachable,
    since the token can't be reconstructed. Off unless HANDOFF_ADMIN_TOKEN is
    set: this lists every call's bearer token at once.
    """
    if not ADMIN_TOKEN:
        return HTMLResponse(
            "<h1>Not enabled</h1><p>Set <code>HANDOFF_ADMIN_TOKEN</code> in .env and restart "
            "to use this page.</p>", status_code=403)
    if not secrets.compare_digest(token or "", ADMIN_TOKEN):
        return HTMLResponse("<h1>Forbidden</h1>", status_code=403)

    _prune_calls()
    calls = sorted(LIVE_CALLS.values(), key=lambda c: c.started_at, reverse=True)
    if not calls:
        body = '<div class="empty">No calls yet. Start one and escalate it.</div>'
    else:
        lines = ["<table><tr><th>Started</th><th>State</th><th>Caller</th><th>Reason</th>"
                 "<th>Lines</th><th>Portal</th></tr>"]
        for call in calls:
            details = (call.escalation or {}).get("details", {}) or {}
            started = time.strftime("%H:%M:%S", time.localtime(call.started_at))
            link = (f'<a href="{escape(call.portal_url(), quote=True)}">open portal</a>'
                    if call.join_token else "<span style='color:#8a92a6'>not escalated</span>")
            lines.append(
                f"<tr><td><code>{started}</code></td>"
                f'<td><span class="pill {escape(call.state)}">{escape(call.state)}</span></td>'
                f"<td>{escape(str(call.mobile_number or '—'))}</td>"
                f"<td>{escape(str(details.get('reason') or '—'))}</td>"
                f"<td>{len(call.transcript)}</td><td>{link}</td></tr>")
        lines.append("</table>")
        body = "".join(lines)
    return HTMLResponse(_QUEUE_PAGE.format(rows=body))


@router.get("/portal/api/call/{call_id}")
async def portal_call(call_id: str, token: str = "") -> JSONResponse:
    call = _authorized(call_id, token)
    if call is None:
        return JSONResponse({"error": "unknown call, or bad token"}, status_code=403)
    return JSONResponse(call.snapshot())


@router.websocket("/portal/ws/{call_id}")
async def portal_ws(websocket: WebSocket, call_id: str, token: str = "") -> None:
    """The human agent's end of the call, reached directly: transcript updates
    and call audio down, join/leave and mic audio up.

    Usable whenever the human's browser can address this process as an ordinary
    origin. On AgentCore Runtime it cannot — see serve_portal_socket, which is
    the same bridge entered through the one path Runtime does publish.
    """
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin):
        print(f"[handoff] refused portal socket from origin {origin!r} "
              f"(allowed: {PORTAL_ALLOWED_ORIGINS})")
        await websocket.close(code=4403)
        return

    call = _authorized(call_id, token)
    if call is None:
        # Refused before accept(), so an unauthorized socket never reaches a
        # live call at all. 4403 rather than a plain close so the portal can
        # tell "not allowed" apart from a dropped connection.
        await websocket.close(code=4403)
        return

    await websocket.accept()
    await _serve_portal(websocket, call)


def is_portal_attach(message: Any) -> bool:
    """Whether a client's first frame on /ws is a human agent joining a call
    rather than a caller starting one."""
    return isinstance(message, dict) and message.get("type") == PORTAL_ATTACH_TYPE


async def serve_portal_socket(websocket: WebSocket, attach: dict[str, Any]) -> None:
    """The human agent's end of the call, reached through the agent's own /ws.

    The door that works on AgentCore Runtime. Runtime publishes one WebSocket
    path per container and chooses the instance from the session id in the
    signed URL, so a portal cannot ask for /portal/ws/{call_id}: it presigns
    /ws with the caller's own session id — putting it in this container — and
    puts the call id and token in its first frame instead of in the path.

    The socket is already accepted when this is called (whoever accepted it had
    to, in order to read that frame), so an unauthorized one is told why and
    then closed, rather than being refused at the handshake the way
    /portal/ws/{call_id} refuses it. Saying why matters more here: a browser
    sees a pre-accept refusal as a bare 1006, and on this path the two likely
    causes — a stale link, and a call now held by a different instance — need
    different actions from the person reading the page.

    There is deliberately no Origin check on this path, unlike portal_ws where
    it is mandatory. Runtime terminates the browser's WebSocket and opens its
    own connection to this container, carrying its own Origin — observed as
    `https://cell01.<region>.prod.arp.kepler-analytics.aws.dev`, an internal
    AgentCore hostname. So the browser's origin is not merely sometimes absent
    here, it is never visible: checking the header refuses every legitimate
    join while protecting nothing.

    What authorizes this instead is the same per-escalation token as the other
    door, plus two things the direct door doesn't have: the presigned URL,
    which only the portal-context Lambda's role can mint and only for someone
    who already proved they hold that token, and that Lambda's own CORS — which
    is where a browser's origin genuinely is enforced, at the only step a
    hostile page would have to get through first.
    """
    call_id = str(attach.get("call") or "")
    token = str(attach.get("token") or "")

    call = _authorized(call_id, token)
    if call is None:
        print(f"[handoff] portal attach refused for call {call_id!r} — "
              f"{'not in this instance' if call_id not in LIVE_CALLS else 'bad token'}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": "This call isn't on this instance of the agent, or the link's "
                           "token doesn't match it. If the caller is still on the line, "
                           "ask for a fresh escalation link in Slack.",
            })
        except Exception:
            pass
        # 4403 as well as the message: a frame sent immediately before a close
        # is not guaranteed to arrive through Runtime's proxy, and the close
        # code always does — so the page can explain itself either way.
        await websocket.close(code=4403)
        return

    print(f"[handoff] portal attached to call {call.call_id} over the runtime socket")
    await _serve_portal(websocket, call)


async def _serve_portal(websocket: WebSocket, call: LiveCall) -> None:
    """The bridge itself, once the socket is accepted and authorized: snapshot
    and durable records down, then transcript lines and call audio down while
    join/leave and mic audio come up. Shared by both doors above so a human
    agent gets identical behaviour whichever way they reached the call."""
    call.attach_portal(websocket)

    async def send_context() -> None:
        """The durable records: AgentCore Memory's conversation and the
        escalations row. Sent as its own message, after the snapshot, so two
        AWS round trips never delay the portal opening."""
        try:
            await websocket.send_json({"type": "context", **await load_context(call)})
        except Exception as exc:
            print(f"[handoff] context load failed for {call.call_id}: {exc}")

    # Tasks are held here only so asyncio doesn't collect one nothing else
    # references; each drops itself when it finishes.
    pending: set[asyncio.Task] = set()

    def spawn_context() -> None:
        task = asyncio.create_task(send_context())
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        await websocket.send_json({"type": "call_state", "call": call.snapshot()})
        await call.push_state()  # tell any other portal the watcher count moved
        spawn_context()
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "join":
                await call.join_human(websocket, message.get("agent_name"))
            elif kind == "leave":
                await call.release_human(websocket)
            elif kind == "refresh_context":
                # Memory is written asynchronously during the call, so the
                # human gets to ask again rather than reloading the page.
                spawn_context()
            elif kind == "bidi_audio_input":
                await call.relay_human_audio(websocket, message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[handoff] portal socket error on call {call.call_id}: {exc}")
    finally:
        await call.detach_portal(websocket)
