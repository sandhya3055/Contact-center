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
3. Joining bridges audio both ways and takes Ava off the line: the caller's
   mic stops being fed to Nova Sonic and is relayed to the human's browser
   instead, the human's mic is relayed to the caller, and anything Ava tries
   to say is dropped. Leaving hands the call back to Ava.

    caller browser <-- audio --> /ws  <-- audio -->  /portal/ws  <-- --> human browser
                                  |
                                  +-- Nova Sonic (starved of input while a human is on)

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
import json
import os
import secrets
import time
import urllib.request
import uuid
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

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
MEMORY_ID = os.getenv("MEMORY_ID", "")
MEMORY_REGION = os.getenv("MEMORY_REGION") or os.getenv("AWS_REGION", "us-west-2")
ESCALATIONS_TABLE = os.getenv("ESCALATIONS_TABLE", "escalations")
ESCALATIONS_REGION = os.getenv("ESCALATIONS_REGION") or os.getenv("AWS_REGION", "us-west-2")

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

        # How this call is keyed in AgentCore Memory, so the portal can read
        # the conversation back from the durable record rather than only from
        # this process's transcript list. Set by strands_agent.py once the
        # caller's number (and so the actor id) is known.
        self.memory_actor_id: str | None = None
        self.memory_session_id: str | None = None

        # Set by strands_agent.py once the caller's socket is up: how to send
        # an event to the caller's browser.
        self.customer_send: Callable[[dict[str, Any]], Awaitable[None]] | None = None

        # Portal sockets watching this call. Everyone attached hears the call;
        # only `human_socket` (the one that pressed Join) can speak into it.
        self.portals: set[WebSocket] = set()
        self.human_socket: WebSocket | None = None

        # The in-flight Slack notification, parked here so asyncio doesn't
        # garbage-collect a task nothing else holds a reference to.
        self.slack_task: asyncio.Task | None = None

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
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "escalation": self.escalation,
            "transcript": self.transcript,
            "human_label": self.human_label,
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
        await self.push_state()
        await self.to_customer({"type": "bidi_handoff", "state": STATE_ESCALATED})
        return self.portal_url()

    def portal_url(self) -> str:
        return f"{PORTAL_BASE_URL}/portal?call={self.call_id}&token={self.join_token}"

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
        """Put a human on the line and take Ava off it."""
        if self.state == STATE_ENDED:
            await self._send(socket, {"type": "error", "message": "This call has already ended."})
            return
        if self.human_socket is not None and self.human_socket is not socket:
            await self._send(socket, {"type": "error", "message": "Another agent is already on this call."})
            return

        self.human_socket = socket
        self.human_label = (label or "").strip()[:60] or "a specialist"
        self.state = STATE_HUMAN
        await self.push_state()
        await self.to_customer({
            "type": "bidi_handoff",
            "state": STATE_HUMAN,
            "agent_name": self.human_label,
        })
        print(f"[handoff] {self.human_label} joined call {self.call_id}")

    async def release_human(self, socket: WebSocket, reason: str = "left") -> None:
        """Hand the call back to Ava."""
        if self.human_socket is not socket:
            return
        self.human_socket = None
        label = self.human_label
        self.human_label = None
        if self.state != STATE_ENDED:
            self.state = STATE_ESCALATED
            await self.to_customer({"type": "bidi_handoff", "state": STATE_AI})
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

_boto3 = None  # imported lazily: only needed when a portal actually asks


def _client(service: str, region: str):
    global _boto3
    if _boto3 is None:
        import boto3 as _b
        _boto3 = _b
    return _boto3.client(service, region_name=region)


def _plain(value: Any) -> Any:
    """DynamoDB numbers come back as Decimal, which json can't encode."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if type(value).__name__ == "Decimal":
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _fetch_escalation_row(escalation_id: str) -> dict[str, Any] | None:
    """Blocking GetItem on the escalations table — call via asyncio.to_thread."""
    from boto3.dynamodb.types import TypeDeserializer

    client = _client("dynamodb", ESCALATIONS_REGION)
    response = client.get_item(
        TableName=ESCALATIONS_TABLE, Key={"escalationId": {"S": escalation_id}}
    )
    item = response.get("Item")
    if not item:
        return None
    deserialize = TypeDeserializer().deserialize
    return _plain({key: deserialize(value) for key, value in item.items()})


def _fetch_memory_transcript(actor_id: str, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """The conversation as AgentCore Memory holds it (blocking; use a thread).

    Events come back newest-first and mix two payload kinds: `conversational`
    (a turn) and `blob` (the agent's own serialized state, which is not
    conversation and is skipped). A turn's text is itself a JSON document
    wrapping the message, so the role comes from inside it rather than from
    the event envelope.
    """
    client = _client("bedrock-agentcore", MEMORY_REGION)
    events: list[dict[str, Any]] = []
    token = None
    while len(events) < limit:
        kwargs = {"memoryId": MEMORY_ID, "actorId": actor_id, "sessionId": session_id,
                  "maxResults": min(100, limit - len(events))}
        if token:
            kwargs["nextToken"] = token
        page = client.list_events(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break

    lines: list[dict[str, Any]] = []
    for event in events:
        stamp = event.get("eventTimestamp")
        at = stamp.timestamp() if hasattr(stamp, "timestamp") else _now()
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


async def load_context(call: LiveCall) -> dict[str, Any]:
    """The portal's view of the durable records behind this call.

    Both lookups are best-effort and independent: a missing memory id or an
    IAM gap on one of them must still leave the human with the other, and
    with the live call itself, rather than an error page.
    """
    context: dict[str, Any] = {"memory": [], "record": None, "notes": []}

    escalation_id = (call.escalation or {}).get("escalation_id")
    if escalation_id:
        try:
            row = await asyncio.to_thread(_fetch_escalation_row, escalation_id)
            context["record"] = row
            if row is None:
                context["notes"].append(
                    f"No row for {escalation_id} in the {ESCALATIONS_TABLE} table.")
        except Exception as exc:
            context["notes"].append(f"Couldn't read the escalations table: {exc}")
            print(f"[handoff] escalations table read failed: {exc}")

    if MEMORY_ID and call.memory_actor_id and call.memory_session_id:
        try:
            context["memory"] = await asyncio.to_thread(
                _fetch_memory_transcript, call.memory_actor_id, call.memory_session_id)
            if not context["memory"]:
                context["notes"].append(
                    "AgentCore Memory has no turns for this session yet — it's written "
                    "asynchronously during the call, so it can lag by a few seconds.")
        except Exception as exc:
            context["notes"].append(f"Couldn't read AgentCore Memory: {exc}")
            print(f"[handoff] memory read failed: {exc}")
    elif not MEMORY_ID:
        context["notes"].append("MEMORY_ID isn't set, so this call isn't being persisted.")

    return context


def register_call(mobile_number: str | None = None) -> LiveCall:
    _prune_calls()
    call = LiveCall(mobile_number)
    LIVE_CALLS[call.call_id] = call
    return call


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
        {"type": "mrkdwn", "text": f"*Caller*\n{call.mobile_number or 'not provided'}"},
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
                        "opening the portal lets you listen before you join",
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


def _authorized(call_id: str, token: str) -> LiveCall | None:
    call = LIVE_CALLS.get(call_id)
    return call if call is not None and call.token_matches(token) else None


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
    """The human agent's end of the call: transcript updates and call audio
    down, join/leave and mic audio up."""
    call = _authorized(call_id, token)
    if call is None:
        # Refused before accept(), so an unauthorized socket never reaches a
        # live call at all. 4403 rather than a plain close so the portal can
        # tell "not allowed" apart from a dropped connection.
        await websocket.close(code=4403)
        return

    await websocket.accept()
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
        print(f"[handoff] portal socket error on call {call_id}: {exc}")
    finally:
        await call.detach_portal(websocket)
