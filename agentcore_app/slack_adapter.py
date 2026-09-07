"""Talk to Ava from Slack, through the deployed AgentCore Runtime.

    python slack_adapter.py

This process contains **no Ava at all**. It has no gateway connection, no
tools, no guardrails, no model and no conversation history. Every turn is a
single `bedrock-agentcore:InvokeAgentRuntime` call against the same runtime
that serves the browser voice call, which is where all of that lives:

    Slack ── Socket Mode ──> slack_adapter.py ──> InvokeAgentRuntime
                                                       │
                                          AGENT_RUNTIME_ARN  (POST /invocations)
                                                       │
                                        strands_agent.py + text_invocations.py
                                                       │
                                               AgentCore Gateway

So Ava's persona, her tools, her guardrails and her memory are deployed once
and shared. Editing the prompt in the Bedrock console, or changing what the
gateway exposes, changes Slack and voice together with nothing to redeploy
here. This file only has to know how to read Slack and how to write Slack.

**A Slack thread is a runtime session.** The thread's channel and root
timestamp are hashed into a stable `runtimeSessionId`, and AgentCore routes
every invoke for that id back to the same instance — so Ava's history and
guardrail state for that thread stay alive between messages. A new thread is a
new session; `reset` clears one.

Socket Mode is used deliberately: it dials out to Slack, so this needs no
public URL, no API Gateway and no request-signature handling. It's small
enough to run anywhere — a laptop, a tiny Fargate task — precisely because the
agent isn't in here.

Environment:
    SLACK_BOT_TOKEN     xoxb-... (OAuth & Permissions -> Bot User OAuth Token)
    SLACK_APP_TOKEN     xapp-... (App-Level Token, scope connections:write)
    AGENT_RUNTIME_ARN   the runtime to invoke; the same ARN the presign Lambda
                        uses for voice
"""

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict

import boto3
from botocore.config import Config
from dotenv import load_dotenv
from pathlib import Path
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

# This file deliberately does NOT import strands_agent — that's the whole
# point; the agent runs in the runtime, not here. So it loads ../.env itself.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN", "")
AGENT_RUNTIME_REGION = os.getenv("AGENT_RUNTIME_REGION") or os.getenv("AWS_REGION", "us-west-2")
AGENT_RUNTIME_QUALIFIER = os.getenv("AGENT_RUNTIME_QUALIFIER", "DEFAULT")

# Local testing: point this at a `python strands_agent.py` running on your own
# machine and every turn is a plain POST to it instead of an AWS invoke — so
# you can drive real Slack messages through code you're still editing, with no
# build, no push and no UpdateAgentRuntime in the loop. Same idea as
# frontend/index.html accepting a direct ws:// URL instead of the presign
# Lambda. Unset (the default) means the deployed runtime.
AGENT_RUNTIME_ENDPOINT = os.getenv("AGENT_RUNTIME_ENDPOINT", "").strip()

# A turn can call the gateway two or three times before Ava answers, and a
# reasoning model is not fast. Well above that, because a timeout here looks to
# the customer exactly like a failure while the runtime is still working.
_INVOKE_TIMEOUT_SECONDS = int(os.getenv("INVOKE_TIMEOUT_SECONDS", "180"))

# Slack has no bot-typing indicator outside its own Assistant surface, and a
# turn takes several seconds — so the incoming message gets a reaction while
# Ava works, removed when she answers.
_WORKING_REACTION = "hourglass_flowing_sand"

_RESET_COMMANDS = {"reset", "restart", "start over", "new chat", "new conversation"}
_FAILURE_REPLY = (
    "Something went wrong on my end just now. Please try that again, and if it "
    "keeps happening I'll get a colleague to pick this up."
)

# The headers AgentCore Runtime injects on an invoke. Sent explicitly in local
# mode so `POST /invocations` reads the session and user the same way.
SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-User-Id"

_MENTION = re.compile(r"<@[UWB][A-Z0-9]+(\|[^>]*)?>")
# Slack wraps URLs and emails as <target|label> (or bare <target>). The label
# is what the person actually typed, so that's what Ava should read.
_SLACK_LINK = re.compile(r"<(?:https?://|mailto:)([^|>]+)(?:\|([^>]*))?>")


def _clean(text: str) -> str:
    text = _MENTION.sub(" ", text or "")
    text = _SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _session_id(channel_id: str, thread_ts: str) -> str:
    """A stable runtime session id for one Slack thread.

    `runtimeSessionId` must be 33-256 characters, which a raw "C123-1699.1"
    thread key is not. Hashing gives a fixed 71 characters, is deterministic
    (the same thread always resolves to the same session, so AgentCore keeps
    routing it to the same instance), and doesn't put the workspace's channel
    ids into an AWS-side identifier.
    """
    digest = hashlib.sha256(f"{channel_id}/{thread_ts}".encode()).hexdigest()
    return f"slack-thread-{digest}"


class _SeenEvents:
    """Slack re-delivers an event if it thinks we missed the ack. A duplicate
    would mean Ava answering twice — or worse, registering a claim twice. The
    idempotency gate inside the runtime's tools is the real backstop for the
    side effect; this stops the duplicate turn being sent at all."""

    def __init__(self, capacity: int = 2000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def is_new(self, event_id: str | None) -> bool:
        if not event_id:
            return True
        if event_id in self._seen:
            return False
        self._seen[event_id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


class RuntimeClient:
    """Sends a turn to Ava. The only thing in this file that knows anything
    about her, and all it knows is where she is.

    Two ways to reach the same `POST /invocations` handler: the deployed
    runtime through `bedrock-agentcore:InvokeAgentRuntime`, or a local
    `strands_agent.py` over plain HTTP when `AGENT_RUNTIME_ENDPOINT` is set.
    The session and user ids that AgentCore normally injects as headers are
    sent explicitly in local mode, so the handler behaves identically either
    way.
    """

    def __init__(self) -> None:
        self.local_endpoint = AGENT_RUNTIME_ENDPOINT or None
        if self.local_endpoint:
            if not self.local_endpoint.startswith(("http://", "https://")):
                raise RuntimeError(
                    f"AGENT_RUNTIME_ENDPOINT must be an http(s) URL, got {self.local_endpoint!r}"
                )
            self.local_endpoint = self.local_endpoint.rstrip("/")
            if not self.local_endpoint.endswith("/invocations"):
                self.local_endpoint += "/invocations"
            self._client = None
        else:
            if not AGENT_RUNTIME_ARN:
                raise RuntimeError(
                    "Set AGENT_RUNTIME_ARN to invoke the deployed runtime (the same ARN "
                    "the presign Lambda uses for voice, see DEPLOY.md), or "
                    "AGENT_RUNTIME_ENDPOINT to test against a local strands_agent.py"
                )
            self._client = boto3.client(
                "bedrock-agentcore",
                region_name=AGENT_RUNTIME_REGION,
                # Long read timeout for the turn itself; retries left off
                # because a retried POST would be a second turn, not a repeat
                # of the first, and Slack has already been acked.
                config=Config(
                    read_timeout=_INVOKE_TIMEOUT_SECONDS,
                    connect_timeout=10,
                    retries={"max_attempts": 0},
                ),
            )
        # Threads that have already been live once. Only used to decide whether
        # a bare (un-mentioned) reply in a channel thread is meant for Ava.
        self._known_threads: set[str] = set()

    def describe(self) -> str:
        if self.local_endpoint:
            return f"LOCAL {self.local_endpoint}"
        return f"{AGENT_RUNTIME_ARN} ({AGENT_RUNTIME_QUALIFIER}, {AGENT_RUNTIME_REGION})"

    def is_known_thread(self, session_id: str) -> bool:
        return session_id in self._known_threads

    async def invoke(self, session_id: str, user_id: str | None, payload: dict) -> str:
        body = json.dumps({**payload, "user_id": user_id, "session_id": session_id})
        try:
            if self.local_endpoint:
                reply = await self._invoke_local(session_id, user_id, body)
            else:
                # boto3 is synchronous, so it runs on a worker thread — a
                # blocked event loop here would stall every other Slack thread.
                reply = await asyncio.to_thread(self._invoke_runtime, session_id, user_id, body)
        except Exception as e:
            print(f"[slack] invoke failed for {session_id}: {type(e).__name__}: {e}")
            return _FAILURE_REPLY

        self._known_threads.add(session_id)
        return reply or _FAILURE_REPLY

    def _invoke_runtime(self, session_id: str, user_id: str | None, body: str) -> str:
        kwargs = {
            "agentRuntimeArn": AGENT_RUNTIME_ARN,
            "runtimeSessionId": session_id,
            "qualifier": AGENT_RUNTIME_QUALIFIER,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": body.encode(),
        }
        if user_id:
            kwargs["runtimeUserId"] = user_id
        response = self._client.invoke_agent_runtime(**kwargs)
        return json.loads(response["response"].read()).get("reply") or ""

    async def _invoke_local(self, session_id: str, user_id: str | None, body: str) -> str:
        import aiohttp  # ships with slack-bolt's async support

        headers = {
            "Content-Type": "application/json",
            SESSION_ID_HEADER: session_id,
        }
        if user_id:
            headers[USER_ID_HEADER] = user_id
        timeout = aiohttp.ClientTimeout(total=_INVOKE_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.local_endpoint, data=body, headers=headers) as response:
                response.raise_for_status()
                return (await response.json()).get("reply") or ""


def build_app(runtime: RuntimeClient) -> AsyncApp:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set — see the Slack section of the README")

    app = AsyncApp(token=SLACK_BOT_TOKEN)
    seen = _SeenEvents()

    async def respond(event: dict, client, event_id: str | None) -> None:
        if not seen.is_new(event_id):
            print(f"[slack] ignoring duplicate delivery of {event_id}")
            return

        text = _clean(event.get("text", ""))
        if not text:
            return

        channel_id = event["channel"]
        # A message that isn't in a thread starts one, rooted at itself, so
        # Ava's replies stay together and the next message continues the same
        # runtime session.
        thread_ts = event.get("thread_ts") or event["ts"]
        session_id = _session_id(channel_id, thread_ts)

        if text.lower().rstrip(".!") in _RESET_COMMANDS:
            reply = await runtime.invoke(session_id, event.get("user"), {"reset": True})
            await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=reply)
            return

        await _react(client, channel_id, event["ts"], add=True)
        try:
            reply = await runtime.invoke(session_id, event.get("user"), {"text": text})
        finally:
            await _react(client, channel_id, event["ts"], add=False)

        await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=reply)

    @app.event("app_mention")
    async def handle_mention(event, client, body):
        await respond(event, client, body.get("event_id"))

    @app.event("message")
    async def handle_message(event, client, body):
        """Two cases reach Ava without a mention: a direct message, and a reply
        in a thread where she's already talking. Everything else in a channel
        she's a member of is other people's conversation, and is left alone.

        `subtype` filters out joins, edits, deletions and file-share notices;
        `bot_id` filters out Ava's own replies, which would otherwise loop.
        """
        if event.get("subtype") or event.get("bot_id"):
            return
        if event.get("channel_type") != "im":
            thread_ts = event.get("thread_ts")
            if not thread_ts or not runtime.is_known_thread(_session_id(event["channel"], thread_ts)):
                return
        await respond(event, client, body.get("event_id"))

    return app


async def _react(client, channel: str, timestamp: str, *, add: bool) -> None:
    """Add or remove the working reaction. Never worth failing a turn over —
    the bot may simply lack `reactions:write`, or the message may be gone."""
    try:
        call = client.reactions_add if add else client.reactions_remove
        await call(channel=channel, timestamp=timestamp, name=_WORKING_REACTION)
    except Exception as e:
        print(f"[slack] could not {'add' if add else 'remove'} the working reaction: {e}")


async def run() -> None:
    if not SLACK_APP_TOKEN:
        raise RuntimeError(
            "SLACK_APP_TOKEN is not set — Socket Mode needs an app-level token "
            "(xapp-...) with connections:write, see the Slack section of the README"
        )

    runtime = RuntimeClient()
    print(f"[slack] sending turns to: {runtime.describe()}")
    handler = AsyncSocketModeHandler(build_app(runtime), SLACK_APP_TOKEN)
    try:
        print("[slack] connecting to Slack over Socket Mode...")
        await handler.start_async()
    finally:
        try:
            await handler.close_async()
        except Exception:
            pass


if __name__ == "__main__":
    print("Starting Ava's Slack adapter...")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
