"""The runtime's text entrypoint: `POST /invocations`.

`strands_agent.py` already serves `/ws`, where Nova Sonic handles a voice
call. This adds the other half of AgentCore Runtime's HTTP contract — the REST
invoke path — so the *same deployed runtime* can also hold a typed
conversation. Slack, WhatsApp, a support widget: none of them need their own
copy of Ava, they just invoke this.

Everything that makes Ava *her* is imported from `strands_agent.py` and used
unmodified — the live gateway tools with every guardrail around them, the
persona prompt (including one you're managing in Bedrock Prompt Management),
AgentCore Memory. Two things have to differ, and only two:

1. **The model.** Nova Sonic is speech-to-speech, so a typed turn is answered
   by Claude (or Nova) on Bedrock — `TEXT_MODEL_ID` — driving the same tools.
2. **The last paragraph of the prompt.** SYSTEM_INSTRUCTION was written for a
   phone call, so `_TEXT_CHANNEL_OVERLAY` overrides the handful of rules that
   are about *speech* — read every digit back and get a yes, one or two
   sentences then stop — and leaves every business rule alone.

**One conversation per `runtimeSessionId`.** AgentCore routes a given session
id back to the same instance, so the Ava built for a session — her history,
her gateway connection, her guardrail state — stays in memory between invokes.
The caller decides what a session *means*: `slack_adapter.py` maps one Slack
thread to one session id.

Request body:  {"text": "...", "reset": false}
Response body: {"reply": "...", "session_id": "..."}
"""

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client


def _agent():
    """The running `strands_agent` module — resolved at call time, never
    imported at module scope.

    `strands_agent.py` imports this file to mount the router, so a top-level
    `from strands_agent import ...` here is circular. Under uvicorn that
    happens to work (the half-built module is already in `sys.modules` under
    the name we ask for), but `python strands_agent.py` runs it as `__main__`,
    so the same import loads a *second, independent copy* of the whole agent —
    two Nova Sonic models, telemetry set up twice — and then fails outright
    when that copy re-enters this module mid-initialisation.

    Looking the module up instead of importing it means there is exactly one
    Ava in the process however it was started.
    """
    for name in ("strands_agent", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "build_gateway_tools"):
            return module
    import strands_agent  # not loaded yet (e.g. a test importing this first)

    return strands_agent


# Claude on Bedrock as an inference profile id — the "global." prefix routes to
# whichever region has capacity. A bare model id will not work: most current
# models have no on-demand throughput.
TEXT_MODEL_ID = os.getenv("TEXT_MODEL_ID", "global.anthropic.claude-opus-5")
TEXT_MODEL_REGION = os.getenv("TEXT_MODEL_REGION") or os.getenv("AWS_REGION", "us-west-2")

# How long a conversation can sit untouched before it's torn down and its
# gateway connection released. AgentCore reclaims an idle session's instance
# on its own timer too; this is the in-process equivalent so a long-lived
# instance serving many sessions doesn't accumulate them.
TEXT_IDLE_TIMEOUT_SECONDS = int(os.getenv("TEXT_IDLE_TIMEOUT_SECONDS", "1800"))
_SWEEP_INTERVAL_SECONDS = 120

SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-User-Id"

_FAILURE_REPLY = (
    "Something went wrong on my end just now. Please try that again, and if it "
    "keeps happening I'll get a colleague to pick this up."
)
_EMPTY_REPLY = "I'm still with you — could you tell me a bit more?"
_RESET_REPLY = "Okay — I've cleared this thread. Tell me what you need and we'll start fresh."


# Appended after SYSTEM_INSTRUCTION (or your Bedrock-managed prompt).
_TEXT_CHANNEL_OVERLAY = """
CHANNEL OVERRIDE — this conversation is happening in a text chat, not on a phone call. Everything above was written for a spoken call. Where the two conflict, this section wins. Everything not mentioned here — never inventing a value, checking before promising, three attempts then a person, when to escalate, and all the business rules — applies exactly as written.
Nothing you write is spoken. There is no text-to-speech and no speech-to-text, so there is no mishearing and nothing to sound out. Never say "call", "caller", "on the line", "I heard", or "say that again" — it's a message, a customer, a chat.
Do not read values back character by character and do not ask for digit-by-digit confirmation. They typed it; you have it exactly. Use what they sent as-is. Only ask them to confirm something if it is genuinely ambiguous or they contradicted themselves — a correction still replaces the old value completely.
Write dates and amounts in plain, normal form ("16 August 2026", "45,000"), not spelled out as speech.
Still one thing at a time: each reply asks for at most one thing. Keep replies to a couple of short sentences — this is a chat window, not an email. No greetings after the first message.
Open your first reply with: "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent." If their first message already says what they need, add that line and then get straight on with it in the same reply.
Formatting: plain text, or the *bold* / _italic_ / `code` markup chat apps share, and "- " bullets. Never use markdown headings, tables, or **double-asterisk** bold; they render literally.
"""


# Some models write their reasoning into the *visible* text as literal
# `<thinking>` tags rather than a separate content block — observed from
# nova-lite-v1:0 on this tool set. Ava's prompt is explicit that there is no
# private channel, so anything like that reaching a customer is raw internal
# monologue. Stripped here rather than prompted away, because a prompt rule is
# exactly what a model in that state has stopped following.
_LEAKED_REASONING = re.compile(
    r"<\s*(thinking|reasoning|scratchpad)\s*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _is_transient_model_error(error: Exception) -> bool:
    """Whether a failed turn is worth immediately retrying.

    `modelStreamErrorException` means the model emitted a malformed tool-use
    block part-way through the stream — a fault in the generation, not in the
    request, so the identical turn usually succeeds on a second attempt.
    Throttling is the other retryable case. Anything else (a bad model id, a
    denied permission, an expired credential) fails identically forever and
    should surface straight away.
    """
    text = f"{type(error).__name__}: {error}"
    return any(
        marker in text
        for marker in ("modelStreamErrorException", "ThrottlingException", "ModelThrottled")
    )


def _reply_text(result) -> str:
    blocks = (getattr(result, "message", None) or {}).get("content") or []
    parts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    cleaned = _LEAKED_REASONING.sub("", text).strip()
    if cleaned != text:
        print("[text] stripped leaked reasoning tags from the reply")
    # An unclosed tag can't be matched as a pair; if one is left over, the rest
    # of the message is monologue too and none of it should be sent.
    if re.search(r"<\s*(thinking|reasoning|scratchpad)\s*>", cleaned, re.IGNORECASE):
        print("[text] reply contained an unclosed reasoning tag — dropping it")
        return ""
    return cleaned


class _Conversation:
    """One live text conversation: an Ava with her own history, her own
    gateway connection, and her own guardrail state."""

    def __init__(self, agent: Agent, mcp_client: MCPClient, recent_digits) -> None:
        self.agent = agent
        self.mcp_client = mcp_client
        self.recent_digits = recent_digits
        self.last_used = time.monotonic()
        # A channel can deliver two messages before the first reply is out;
        # Strands would raise on the concurrent invocation. Serializing means
        # the second simply waits its turn.
        self.lock = asyncio.Lock()

    def close(self) -> None:
        try:
            self.mcp_client.stop(None, None, None)
        except Exception as e:
            print(f"[text] error closing gateway connection: {e}")


class TextConversations:
    """Every live text conversation on this instance, keyed by session id."""

    def __init__(self) -> None:
        self._live: dict[str, _Conversation] = {}
        self._registry_lock = asyncio.Lock()
        self._model: BedrockModel | None = None
        self._reaper: asyncio.Task | None = None

    def _text_model(self) -> BedrockModel:
        if self._model is None:
            print(f"[text] model: {TEXT_MODEL_ID} (region {TEXT_MODEL_REGION})")
            self._model = BedrockModel(model_id=TEXT_MODEL_ID, region_name=TEXT_MODEL_REGION)
        return self._model

    async def _open(self, session_id: str, user_id: str | None) -> _Conversation:
        """Build a fresh Ava for one session — the text equivalent of what
        `voice_chat` does when a socket connects."""
        agent_module = _agent()
        recent_digits = agent_module._RecentUserDigits()
        mcp_client = MCPClient(
            transport_callable=lambda: aws_iam_streamablehttp_client(
                endpoint=agent_module.GATEWAY_URL,
                aws_region=agent_module.GATEWAY_REGION,
                aws_service="bedrock-agentcore",
            )
        )

        async def _no_ui_to_sync(_value: str) -> None:
            """`build_gateway_tools` reports a newly-confirmed mobile number so
            the browser UI can update its field. A text channel has no such
            field, so this is a no-op — the number is still recorded for the
            guardrails."""

        try:
            tools, base_prompt = await asyncio.gather(
                agent_module.build_gateway_tools(
                    mcp_client,
                    recent_digits,
                    _no_ui_to_sync,
                    [None],
                    # Typed, not spoken: there's no speech-to-text here, so
                    # the dropped-trailing-digit check has nothing real to
                    # catch and false-positives whenever someone repeats
                    # their number to confirm it.
                    check_truncation=False,
                ),
                agent_module.get_system_prompt(),
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
        today = datetime.now(timezone.utc)
        system_prompt = (
            f"{base_prompt}\n\n"
            f"{_TEXT_CHANNEL_OVERLAY.strip()}\n\n"
            f"Today's date is {today:%Y-%m-%d} ({today:%A}), UTC. Resolve any "
            'relative date given to you ("yesterday", "last Monday", "three days '
            'ago") against this, in ISO 8601 (YYYY-MM-DD) format for any tool '
            "argument — never invent a date."
        )

        # The channel's own stable id for this person (a Slack member id), so
        # someone who wrote last week is recognised this week. Deliberately
        # *not* a phone number: no chat platform proves one, and guessing would
        # attach one person's history to another's. Ava asks for the number in
        # conversation, exactly as she does on a call.
        actor_id = user_id or f"anon-{session_id}"

        def _build() -> Agent:
            return Agent(
                model=self._text_model(),
                tools=tools,
                system_prompt=system_prompt,
                session_manager=agent_module.build_memory_session_manager(actor_id, session_id),
                agent_id="ava-text-agent",
                name="Ava",
                # Strands' default handler prints the reply to stdout as it
                # streams; the reply is already going back to the caller.
                callback_handler=None,
            )

        # On a worker thread: with MEMORY_ID set, both the session manager and
        # the Agent constructor do blocking AWS reads.
        agent = await asyncio.to_thread(_build)
        print(f"[text] opened session {session_id} (actor={actor_id})")
        return _Conversation(agent, mcp_client, recent_digits)

    async def _conversation_for(self, session_id: str, user_id: str | None) -> _Conversation:
        self._ensure_reaper()
        async with self._registry_lock:
            existing = self._live.get(session_id)
            if existing is not None:
                return existing

        # Built outside the lock: this does a gateway round trip, and holding
        # the registry lock through it would stall every other session.
        opened = await self._open(session_id, user_id)

        async with self._registry_lock:
            # A second message for the same session may have raced us. First
            # one in wins, so a session never ends up with two Avas.
            existing = self._live.get(session_id)
            if existing is not None:
                opened.close()
                return existing
            self._live[session_id] = opened
            return opened

    async def answer(self, session_id: str, user_id: str | None, text: str) -> str:
        conversation = await self._conversation_for(session_id, user_id)
        async with conversation.lock:
            conversation.last_used = time.monotonic()
            # Recorded before the model sees it, so the fabrication and
            # stale-number gates can check a tool argument against what was
            # actually typed.
            conversation.recent_digits.record(text)
            try:
                result = await conversation.agent.invoke_async(text)
            except Exception as e:
                if not _is_transient_model_error(e):
                    print(f"[text] turn failed in {session_id}: {type(e).__name__}: {e}")
                    return _FAILURE_REPLY
                # Safe to replay even if a tool already fired before the stream
                # broke: an identical call within IDEMPOTENCY_WINDOW_SECONDS is
                # served from _IdempotencyGateTool's cache rather than
                # re-executed, so a retry can't double a side effect like
                # registering a claim.
                print(f"[text] {session_id}: {e} — retrying the turn once")
                try:
                    result = await conversation.agent.invoke_async(text)
                except Exception as retry_error:
                    print(f"[text] retry failed in {session_id}: {type(retry_error).__name__}: {retry_error}")
                    return _FAILURE_REPLY
            conversation.last_used = time.monotonic()

        answer = _reply_text(result)
        if not answer:
            print(f"[text] empty reply in {session_id} (stop_reason={getattr(result, 'stop_reason', None)})")
            return _EMPTY_REPLY
        return answer

    async def end(self, session_id: str) -> None:
        async with self._registry_lock:
            conversation = self._live.pop(session_id, None)
        if conversation is not None:
            conversation.close()
            print(f"[text] ended session {session_id}")

    def _ensure_reaper(self) -> None:
        """Started on first use rather than at import: there has to be a
        running event loop for `create_task`, and at import time under uvicorn
        there isn't one yet."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                cutoff = time.monotonic() - TEXT_IDLE_TIMEOUT_SECONDS
                async with self._registry_lock:
                    stale = [k for k, c in self._live.items() if c.last_used < cutoff]
                    conversations = [self._live.pop(k) for k in stale]
                for key, conversation in zip(stale, conversations):
                    print(f"[text] closing idle session {key}")
                    conversation.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[text] idle sweep failed: {e}")


CONVERSATIONS = TextConversations()

router = APIRouter()


@router.post("/invocations")
async def invocations(request: Request) -> dict:
    """One text turn. AgentCore Runtime's REST invoke path.

    The session and user ids arrive as headers when this is invoked through
    `bedrock-agentcore:InvokeAgentRuntime`; the body is accepted as a fallback
    so the endpoint can also be driven directly (`uvicorn strands_agent:app`)
    while developing a channel.
    """
    try:
        body = await request.json()
    except Exception:
        return {"reply": _FAILURE_REPLY, "error": "body must be JSON"}
    if not isinstance(body, dict):
        return {"reply": _FAILURE_REPLY, "error": "body must be a JSON object"}

    session_id = request.headers.get(SESSION_ID_HEADER) or body.get("session_id")
    user_id = request.headers.get(USER_ID_HEADER) or body.get("user_id")
    if not session_id:
        return {"reply": _FAILURE_REPLY, "error": "no session id in headers or body"}

    if body.get("reset"):
        await CONVERSATIONS.end(session_id)
        return {"reply": _RESET_REPLY, "session_id": session_id}

    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": _EMPTY_REPLY, "session_id": session_id}

    reply = await CONVERSATIONS.answer(session_id, user_id, text)
    return {"reply": reply, "session_id": session_id}
