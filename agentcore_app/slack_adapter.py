"""Talk to Ava from Slack.

    python slack_adapter.py

This is an adapter, not a second agent. Everything that makes Ava *her* is
imported straight out of `strands_agent.py` and used unmodified:

    build_gateway_tools()          the live gateway tools, wrapped in every
                                   guardrail — grounding, fabrication,
                                   idempotency, the high-value-claim gate,
                                   output sanitizing
    get_system_prompt()            the persona, including a prompt you're
                                   managing in Bedrock Prompt Management
    build_memory_session_manager() AgentCore Memory, short- and long-term
    _RecentUserDigits              what the guardrails check arguments against

Nothing in `strands_agent.py` changes, and the voice call on `/ws` is
untouched. Two things have to differ here, and only two:

1. **The model.** Nova Sonic is speech-to-speech — it has no text mode worth
   using in a chat window — so a Slack turn is answered by Claude on Bedrock
   (`TEXT_MODEL_ID`) driving the exact same tools.
2. **The last paragraph of the prompt.** SYSTEM_INSTRUCTION was written for a
   phone call, so `_TEXT_CHANNEL_OVERLAY` below overrides the handful of rules
   that are about *speech* — read every digit back and get a yes; one or two
   sentences then stop — and leaves every business rule alone.

How Slack maps onto a conversation: **a thread is a call.** Everything under
one root message shares Ava's history and her guardrail state; a new thread
starts fresh, and so does a thread that's been quiet for
`SLACK_IDLE_TIMEOUT_SECONDS`. The Slack member id is the memory actor id, so
someone who messaged last week is recognised this week. Their mobile number is
never inferred from Slack — Ava asks for it and looks it up exactly as she
would on a call.

Socket Mode is used deliberately: it dials out to Slack, so there's no public
URL, no API Gateway and no request-signature handling. The same command works
from a laptop and from the deployed container.
"""

import asyncio
import os
import re
import time
from collections import OrderedDict

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

# Importing this is what makes the adapter an adapter: the persona, the tools
# and every guardrail come from your agent, not from a copy of it. It also
# loads ../.env and sets up tracing as a side effect, which is what we want.
from strands_agent import (
    GATEWAY_REGION,
    GATEWAY_URL,
    _RecentUserDigits,
    build_gateway_tools,
    build_memory_session_manager,
    get_system_prompt,
)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")

# Claude on Bedrock, as an inference profile id — the "global." prefix routes
# to whichever region has capacity. Override with TEXT_MODEL_ID if you'd
# rather run a different one.
TEXT_MODEL_ID = os.getenv("TEXT_MODEL_ID", "global.anthropic.claude-opus-5")
TEXT_MODEL_REGION = os.getenv("TEXT_MODEL_REGION", GATEWAY_REGION)

# How long a thread can sit untouched before Ava forgets it and closes its
# gateway connection. The next message in that thread starts a fresh
# conversation (with whatever long-term memory recalls, if MEMORY_ID is set).
SLACK_IDLE_TIMEOUT_SECONDS = int(os.getenv("SLACK_IDLE_TIMEOUT_SECONDS", "1800"))
_SWEEP_INTERVAL_SECONDS = 120

# Slack has no bot-typing indicator outside its own Assistant surface, and a
# turn that hits the gateway takes a few seconds — so the incoming message
# gets a reaction while Ava works, removed when she answers.
_WORKING_REACTION = "hourglass_flowing_sand"

_RESET_COMMANDS = {"reset", "restart", "start over", "new chat", "new conversation"}
_RESET_REPLY = "Okay — I've cleared this thread. Tell me what you need and we'll start fresh."
_FAILURE_REPLY = (
    "Something went wrong on my end just now. Please try that again, and if it "
    "keeps happening I'll get a colleague to pick this up."
)

_MENTION = re.compile(r"<@[UWB][A-Z0-9]+(\|[^>]*)?>")
# Slack wraps URLs and emails as <target|label> (or bare <target>). The label
# is what the person actually typed, so that's what Ava should read.
_SLACK_LINK = re.compile(r"<(?:https?://|mailto:)([^|>]+)(?:\|([^>]*))?>")


# Appended after SYSTEM_INSTRUCTION (or your Bedrock-managed prompt). Every
# rule in there about *speech* is either wrong here or actively annoying —
# reading a phone number back one digit at a time to someone who typed it is
# the clearest example — so this is written to win where the two conflict, and
# to touch nothing else.
_TEXT_CHANNEL_OVERLAY = """
CHANNEL OVERRIDE — this conversation is happening in a Slack chat, not on a phone call. Everything above was written for a spoken call. Where the two conflict, this section wins. Everything not mentioned here — never inventing a value, checking before promising, three attempts then a person, when to escalate, and all the business rules — applies exactly as written.
Nothing you write is spoken. There is no text-to-speech and no speech-to-text, so there is no mishearing and nothing to sound out. Never say "call", "caller", "on the line", "I heard", or "say that again" — it's a message, a customer, a chat.
Do not read values back character by character and do not ask for digit-by-digit confirmation. They typed it; you have it exactly. Use what they sent as-is. Only ask them to confirm something if it is genuinely ambiguous or they contradicted themselves — a correction still replaces the old value completely.
Write dates and amounts in plain, normal form ("16 August 2026", "45,000"), not spelled out as speech.
Still one thing at a time: each reply asks for at most one thing. Keep replies to a couple of short sentences — this is a chat window, not an email. No greetings after the first message.
Open your first reply with: "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent." If their first message already says what they need, add that line and then get straight on with it in the same reply.
Formatting: plain text, or Slack's own markup — *bold*, _italic_, `code`, and "- " bullets. Never use markdown headings, tables, or **double-asterisk** bold; Slack renders those literally.
"""


def _clean(text: str) -> str:
    text = _MENTION.sub(" ", text or "")
    text = _SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _reply_text(result) -> str:
    """The visible text of an AgentResult, with non-text blocks (thinking,
    tool use) left out."""
    blocks = (getattr(result, "message", None) or {}).get("content") or []
    parts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


class _Thread:
    """One live Slack thread: an Ava with her own history, her own gateway
    connection, and her own guardrail state."""

    def __init__(self, agent: Agent, mcp_client: MCPClient, recent_digits: _RecentUserDigits) -> None:
        self.agent = agent
        self.mcp_client = mcp_client
        self.recent_digits = recent_digits
        self.last_used = time.monotonic()
        # Slack can deliver two messages in a thread before the first reply is
        # out; Strands would raise on the concurrent invocation. Serializing
        # means the second simply waits its turn, which is what a person
        # expects anyway.
        self.lock = asyncio.Lock()

    def close(self) -> None:
        try:
            self.mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"[slack] error closing gateway connection: {e}")


class _SeenEvents:
    """Slack re-delivers an event if it thinks we missed the ack. A duplicate
    would mean Ava answering twice — or worse, registering a claim twice. The
    idempotency gate inside `build_gateway_tools` is the real backstop for the
    side effect; this stops the duplicate turn reaching the model at all."""

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


class AvaSlackAdapter:
    """Holds the live threads and answers one Slack message at a time."""

    def __init__(self) -> None:
        self._threads: dict[str, _Thread] = {}
        self._registry_lock = asyncio.Lock()
        self._model: BedrockModel | None = None
        self._seen = _SeenEvents()

    def _text_model(self) -> BedrockModel:
        if self._model is None:
            print(f"[slack] text model: {TEXT_MODEL_ID} (region {TEXT_MODEL_REGION})")
            self._model = BedrockModel(model_id=TEXT_MODEL_ID, region_name=TEXT_MODEL_REGION)
        return self._model

    # ------------------------------------------------------------- threads

    async def _open(self, thread_key: str, slack_user_id: str | None) -> _Thread:
        """Build a fresh Ava for one Slack thread — the text-channel
        equivalent of what `voice_chat` does when a socket connects."""
        recent_digits = _RecentUserDigits()
        mcp_client = MCPClient(
            transport_callable=lambda: aws_iam_streamablehttp_client(
                endpoint=GATEWAY_URL,
                aws_region=GATEWAY_REGION,
                aws_service="bedrock-agentcore",
            )
        )

        async def _no_ui_to_sync(_value: str) -> None:
            """`build_gateway_tools` reports a newly-confirmed mobile number so
            the browser UI can update its field. Slack has no such field, so
            this is a no-op — the number is still recorded in
            `known_mobile_number` for the guardrails."""

        try:
            tools, base_prompt = await asyncio.gather(
                build_gateway_tools(mcp_client, recent_digits, _no_ui_to_sync, [None]),
                get_system_prompt(),
            )
        except Exception:
            try:
                mcp_client.stop(None, None, None)
            except Exception:
                pass
            raise

        # Same reason voice_chat appends this: neither SYSTEM_INSTRUCTION nor a
        # Bedrock-managed prompt knows today's date, so "the incident was
        # yesterday" has nothing real to resolve against and the model invents
        # one. Computed per conversation, never baked into prompt text.
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc)
        system_prompt = (
            f"{base_prompt}\n\n"
            f"{_TEXT_CHANNEL_OVERLAY.strip()}\n\n"
            f"Today's date is {today:%Y-%m-%d} ({today:%A}), UTC. Resolve any "
            'relative date given to you ("yesterday", "last Monday", "three days '
            'ago") against this, in ISO 8601 (YYYY-MM-DD) format for any tool '
            "argument — never invent a date."
        )

        # A Slack member id is stable across threads and across weeks, so it's
        # what memory is filed under. Deliberately *not* a phone number: Slack
        # doesn't prove one, and guessing would attach one person's history to
        # another's. Ava asks for the number in conversation, as on a call.
        actor_id = f"slack-{slack_user_id}" if slack_user_id else f"slack-anon-{thread_key}"

        # On a worker thread: with MEMORY_ID set, both the session manager and
        # the Agent constructor do blocking AWS reads.
        def _build() -> Agent:
            return Agent(
                model=self._text_model(),
                tools=tools,
                system_prompt=system_prompt,
                session_manager=build_memory_session_manager(actor_id, thread_key),
                agent_id="ava-slack-agent",
                name="Ava",
                # Strands' default handler prints the reply to stdout as it
                # streams; the reply is already going to Slack.
                callback_handler=None,
            )

        agent = await asyncio.to_thread(_build)
        print(f"[slack] opened thread {thread_key} (actor={actor_id})")
        return _Thread(agent, mcp_client, recent_digits)

    async def _thread_for(self, thread_key: str, slack_user_id: str | None) -> _Thread:
        async with self._registry_lock:
            existing = self._threads.get(thread_key)
            if existing is not None:
                return existing

        # Built outside the lock: this does a gateway round trip, and holding
        # the registry lock through it would stall every other thread.
        opened = await self._open(thread_key, slack_user_id)

        async with self._registry_lock:
            # A second message in the same thread may have raced us. First one
            # in wins, so a thread never ends up with two Avas.
            existing = self._threads.get(thread_key)
            if existing is not None:
                opened.close()
                return existing
            self._threads[thread_key] = opened
            return opened

    async def end(self, thread_key: str) -> None:
        async with self._registry_lock:
            thread = self._threads.pop(thread_key, None)
        if thread is not None:
            thread.close()
            print(f"[slack] ended thread {thread_key}")

    async def reap_idle_threads(self) -> None:
        """Close threads nobody has spoken in for a while, so a busy workspace
        doesn't accumulate open gateway connections."""
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                cutoff = time.monotonic() - SLACK_IDLE_TIMEOUT_SECONDS
                async with self._registry_lock:
                    stale = [k for k, t in self._threads.items() if t.last_used < cutoff]
                    threads = [self._threads.pop(k) for k in stale]
                for key, thread in zip(stale, threads):
                    print(f"[slack] closing idle thread {key}")
                    thread.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[slack] idle sweep failed: {e}")

    async def close_all(self) -> None:
        async with self._registry_lock:
            threads = list(self._threads.values())
            self._threads.clear()
        for thread in threads:
            thread.close()

    # -------------------------------------------------------------- replies

    async def answer(self, thread_key: str, slack_user_id: str | None, text: str) -> str:
        thread = await self._thread_for(thread_key, slack_user_id)
        async with thread.lock:
            thread.last_used = time.monotonic()
            # Recorded before the model sees it, so the fabrication and
            # stale-number gates can check a tool argument against what was
            # actually typed.
            thread.recent_digits.record(text)
            try:
                result = await thread.agent.invoke_async(text)
            except Exception as e:
                print(f"[slack] turn failed in {thread_key}: {type(e).__name__}: {e}")
                return _FAILURE_REPLY
            thread.last_used = time.monotonic()

        answer = _reply_text(result)
        if not answer:
            # The model ended its turn with nothing to say. Better to say
            # something than to leave the message unanswered.
            print(f"[slack] empty reply in {thread_key} (stop_reason={getattr(result, 'stop_reason', None)})")
            return "I'm still with you — could you tell me a bit more?"
        return answer

    def is_live(self, thread_key: str) -> bool:
        return thread_key in self._threads

    def is_new_event(self, event_id: str | None) -> bool:
        return self._seen.is_new(event_id)


def build_app(adapter: AvaSlackAdapter) -> AsyncApp:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set — see the Slack section of the README")

    app = AsyncApp(token=SLACK_BOT_TOKEN)

    async def respond(event: dict, client, event_id: str | None) -> None:
        if not adapter.is_new_event(event_id):
            print(f"[slack] ignoring duplicate delivery of {event_id}")
            return

        text = _clean(event.get("text", ""))
        if not text:
            return

        channel_id = event["channel"]
        # A message that isn't in a thread starts one, rooted at itself, so
        # Ava's replies stay together and the next message continues the same
        # conversation.
        thread_ts = event.get("thread_ts") or event["ts"]
        thread_key = f"{channel_id}-{thread_ts}"

        if text.lower().rstrip(".!") in _RESET_COMMANDS:
            await adapter.end(thread_key)
            await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=_RESET_REPLY)
            return

        await _react(client, channel_id, event["ts"], add=True)
        try:
            reply = await adapter.answer(thread_key, event.get("user"), text)
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
            if not thread_ts or not adapter.is_live(f"{event['channel']}-{thread_ts}"):
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

    adapter = AvaSlackAdapter()
    reaper = asyncio.create_task(adapter.reap_idle_threads())
    handler = AsyncSocketModeHandler(build_app(adapter), SLACK_APP_TOKEN)
    try:
        print("[slack] connecting to Slack over Socket Mode...")
        await handler.start_async()
    finally:
        reaper.cancel()
        try:
            await handler.close_async()
        except Exception:
            pass
        await adapter.close_all()


if __name__ == "__main__":
    print("Starting Ava's Slack adapter...")
    print(f"Gateway: {GATEWAY_URL}")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
