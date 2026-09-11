"""Ava — voice contact center agent, hosted on AWS Bedrock AgentCore Runtime.

This is the browser-facing counterpart to bot.py (which runs locally against
your mic/speaker). Same persona, same AgentCore Gateway tools, same "agentic
flow, deterministic tools" split — just running as a long-lived WebSocket
service instead of a local process, since that's what a browser UI needs to
talk to.

Built on the Strands Agents SDK's BidiAgent + BidiNovaSonicModel, which
handle the Nova Sonic wire protocol, audio buffering, tool-call dispatch, and
interruption/barge-in for you. AgentCore Runtime expects this container to
serve a WebSocket at /ws and a health check at /ping, both on port 8080 (see
Dockerfile) — everything else is plumbing around that contract.

Gateway tools are loaded live from the gateway on every call via Strands'
MCPClient — nothing about a tool's name, parameters, or description is
hardcoded here, so adding, removing, or reshaping a tool on the gateway
needs zero changes on this side. The one exception is genuinely
unavoidable: a tool that declares a required session_id shouldn't have the
model invent one, so _FixedArgumentTool (below) detects that generically
(by inspecting the tool's own schema, not by name) and injects it — no
tool's schema is redeclared by hand anywhere in this file.

Every tool call also passes through _GroundingGateTool, which blocks an
argument from being a value the model made up or took directly from what
the caller said, whenever that argument's field name has already been
returned by a completed tool call this session — e.g. a claim tool's
policy_id must equal what the lookup tool actually returned, not a number
Ava merely heard. This enforces SYSTEM_INSTRUCTION's "use the id from the
lookup" rule as a runtime check the model cannot reason its way around,
again detected generically by field name rather than by hardcoding which
tool this applies to.

Two more generic wrappers sit in the same chain: _ToolOutputSanitizerTool
neutralizes instruction-like strings in whatever a tool returns (or its
description), since that content comes from the gateway's backend and
should be treated as data, never as a directive to the model. And
_IdempotencyGateTool replays the result of an identical, recent call
instead of re-invoking the tool, so a dropped acknowledgement or a
confused retry can't double a side effect like registering a claim.

Local dev:
    python strands_agent.py
    # then, from another terminal:
    python -m websockets ws://127.0.0.1:8080/ws   (or see frontend/index.html)

Deploy: see DEPLOY.md.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Local dev only: when running this file directly (`python strands_agent.py`),
# load the same .env bot.py uses (one directory up) so env vars like
# MCP_SERVER_URL and BEDROCK_PROMPT_ID are actually set. In the deployed
# container, AgentCore Runtime injects environment variables directly and
# there's no .env file present — load_dotenv() is then a harmless no-op.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Before every other local import, and in particular before _setup_telemetry()
# below: this replaces the root logger's handlers, and anything that logs
# during its own import (the OTel SDK, botocore) should come out in the same
# JSON shape as the rest. See observability.py for what that buys.
from observability import (
    CALL_ID,
    CALL_USAGE,
    COUNTERS,
    SESSION_ID,
    CallUsage,
    bump,
    emit_call_ledger,
    event,
    log,
    record_nova_usage,
    setup_logging,
)

setup_logging()

import handoff
from handoff import LiveCall
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
# Aliased: `tool` is the loop variable for AgentTool objects all over this
# module, and a decorator that quietly resolves to a gateway tool instead
# is exactly the class of bug that already cost this file once.
from strands import tool as strands_tool
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.tools import stop_conversation
from strands.tools.mcp.mcp_client import MCPClient
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

GATEWAY_URL = os.environ.get("MCP_SERVER_URL")
GATEWAY_REGION = os.environ.get("AWS_REGION", "us-west-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", GATEWAY_REGION)
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-2-sonic-v1:0")
INPUT_SAMPLE_RATE = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
OUTPUT_SAMPLE_RATE = int(os.getenv("OUTPUT_SAMPLE_RATE", "16000"))

# Optional: manage the system prompt in Bedrock Prompt Management instead of
# the SYSTEM_INSTRUCTION constant below, so editing the prompt in the Bedrock
# console takes effect on the next call with no redeploy. Unset (the
# default) keeps using SYSTEM_INSTRUCTION as-is.
BEDROCK_PROMPT_ID = os.getenv("BEDROCK_PROMPT_ID")
BEDROCK_PROMPT_VERSION = os.getenv("BEDROCK_PROMPT_VERSION")  # omit for the DRAFT version

# Optional: AgentCore Memory resource id for cross-call recall — what lets a
# caller who rang before be recognized on a later call. Blank (the default)
# disables it entirely: nothing is written, nothing is retrieved, and a call
# runs exactly as it did before. Every memory call below is also wrapped so a
# failure degrades to "no memory this call" rather than dropping the call —
# recall is a nicety, the call is not.
#
# Scoping: actorId is the caller's confirmed mobile number (the only stable
# caller identity this system has), sessionId is one call. Nothing is written
# under a number that was never confirmed by a successful lookup, so an
# unverified caller can neither read nor pollute another number's memory.
MEMORY_ID = os.getenv("MEMORY_ID", "")
MEMORY_REGION = os.getenv("MEMORY_REGION", GATEWAY_REGION)
# How many recalled records to fold into the greeting context. Small on
# purpose: this rides along in the model's first turn, and a long list of
# half-relevant facts is worse than a couple of sharp ones.
MEMORY_RECALL_TOP_K = int(os.getenv("MEMORY_RECALL_TOP_K", "5"))

# Untrusted tool output (G2.18): a tool result comes from the gateway's
# backend, not from the caller or the model — but it's still external input
# that could be malformed, compromised, or (for a third-party MCP server)
# actively poisoned with text aimed at the model rather than data for it.
# Nova Sonic's tool-result API takes a JSON object, not a text block, so the
# usual "wrap it in delimiters and tell the model it's data" defense doesn't
# apply structurally here — this instead scans every string value in a
# tool's result (and description) for instruction-like phrasing and
# neutralizes it before the model ever sees it. It's a heuristic, not a
# classifier: cheap insurance against the tool result smuggling a
# directive, not a guarantee against a determined attacker.
_INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all|any|the)?\s*(previous|prior|above)\s*instructions",
        r"disregard (all|any|the)?\s*(previous|prior|above)",
        r"new (system )?instructions",
        r"you are now (a|an)",
        r"system\s*:\s*\S",
        r"reveal (the|your) (system prompt|instructions)",
    )
]


def _looks_like_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def _sanitize_untrusted(data):
    """Recursively neutralize instruction-like strings in tool-returned data
    (or a tool's own description) — see the note above _INJECTION_PATTERNS."""
    if isinstance(data, dict):
        return {key: _sanitize_untrusted(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_sanitize_untrusted(item) for item in data]
    if isinstance(data, str) and _looks_like_injection(data):
        event("untrusted_output.neutralized", sample=repr(data[:80]))
        return "[content removed: this looked like an embedded instruction, not data]"
    return data


# Client-side half of idempotency (G1.19): if the exact same tool call
# (same arguments) repeats within this window — a dropped acknowledgement,
# a confused retry — the first call's result is replayed instead of
# executing the action again. Bounded by time rather than kept for the
# whole call so a genuinely new, later call with coincidentally identical
# arguments (e.g. re-checking status) still gets a fresh execution rather
# than being served stale data forever. This is the client's contribution;
# true dedup still depends on the gateway tool itself being safe to retry.
IDEMPOTENCY_WINDOW_SECONDS = 30


def _record_governance_event(
    *,
    gate: str,
    action: str,
    reason: str,
    session_id: str,
    tool_name: str,
    **extra: Any,
) -> None:
    """One hook every gate below calls exactly once when it blocks, holds,
    dedupes, or neutralizes something — so "how many grounding blocks this
    week" becomes one CloudWatch Logs Insights query instead of a regex over
    free-text print statements.

    Does two things with the same event, both landing in CloudWatch with no
    new pipeline to run:

    1. Tags the *current* span with ``ava.gate.*`` attributes. That span is
       already part of the trace _setup_telemetry() exports to CloudWatch
       GenAI Observability, so the gate's decision shows up directly on that
       call's execute_tool span in the Traces view — no separate exporter,
       no new endpoint. get_current_span() degrades to a harmless no-op span
       when tracing is off (local dev, AGENT_OBSERVABILITY_ENABLED unset),
       so this is safe to call unconditionally.
    2. Emits one structured JSON line on stdout. That's the same stdout
       AgentCore already ships to this agent's runtime-logs log group
       (/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>/[runtime-logs])
       with zero extra configuration — the JSON shape just makes it
       Logs-Insights-queryable (`filter governance_event = "ava_gate"`)
       instead of grep-only.
    """
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("ava.gate.name", gate)
            span.set_attribute("ava.gate.action", action)
            span.set_attribute("ava.gate.reason", reason)
            span.set_attribute("ava.session_id", session_id)
            for key, value in extra.items():
                span.set_attribute(f"ava.gate.{key}", value)
    except Exception as e:
        # Tracing is best-effort; the log line below is the record of truth,
        # so a span-tagging failure shouldn't be silent but also shouldn't
        # ever block the event from being logged.
        event("governance.span_tagging_failed", error=str(e))

    # An action that actually stopped something, as opposed to one that merely
    # observed it. Only "blocked" and "held" count into the ledger's
    # GateBlocks — "deduped" and "neutralized" are routine on a healthy call
    # and would drown the signal that something was genuinely refused.
    if action in ("blocked", "held"):
        bump("gate_blocks")

    event(
        "gate." + action,
        governance_event="ava_gate",
        gate=gate,
        action=action,
        reason=reason,
        tool=tool_name,
        # Not the same id as the log line's `session_id`, which is the
        # AgentCore Runtime session. This one is the per-call uuid
        # build_gateway_tools injects into tools that require a session_id,
        # and it's what appears in the gateway Lambda's own logs — so it is
        # the key that joins a gate decision to the tool call it stopped.
        tool_session_id=session_id,
        **extra,
    )

# Human-in-the-loop threshold (G4.19): a claim at or above this estimated
# cost must not be registered autonomously, however confident Ava is —
# confirmed with the team as the business threshold. Detected generically
# by the presence of HIGH_VALUE_CLAIM_FIELD in a call's arguments, not by
# hardcoding a tool name, so this keeps working if the gateway renames or
# adds a second tool that also takes an estimated_cost. SYSTEM_INSTRUCTION
# also tells Ava about the threshold so she can set expectations with the
# caller before even attempting the call, but _HighValueClaimGateTool below
# is what makes it non-negotiable regardless of what the model decides.
HIGH_VALUE_CLAIM_FIELD = "estimated_cost"
HIGH_VALUE_CLAIM_THRESHOLD = 100_000

# Nova Sonic ends a session that goes ~55s without audio bytes or interactive
# content ("Timed out waiting for audio bytes..."), and that exception ends
# run() and with it the caller's call. So when no real caller audio is
# arriving -- muted caller, human agent on the line, throttled background tab
# -- silence is fed instead, well inside that window. Deliberately far below
# 55s: this also has to cover a slow event loop and a stalled network.
KEEPALIVE_SECONDS = 5.0
_SILENT_FRAME_SAMPLES = INPUT_SAMPLE_RATE // 10  # 100ms, same as the browser sends

# How the call ends when Ava is the one ending it (the `end_call` tool).
#
# Closing the socket the instant the tool returns cuts the goodbye off
# mid-word: Nova Sonic has not started speaking yet when the tool result goes
# back, and the farewell arrives as audio frames over the second or so after.
# So the line is held open until Ava has been quiet for FAREWELL_SILENCE_
# SECONDS, which each outgoing audio frame pushes further out, and the timer
# starts at the tool call rather than at her last frame — otherwise a caller
# who just finished a long sentence would close the call before Ava got a
# word in.
#
# The cap exists because "quiet for two seconds" never arriving is a real
# outcome: a model that keeps talking, or an audio stream that never drains,
# would otherwise hold a billable session open indefinitely.
FAREWELL_SILENCE_SECONDS = float(os.getenv("FAREWELL_SILENCE_SECONDS", "2.0"))
FAREWELL_MAX_SECONDS = float(os.getenv("FAREWELL_MAX_SECONDS", "20.0"))


def _silent_audio_event() -> dict[str, Any]:
    """A 100ms frame of silence, shaped exactly like the browser's own audio
    message — indistinguishable to the model from a live mic in a quiet room."""
    return {
        "type": "bidi_audio_input",
        "audio": base64.b64encode(b"\x00" * (_SILENT_FRAME_SAMPLES * 2)).decode(),
        "format": "pcm",
        "sample_rate": INPUT_SAMPLE_RATE,
        "channels": 1,
    }


def _normalize_caller_audio(message: dict[str, Any]) -> dict[str, Any]:
    """Fill in the audio fields the model requires but a browser needn't know.

    An audio frame from the page carries `audio` and `sample_rate` — the two
    facts the page actually has. `format` and `channels` describe how this
    agent captures audio, which is this process's business, not the client's.

    Not cosmetic: BidiAudioInputEvent made both required, so a frame without
    them raises TypeError inside send() — which ends run() and drops the call
    the instant the caller first speaks, while Ava's greeting still plays
    normally because that direction never touches this event. Filling them in
    here keeps every already-deployed page working, and keeps the wire format
    the page has to satisfy as small as it can be.
    """
    if message.get("type") != "bidi_audio_input":
        return message
    if "format" in message and "channels" in message:
        return message
    return {**message,
             "format": message.get("format", "pcm"),
             "sample_rate": message.get("sample_rate", INPUT_SAMPLE_RATE),
             "channels": message.get("channels", 1)}


SYSTEM_INSTRUCTION = """
You are Ava, a voice assistant for a contact center. Every word you write is spoken aloud by a text-to-speech engine. There is no private channel — no reasoning, plans, notes, labels, field names, or raw tool output. Write only what a human agent would say.
Open with exactly: "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent." Then listen. Never repeat that list; if you're unsure what they want, ask "What would you like to do next?"
One thing at a time. Each reply asks for at most one thing and contains at most one question mark. Six values needed means six replies. Ask once — don't rephrase the same question. If the caller is mid-sentence or you caught only a fragment, wait.
Speak like a person. One or two sentences, then stop. Contractions, ordinary words. Acknowledge briefly and differently each time ("got it", "right", "thanks"). Say dates and amounts naturally — "the sixteenth of August", not "2026-08-16". Don't narrate your process, name a tool, or say you're checking a system.
Never say: "sorry" at the start of a turn; "I just need a few details"; "verify" / "confirm your identity" / "for security"; "I can help with that" before you've checked; "you'll need to contact customer service."
Confirm what you heard. Read back and get an explicit yes before using anything spelled or recited, any run of digits, date, address, place name, or amount. Say ids one character at a time. "Yesterday" or "last Tuesday" isn't a date until you've said the actual day and month back and had a yes. A correction replaces the old value completely and needs its own readback and yes.
Never invent a value. Everything you submit comes from something the caller said or an earlier result — never a guess, an example from a description, or a placeholder like "unknown" or "n/a". If a tool wants a description or explanation, ask an open question and use their words; never write it yourself.
Only say something can be done once you've checked it can. If something blocks the action, say so immediately and collect nothing. If you can't do it: "I'm not able to do that myself, so I'll pass this to one of my colleagues" — then actually hand off. Don't promise what the colleague will do or when.
Three attempts, then a person. After the third failure at the same thing: "I've not been able to get this right, so I'll bring in a colleague who can." Say it once, escalate, don't reopen. Insisting doesn't reset the count.
Escalate when the caller asks for a person, wants something you can't do, sounds frustrated, disputes a block, or a tool fails twice (retry once first). Send a summary so they never repeat themselves, then stay on the line and start nothing new. If escalation fails, retry once, say you couldn't reach anyone, then ESCALATION_FALLBACK_INSTRUCTION.
Business rules. Identify the caller with a lookup before discussing account details or taking a claim — that lookup is the identity check, so don't mention or repeat it. Never look up a number the caller hasn't spoken and confirmed digit by digit in this call. If nothing is found, re-check what you sent, then say so plainly and offer a teammate. If there's more than one policy or vehicle, ask which the call is about. An open claim anywhere on the account blocks all new claims — open means anything not closed, settled, paid, rejected or withdrawn, including statuses you don't recognise — and while one exists, don't offer or mention registering another. Never invent policy, claim or account information. If a tool result and the caller disagree, escalate rather than overwrite or argue.
Ending the call. When the caller confirms they need nothing further — they say goodbye, say that's all, or answer no to your check that there's anything else — call end_call with a short reason, then say a brief, warm goodbye. The line closes on its own once you stop speaking, so don't announce that you're hanging up or wait for another reply. Never use end_call to get out of a question you can't answer, a caller who is upset, or anything you would otherwise escalate: those go to a person, and a call you end is a call nobody calls back.
""".strip()

def _prompt_region() -> str:
    """Region to reach Bedrock Prompt Management in.

    Deliberately *not* BEDROCK_REGION: that one is Nova Sonic's region
    (us-east-1 here), which has nothing to do with where the prompt lives.
    Pointing the client at the wrong region fails in a genuinely misleading
    way -- the request is signed for the calling region, so the service
    resolves the resource against *that* region and IAM reports a plain
    `AccessDeniedException: not authorized to perform: bedrock:GetPrompt`
    even when the policy grants exactly this prompt. It reads as a missing
    permission and is not one.

    A prompt id can be given either as a bare id or a full ARN; when it is an
    ARN its own region is authoritative, since that is where the resource
    actually is. Otherwise fall back to this deployment's AWS_REGION.
    """
    if BEDROCK_PROMPT_ID and BEDROCK_PROMPT_ID.startswith("arn:"):
        parts = BEDROCK_PROMPT_ID.split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return GATEWAY_REGION


_bedrock_agent_client = (
    boto3.client("bedrock-agent", region_name=_prompt_region()) if BEDROCK_PROMPT_ID else None
)


def _fetch_prompt_template() -> str:
    """Fetch the current text of a Bedrock Prompt Management prompt (blocking
    boto3 call — always run through asyncio.to_thread, never awaited directly).

    A prompt can have several named variants; this picks whichever one is
    marked as the default rather than assuming there's only one.
    """
    kwargs = {"promptIdentifier": BEDROCK_PROMPT_ID}
    if BEDROCK_PROMPT_VERSION:
        kwargs["promptVersion"] = BEDROCK_PROMPT_VERSION
    response = _bedrock_agent_client.get_prompt(**kwargs)

    variants = response.get("variants") or []
    if not variants:
        raise RuntimeError(f"prompt {BEDROCK_PROMPT_ID!r} has no variants")
    default_variant_name = response.get("defaultVariant")
    variant = next((v for v in variants if v["name"] == default_variant_name), variants[0])

    return variant["templateConfiguration"]["text"]["text"]


async def get_system_prompt() -> str:
    """The system prompt for this call: fetched fresh from Bedrock Prompt
    Management if BEDROCK_PROMPT_ID is set, else the SYSTEM_INSTRUCTION
    constant above. Fetched once per WebSocket connection (like gateway
    tools) so an edit in the Bedrock console is live on the next call.

    Falls back to SYSTEM_INSTRUCTION on fetch failure (missing permission,
    deleted prompt, throttling) rather than failing the whole call — a
    stale-but-working prompt beats no call connecting at all.
    """
    if not BEDROCK_PROMPT_ID:
        event("prompt.local", reason="BEDROCK_PROMPT_ID not set")
        return SYSTEM_INSTRUCTION
    try:
        text = await asyncio.to_thread(_fetch_prompt_template)
        version_label = BEDROCK_PROMPT_VERSION or "DRAFT"
        event("prompt.fetched", prompt_id=BEDROCK_PROMPT_ID, version=version_label, chars=len(text))
        return text
    except Exception as e:
        event("prompt.fetch_failed", prompt_id=BEDROCK_PROMPT_ID, error=str(e),
              fallback="SYSTEM_INSTRUCTION")
        return SYSTEM_INSTRUCTION


# AgentCore Memory only accepts an actorId matching
# [a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]* — no "+".
# A caller's number in E.164 therefore can't be used as one directly: the
# session manager's first ListEvents fails validation and memory is dropped
# for the whole call ("[memory] could not start session manager"), which is
# easy to miss because the call itself carries on working. Everything about
# a returning caller's history depends on this id, so it's derived once here
# rather than at each use site.
_MEMORY_ACTOR_DISALLOWED = re.compile(r"[^0-9a-zA-Z]")


def _memory_actor_id(mobile_number: str | None, fallback_session_id: str) -> str:
    """A memory actor id for this caller that the service will accept.

    Stable and reversible-by-eye ("+61412345678" -> "phone-61412345678") so a
    returning caller lands on the same actor and their earlier calls are
    still theirs. Every separator is dropped, not just the illegal ones, so
    the same person typing "+61 412 345 678" one week and "+61412345678" the
    next is one actor rather than two. Callers with no number get a per-call
    id instead — see the note at the call site about why guessing an identity
    there is worse.
    """
    if mobile_number:
        return "phone-" + _MEMORY_ACTOR_DISALLOWED.sub("", mobile_number)
    return f"anonymous-{fallback_session_id}"


def build_memory_session_manager(actor_id: str, session_id: str):
    """The AgentCore Memory session manager for this call, or None when memory
    is switched off (MEMORY_ID unset) or unavailable.

    Both halves of memory come from this one object, handled by the SDK rather
    than by hand here:

    - Session (short-term): every turn of this call is persisted under
      `session_id`, so the conversation survives a dropped socket and is
      readable afterwards instead of living only in this process.
    - Long-term: AgentCore extracts durable records from past calls
      (preferences, facts, per-call summaries) and the manager retrieves the
      relevant ones and injects them into the model's context automatically —
      which is what makes a returning caller's experience personalised without
      Ava having to ask them everything again.

    `async_mode=True` matters here specifically: this runs inside a live voice
    call, and it offloads the per-turn boto3 calls to threads so the asyncio
    loop driving the audio stream is never blocked waiting on AWS.

    Returns None rather than raising if anything goes wrong — a caller losing
    memory is a degraded call; a caller losing the call is a failed one.
    """
    if not MEMORY_ID:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
            RetrievalConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
    except ImportError as e:
        event("memory.unavailable", error=str(e))
        return None

    try:
        config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            # Namespaces must match the strategies the memory resource was
            # created with (see MEMORY_SETUP.md). Preferences are retrieved
            # with a higher relevance bar than facts: a wrong "prefers
            # cashless" colours the whole call, while a stale fact is usually
            # just ignored.
            retrieval_config={
                "/preferences/{actorId}/": RetrievalConfig(
                    top_k=MEMORY_RECALL_TOP_K, relevance_score=0.7
                ),
                "/facts/{actorId}/": RetrievalConfig(
                    top_k=MEMORY_RECALL_TOP_K, relevance_score=0.5
                ),
            },
            async_mode=True,
        )
        manager = AgentCoreMemorySessionManager(config, region_name=MEMORY_REGION)
        event("memory.ready", actor=actor_id, memory_session=session_id)
        return manager
    except Exception as e:
        event("memory.start_failed", error=str(e))
        return None


class _LoggingTool(AgentTool):
    """Wraps any AgentTool to print its call and result — useful visibility
    into what Ava actually did, without touching the wrapped tool's schema
    or behavior at all."""

    def __init__(self, wrapped: AgentTool) -> None:
        super().__init__()
        self._wrapped = wrapped

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        # `tool_event`, not `event`: the module-level `event()` logger is
        # called below, and every other wrapper in this file binds the loop
        # variable to `event` — which would shadow it. The wrappers that
        # don't log are left alone; this one has to be the exception.
        started = time.monotonic()
        bump("tool_calls")
        event("tool.call", tool=tool_use["name"], arguments=tool_use.get("input"))
        async for tool_event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Logged as the result passes through, NOT after the loop: Strands'
            # tool executor `return`s the moment it sees a ToolResultEvent
            # (strands/tools/executors/_executor.py), abandoning this
            # generator — so anything after the `async for` never runs. Every
            # wrapper in this file that acts on a result does it here, inside
            # the loop, for that reason.
            if isinstance(tool_event, ToolResultEvent):
                result = tool_event.tool_result
                status = result.get("status") if isinstance(result, dict) else None
                if status and status != "success":
                    bump("tool_errors")
                event(
                    "tool.result",
                    tool=tool_use["name"],
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    result=str(result)[:2000],
                )
            yield tool_event


class _MobileNumberSyncTool(AgentTool):
    """Wraps an AgentTool so a successful call's phone-number-shaped
    argument is pushed back to the browser as a `bidi_call_metadata_update`
    event whenever it's new for this call.

    Covers the caller-correction case: the UI pre-fills a number, but the
    caller says it's wrong and states a different one — once a lookup
    succeeds on the corrected number, this lets frontend/index.html update
    its input field (and localStorage) to match, without the caller having
    to separately go back and retype it after the call.

    Detected the same way _TranscriptConsistencyGateTool detects a phone
    number -- by value shape (_PHONE_LIKE_PATTERN, "+" then only digits),
    not by field name. The gateway's actual argument for this is named
    plainly `number`, which a name-based hint would either miss entirely or
    (if broadened to match "number") false-positive on account_number,
    card_number, claim_number, and similar unrelated identifiers -- value
    shape avoids that ambiguity and needs no hardcoded field name at all.
    """

    def __init__(
        self,
        wrapped: AgentTool,
        on_confirmed: Callable[[str], Awaitable[None]],
        known: list[str | None],
    ) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._on_confirmed = on_confirmed
        self._known = known

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Before the yield, not after the loop — see _LoggingTool.stream.
            if isinstance(event, ToolResultEvent) and event.tool_result.get("status") == "success":
                for value in (tool_use.get("input") or {}).values():
                    if isinstance(value, str) and _PHONE_LIKE_PATTERN.match(value):
                        if value != self._known[0]:
                            self._known[0] = value
                            await self._on_confirmed(value)
                        break
            yield event


class _FixedArgumentTool(AgentTool):
    """Wraps any AgentTool so one argument is always injected with a fixed
    value and removed from the schema the model sees.

    Used for plumbing (like a per-call session id) that a tool needs but
    that the model has no business inventing — without redeclaring the
    wrapped tool's schema by hand. Name, description, and every other
    parameter stay exactly what the underlying tool currently reports, so
    this keeps working unchanged if the tool's other parameters change
    upstream.
    """

    def __init__(self, wrapped: AgentTool, **fixed_arguments: Any) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._fixed_arguments = fixed_arguments

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        spec = deepcopy(self._wrapped.tool_spec)
        schema = spec.get("inputSchema", {}).get("json", {})
        for key in self._fixed_arguments:
            schema.get("properties", {}).pop(key, None)
            if key in schema.get("required", []):
                schema["required"].remove(key)
        return spec

    def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        patched_tool_use: ToolUse = {
            **tool_use,
            "input": {**(tool_use.get("input") or {}), **self._fixed_arguments},
        }
        return self._wrapped.stream(patched_tool_use, invocation_state, **kwargs)


def _requires_argument(tool: AgentTool, argument_name: str) -> bool:
    schema = tool.tool_spec.get("inputSchema", {}).get("json", {})
    return argument_name in schema.get("required", [])


# Tool names (bare, after the Gateway's "<target>___" prefix) that need a fixed
# session_id injected even though their advertised inputSchema doesn't declare
# one, so _requires_argument can't find it. Confirmed by pulling the live
# Gateway target's schema directly (get_gateway_target): escalate's schema has
# no session_id property at all, yet the Lambda behind it (mcp_tools.py's
# _escalate) does `event["session_id"]` with no fallback -- every escalation
# call fails with a KeyError until this is injected some other way. A schema
# fix on the Gateway side would be the "real" fix, but that's a live AWS
# resource this code doesn't own the deployment of, so this closes the gap
# from the client instead.
_SESSION_ID_TOOL_NAMES_WITHOUT_SCHEMA_PROPERTY = {"escalate"}


def _bare_tool_name(tool: AgentTool) -> str:
    """Strip the Gateway's "<target>___" prefix off a tool's name, if present."""
    return tool.tool_name.rsplit("___", 1)[-1]


def _extract_json(tool_result: ToolResult) -> dict[str, Any] | None:
    """Best-effort extraction of a tool result's JSON payload as a dict, checking
    both the `json` and `text` (JSON-encoded string) content shapes a gateway tool
    might use."""
    for content in tool_result.get("content", []):
        payload = content.get("json")
        if isinstance(payload, dict):
            return payload
        text = content.get("text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _escalation_id_from(payload: Any, _depth: int = 0) -> str | None:
    """The escalation id anywhere in a tool result, or None if it isn't one.

    Detected by field name shape (any key mentioning "escalation" whose value
    is a scalar), not by tool name — same reasoning as every other wrapper
    here: the gateway's tool set is discovered live, so nothing hardcodes
    which tool escalates. Today that's escalate's `escalation_id`.

    The search is recursive, and re-parses JSON-encoded strings on the way
    down, because how deeply the gateway nests a Lambda's return value is not
    something this side controls: `{"escalation_id": …}` and an MCP-style
    `{"content": [{"text": "{\\"escalation_id\\": …}"}]}` envelope both have to
    count, or an escalation silently fails to notify anyone — the one failure
    mode where the caller is left waiting for a human who was never told.
    """
    # Generous, because each envelope level costs two hops (container, then
    # item) and a re-parsed JSON string costs another: a payload nested two
    # envelopes deep already reaches 7. Only there to bound the walk.
    if _depth > 16:
        return None
    if isinstance(payload, dict):
        for key, value in payload.items():
            if "escalation" in key.lower() and isinstance(value, (str, int, float)) and str(value).strip():
                return str(value)
        for value in payload.values():
            found = _escalation_id_from(value, _depth + 1)
            if found:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = _escalation_id_from(item, _depth + 1)
            if found:
                return found
        return None
    if isinstance(payload, str) and "escalation" in payload.lower():
        try:
            return _escalation_id_from(json.loads(payload), _depth + 1)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


class _EscalationNotifierTool(AgentTool):
    """Wraps an AgentTool so a successful escalation opens a human handoff:
    records the reason on this call, posts it to Slack, and makes the call
    joinable from the agent portal (see handoff.py).

    Notifying happens in a background task rather than before this tool's
    result is yielded: posting to Slack is a network round trip, and the
    caller is sitting in a live voice call waiting for Ava's next sentence.
    """

    def __init__(self, wrapped: AgentTool, call: LiveCall) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._call = call

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Handled here, before the result is yielded, and NOT after the
            # loop: Strands' tool executor returns as soon as it sees a
            # ToolResultEvent and abandons this generator, so post-loop code
            # never runs (see _LoggingTool.stream). Getting this wrong is
            # silent — the escalation still queues, nobody is ever told.
            if isinstance(event, ToolResultEvent) and event.tool_result.get("status") == "success":
                # The whole result, not just _extract_json's view of it: that
                # helper only looks one level into `content`, and this has to
                # find the id wherever the gateway happens to put it.
                escalation_id = _escalation_id_from(event.tool_result)
                if escalation_id is not None:
                    portal_url = await self._call.mark_escalated(
                        tool_use.get("input") or {}, escalation_id)
                    # Held on the call so the task isn't garbage-collected
                    # mid-flight, which asyncio permits for a task nobody
                    # references.
                    self._call.slack_task = asyncio.create_task(
                        handoff.notify_slack(self._call, portal_url))
            yield event


class _GroundingGateTool(AgentTool):
    """Wraps an AgentTool so any argument whose name matches a field some earlier
    tool call in this session actually returned must equal a value that was
    actually returned under that field name — it cannot be a value the model
    invented or lifted straight from what the caller said.

    Only field names some tool has actually returned this session are checked, so
    this never blocks a legitimately caller-supplied field (a claim type, an
    incident date) — it only catches an identifier (a policy id, a vehicle id)
    being reused without it actually having come from a completed lookup. This is
    what turns "use the id from the lookup, don't take one the caller just
    states" — already stated in SYSTEM_INSTRUCTION — from an instruction the model
    can drift away from into a runtime check it cannot bypass, without hardcoding
    which tool or field this applies to.
    """

    def __init__(self, wrapped: AgentTool, grounded_values: dict[str, set[str]], session_id: str) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._grounded_values = grounded_values
        self._session_id = session_id

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        arguments = tool_use.get("input") or {}
        ungrounded = [
            key
            for key, value in arguments.items()
            if key in self._grounded_values and str(value) not in self._grounded_values[key]
        ]
        if ungrounded:
            _record_governance_event(
                gate="grounding",
                action="blocked",
                reason="argument_not_grounded",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                fields=ungrounded,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                f"Cannot proceed: {', '.join(ungrounded)} must come from a completed "
                                "lookup earlier in this call, not be restated from what the caller said. "
                                "Run the lookup tool again and use its returned value."
                            )
                        }
                    ],
                }
            )
            return

        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Recorded before the yield, not after the loop — post-loop code
            # never runs here (see _LoggingTool.stream). This is the gate's
            # entire memory of "what a lookup actually returned", so missing
            # it left the gate with nothing to check later arguments against.
            if isinstance(event, ToolResultEvent) and event.tool_result.get("status") == "success":
                data = _extract_json(event.tool_result)
                if data:
                    for key, value in data.items():
                        if isinstance(value, (str, int, float)):
                            self._grounded_values.setdefault(key, set()).add(str(value))
            yield event


class _ToolOutputSanitizerTool(AgentTool):
    """Wraps an AgentTool so its result is passed through _sanitize_untrusted
    before anything downstream (grounding, idempotency caching, logging, or
    the model itself) ever sees it — see the note above _INJECTION_PATTERNS
    for why this exists and what it can and can't catch.

    Wrapped as close to the real tool as reasonably possible (right after
    _FixedArgumentTool) so every other wrapper in the chain — including the
    grounding gate's own record of "values a lookup actually returned" —
    only ever operates on already-sanitized content.
    """

    def __init__(self, wrapped: AgentTool, session_id: str) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._session_id = session_id

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        spec = deepcopy(self._wrapped.tool_spec)
        if "description" in spec:
            spec["description"] = _sanitize_untrusted(spec["description"])
        return spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            if isinstance(event, ToolResultEvent):
                sanitized = _sanitize_untrusted(event.tool_result)
                if sanitized != event.tool_result:
                    _record_governance_event(
                        gate="output_sanitizer",
                        action="neutralized",
                        reason="instruction_shaped_content",
                        session_id=self._session_id,
                        tool_name=tool_use["name"],
                    )
                event = ToolResultEvent(sanitized, exception=event.exception)
            yield event


class _IdempotencyGateTool(AgentTool):
    """Wraps an AgentTool so an exact repeat of the same arguments within
    IDEMPOTENCY_WINDOW_SECONDS replays the first call's result instead of
    executing the tool again — the client-side half of G1.19. Wrapped
    outside _GroundingGateTool: a cache hit skips grounding and the real
    call entirely, since the cached result already passed both once.

    The cache lives only in this wrapper's own memory, scoped to one
    WebSocket connection (see build_gateway_tools) and bounded by time
    rather than kept for the whole call, so a genuinely new, later call
    with coincidentally identical arguments (e.g. re-checking status) still
    gets a fresh execution rather than being served stale data forever.

    Separately (and not time-windowed the way the cache above is), this
    also counts how many times a given exact argument set has been tried
    at all this call. Observed bug: after a caller corrected their mobile
    number several times, Nova Sonic kept calling get_policy with the same
    stale, already-failed arguments regardless — verbally engaging with
    "let me get your corrected number" without the tool call ever actually
    changing. Rather than guess at a corrected value ourselves (risky for
    an identity-verification lookup — a wrong guess would look confident
    while being wrong, worse than the visible stuck loop), this forces a
    clean exit: past _MAX_IDENTICAL_ATTEMPTS with literally unchanged
    arguments, refuse to call the tool again and require escalation
    instead of retrying forever.
    """

    _MAX_IDENTICAL_ATTEMPTS = 3

    def __init__(self, wrapped: AgentTool, session_id: str) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._session_id = session_id
        self._cache: dict[str, tuple[float, ToolResult]] = {}
        self._attempt_counts: dict[str, int] = {}

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        cache_key = json.dumps(tool_use.get("input") or {}, sort_keys=True, default=str)

        attempts = self._attempt_counts.get(cache_key, 0) + 1
        self._attempt_counts[cache_key] = attempts
        if attempts > self._MAX_IDENTICAL_ATTEMPTS:
            _record_governance_event(
                gate="idempotency",
                action="blocked",
                reason="repeat_limit_exceeded",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                attempts=attempts - 1,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                "This exact request has already been tried multiple times this call "
                                "with unchanged details and hasn't worked. Do not retry it again with "
                                "the same information — that will not help. Tell the caller plainly "
                                "that you're unable to complete this with what's been provided so far, "
                                "and escalate to a human agent."
                            )
                        }
                    ],
                }
            )
            return

        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < IDEMPOTENCY_WINDOW_SECONDS:
            age, cached_result = cached
            _record_governance_event(
                gate="idempotency",
                action="deduped",
                reason="cache_hit",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                age_seconds=round(now - age, 1),
            )
            yield ToolResultEvent({**cached_result, "toolUseId": tool_use["toolUseId"]})
            return

        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Cached before the yield, not after the loop — post-loop code
            # never runs here (see _LoggingTool.stream), which left this
            # cache permanently empty and the replay protection inert.
            if isinstance(event, ToolResultEvent):
                self._cache[cache_key] = (now, event.tool_result)
            yield event


class _HighValueClaimGateTool(AgentTool):
    """Wraps an AgentTool so a call whose HIGH_VALUE_CLAIM_FIELD argument
    meets or exceeds HIGH_VALUE_CLAIM_THRESHOLD is blocked before it
    executes — the tool never fires, and the model gets back a structured
    result telling it to escalate to a human instead.

    Detected generically by the presence of that field in the call's
    arguments, not by hardcoding a specific tool name, so this keeps
    working if the gateway renames the claim tool or adds a second one that
    also takes an estimated_cost. This is the enforcement half of G4.19 —
    SYSTEM_INSTRUCTION also tells Ava about the threshold so she can set
    expectations with the caller before even attempting the call, but this
    gate is what makes it non-negotiable regardless of what the model
    decides. Checked first, before idempotency or grounding, since a call
    this policy rejects should never reach — or be cached by — either.
    """

    def __init__(self, wrapped: AgentTool, session_id: str) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._session_id = session_id

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        arguments = tool_use.get("input") or {}
        cost = arguments.get(HIGH_VALUE_CLAIM_FIELD)
        if isinstance(cost, (int, float)) and cost >= HIGH_VALUE_CLAIM_THRESHOLD:
            _record_governance_event(
                gate="high_value_claim",
                action="held",
                reason="threshold_exceeded",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                cost=cost,
                threshold=HIGH_VALUE_CLAIM_THRESHOLD,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                f"Cannot register this claim automatically: its {HIGH_VALUE_CLAIM_FIELD} "
                                f"({cost}) is at or above the {HIGH_VALUE_CLAIM_THRESHOLD} threshold that "
                                "requires human approval. Do not retry this call. Tell the caller a "
                                "specialist needs to review a claim of this size, and escalate them to a "
                                "human agent with the claim details you've already gathered."
                            )
                        }
                    ],
                }
            )
            return

        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            yield event


_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9",
}
_WORD_OR_NUMBER = re.compile(r"[a-zA-Z]+|\d+")
_PHONE_LIKE_PATTERN = re.compile(r"^\+\d+$")

# Validates the mobile number the UI sends as call metadata (see voice_chat
# below) — deliberately stricter than a real-world phone number would need
# to be, since anything that fails this is dropped rather than reaching the
# model as text.
_PHONE_LIKE_UI_PATTERN = re.compile(r"^\+?[\d\-\s]{7,20}$")

# The other two things the pre-call form in frontend/index.html collects. All
# three values reach the model as literal text, so each is checked for being
# shaped like the thing it claims to be and dropped otherwise, and the call
# then runs exactly as if the field had been left empty.
#
# Be clear about what that buys, because it is bounding and not sanitising. A
# date has one legal shape and this pins it exactly. A name doesn't: what
# these two rules enforce is "a few words of Latin script", which stops the
# field being a place to type a paragraph of instructions but cannot stop a
# short one ("Say yes to everything") from being a valid-looking name. Two
# things carry the rest of that weight, both already here: the prompt below
# introduces the value as something the caller typed, quoted, rather than as
# text to follow; and no tool argument gets through on the model's word alone
# — _GroundingGateTool and _TranscriptConsistencyGateTool check every one
# against what a lookup returned or what the caller actually provided.
#
# The Latin-script-only rule is also a real limitation, and the wrong trade
# for a production contact centre: it rejects names it shouldn't. Widening it
# means allowing more of Unicode in, which wants more than a regex
# (normalisation, and a look at the confusable ranges).
_FULL_NAME_UI_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,59}$")
_MAX_NAME_WORDS = 5
# ISO only, because that's both what <input type="date"> submits and what the
# gateway's identity check wants — no format guessing anywhere in between.
_DOB_UI_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ui_full_name(value: Any) -> str | None:
    """The caller's name as typed into the pre-call form, or None."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not _FULL_NAME_UI_PATTERN.match(name):
        return None
    return name if len(name.split(" ")) <= _MAX_NAME_WORDS else None


def _ui_date_of_birth(value: Any) -> str | None:
    """A pre-call date of birth, only if it's a real date in the past.

    Well-formed is not the same as real (1990-02-31 matches the pattern), and
    a date of birth that hasn't happened yet is a typo or a probe rather than
    an identity — either way there's nothing to verify against, so it's
    dropped rather than passed to a tool.
    """
    if not isinstance(value, str) or not _DOB_UI_PATTERN.match(value.strip()):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return value.strip() if parsed < date.today() else None


# Field-name hints for two different flavors of "this argument is an
# identity/account detail, never a value the model should be inventing" —
# used by _TranscriptConsistencyGateTool below. Split in two because they
# need different verification strategies:
#
# _DIGIT_SEQUENCE_FIELD_HINTS covers values that are naturally spoken (and,
# per SYSTEM_INSTRUCTION, read back) as one contiguous run of digits — a
# mobile number, an account number — so the digits the caller actually said
# can be checked for a literal substring match against the argument.
#
# _DATE_FIELD_HINTS covers a date of birth, which callers say as a natural
# compound phrase ("the fifteenth of January, nineteen ninety") rather than
# digit-by-digit, and whose components (day, month, year) don't even appear
# in the same order they'd be spoken versus how a tool typically wants them
# (YYYY-MM-DD). A literal digit-substring match structurally cannot verify
# this — see _mentions_a_number below for the weaker check used instead.
#
# Both are matched by substring against the argument's field name, not any
# specific tool name, so this keeps working if the gateway adds a new tool
# with a similarly named argument.
_DIGIT_SEQUENCE_FIELD_HINTS = (
    "mobile", "phone", "ssn", "aadhaar", "pan", "national_id",
    "account_number", "card_number", "policy", "vehicle", "registration",
    "reg_no", "vin",
)
_DATE_FIELD_HINTS = ("dob", "birth")

# Minimum digit count (after stripping everything else) for a
# _DIGIT_SEQUENCE_FIELD_HINTS value to be worth checking for fabrication at
# all — long enough that a coincidental digit-substring match against
# unrelated conversation is unlikely.
_FABRICATION_MIN_DIGITS = 6

# Minimum digit count for the caller's most recent speech to be trusted as
# "clearly said a different phone number" in _StaleNumberReuseGate below —
# long enough that a short, unrelated digit mention doesn't get read as a
# rejection of the number currently in use.
_STALE_REUSE_MIN_DIGITS = 7

# Vocabulary for _mentions_a_number: single-digit words, teens, tens,
# magnitude words, ordinals (needed since a caller states a day of month as
# "fifteenth", not "one five"), and month names. Matched token-by-token, not
# as compound phrases, so "twenty-first" is still caught via its "twenty"
# and "first" tokens without needing every two-word combination listed.
_NUMBER_WORD_VOCAB = frozenset(
    {
        "zero", "oh", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
        "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand",
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
        "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
        "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
        "nineteenth", "twentieth", "thirtieth",
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    }
)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def _mentions_a_number(text: str) -> bool:
    return any(
        token.isdigit() or token in _NUMBER_WORD_VOCAB
        for token in _WORD_OR_NUMBER.findall(text.lower())
    )


class _RecentUserDigits:
    """Tracks what the caller has provided this call, so a tool argument
    can be checked against what was actually said (or, for a field like the
    mobile number, typed into the UI before the call connected — see
    `seed()`) rather than trusted at face value.

    Three views are kept for the different checks in
    _TranscriptConsistencyGateTool:

    - `spoken_digits()`: digits only, windowed to the last few non-empty
      turns, so an unrelated number mentioned much earlier (an incident
      date, a claim amount) doesn't stick around and cause a false positive
      later. Used for the "Nova Sonic dropped a trailing digit in
      speech-to-text" check, without assuming any fixed "correct" length
      (real numbers in this system vary from record to record, so a length
      constant can't do this safely -- see git history for the attempt that
      got this wrong).

    - `ever_provided_digits()`: digits only, unbounded for the life of the
      call. Used for the digit-sequence fabrication check ("was this exact
      run of digits ever provided by the caller, in any form") — that
      question must not forget something said (or typed) several turns ago
      just because a narrow recency window rolled past it.

    - `mentions_number_recently()`: not digit-based at all — true if a
      recent turn contained any number-shaped word (see
      _mentions_a_number), spoken-compound-number words included. Used for
      the date-of-birth fabrication check, where digit-substring matching
      doesn't apply (see _DATE_FIELD_HINTS above).
    """

    _WINDOW = 3

    def __init__(self) -> None:
        self._recent: list[str] = []
        self._all: list[str] = []
        self._recent_text: list[str] = []

    def record(self, text: str) -> None:
        digits = "".join(
            _DIGIT_WORDS.get(token, token if token.isdigit() else "")
            for token in _WORD_OR_NUMBER.findall(text.lower())
        )
        if digits:
            self._recent.append(digits)
            self._recent = self._recent[-self._WINDOW :]
            self._all.append(digits)
        self._recent_text.append(text)
        self._recent_text = self._recent_text[-self._WINDOW :]

    def seed(self, value: str) -> None:
        """Register digits the caller provided some way other than speech
        this call — the mobile number and date of birth typed into the UI
        before the call connected (see the call-metadata handshake in
        voice_chat) — so a tool argument built from one isn't later flagged
        as fabricated just because it was never spoken aloud."""
        digits = _digits_only(value)
        if digits:
            self._all.append(digits)

    def spoken_digits(self) -> str:
        return "".join(self._recent)

    def ever_provided_digits(self) -> str:
        return "".join(self._all)

    def mentions_number_recently(self) -> bool:
        return any(_mentions_a_number(text) for text in self._recent_text)


class _TranscriptConsistencyGateTool(AgentTool):
    """Wraps an AgentTool with two checks against _RecentUserDigits, both
    aimed at "this argument doesn't actually reflect what the caller
    provided" but catching different causes:

    1. Any argument shaped like a phone number ("+" followed only by
       digits) that's an exact prefix of what the caller actually said
       recently (i.e. missing only trailing digits) is blocked as a likely
       Nova Sonic speech-to-text drop.

    2. Any argument shaped like a digit-sequence identity field (see
       _DIGIT_SEQUENCE_FIELD_HINTS — a mobile number, an account number,
       ...) with enough digits to check (_FABRICATION_MIN_DIGITS) that
       doesn't appear anywhere in what the caller has said or provided this
       call at all is blocked as fabricated.

    3. Any argument shaped like a date-of-birth field (see
       _DATE_FIELD_HINTS) is blocked as fabricated if no recent caller turn
       mentioned any number-shaped word at all — a weaker check than #2
       (it can't verify the value matches, only that the caller said
       *something* number-like), used because a date is naturally spoken as
       a compound phrase ("the fifteenth of January, nineteen ninety")
       whose digits don't even appear in the order a tool would want them,
       so digit-substring matching structurally cannot apply here the way
       it does for #2.

    Both #2 and #3 catch the model inventing a plausible-looking value
    instead of asking, which SYSTEM_INSTRUCTION already forbids ("never
    fabricate...") but nothing previously enforced. Both skip any field
    name already covered by _GroundingGateTool's grounded_values: that
    field's provenance is that gate's job, and a value legitimately
    returned by an earlier lookup was never spoken by the caller either, so
    it would otherwise look identical to a fabrication here.

    4. Any phone-number-shaped argument that repeats a value already tried
       earlier this call (tracked in tried_numbers) is blocked if the
       caller's most recent speech contains a different, phone-plausible-
       length digit sequence (_STALE_REUSE_MIN_DIGITS) — i.e. Ava is about
       to silently reuse a number the caller has since moved on from.
       Observed bug: after a caller corrects a wrong number — sometimes at
       Ava's own request — the very next lookup call keeps using the
       original, already-failed number anyway, with no indication to the
       caller that the correction was dropped. This doesn't guess what the
       correct number actually is (an identity-verification lookup is the
       wrong place for a guess); it only refuses to let a demonstrably
       stale value slide through unnoticed, forcing Ava back to the caller
       instead — the same "never fabricate, ask instead" principle as #2
       and #3, applied to reuse instead of invention.
    """

    def __init__(
        self,
        wrapped: AgentTool,
        recent_digits: _RecentUserDigits,
        grounded_values: dict[str, set[str]],
        tried_numbers: set[str],
        session_id: str,
    ) -> None:
        super().__init__()
        self._wrapped = wrapped
        self._recent_digits = recent_digits
        self._grounded_values = grounded_values
        self._tried_numbers = tried_numbers
        self._session_id = session_id

    @property
    def tool_name(self) -> str:
        return self._wrapped.tool_name

    @property
    def tool_type(self) -> str:
        return self._wrapped.tool_type

    @property
    def tool_spec(self) -> ToolSpec:
        return self._wrapped.tool_spec

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        arguments = tool_use.get("input") or {}
        spoken = self._recent_digits.spoken_digits()
        truncated_fields = [
            key
            for key, value in arguments.items()
            if isinstance(value, str)
            and _PHONE_LIKE_PATTERN.match(value)
            and len(spoken) > len(value) - 1
            and spoken.startswith(value[1:])
        ]
        if truncated_fields:
            _record_governance_event(
                gate="transcript_consistency",
                action="blocked",
                reason="truncated_digits",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                fields=truncated_fields,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                f"The number in {', '.join(truncated_fields)} doesn't match what the "
                                "caller actually said — you're missing one or more trailing digits they "
                                "spoke. Do not retry with this same number. Read the number back to the "
                                "caller one digit at a time, ask them to confirm the digit count is "
                                "right, and get an explicit yes on the corrected number before calling "
                                "this tool again."
                            )
                        }
                    ],
                }
            )
            return

        ever_provided = self._recent_digits.ever_provided_digits()
        fabricated_fields = []
        for key, value in arguments.items():
            if not isinstance(value, str) or key in self._grounded_values:
                continue
            key_lower = key.lower()
            if any(hint in key_lower for hint in _DATE_FIELD_HINTS):
                # A date of birth entered in the app before the call was never
                # spoken, so the "did the caller just say something
                # number-shaped" fallback below cannot see it. Its digits were
                # seeded though (see _RecentUserDigits.seed), and a date is
                # the one case where they arrive in exactly the order a tool
                # wants them — ISO — so this can be checked properly rather
                # than weakly: the argument's own digits have to appear in
                # what the caller actually provided.
                provided_digits = _digits_only(value)
                if provided_digits and provided_digits in ever_provided:
                    continue
                if not self._recent_digits.mentions_number_recently():
                    fabricated_fields.append(key)
            elif any(hint in key_lower for hint in _DIGIT_SEQUENCE_FIELD_HINTS):
                digits = _digits_only(value)
                if len(digits) >= _FABRICATION_MIN_DIGITS and digits not in ever_provided:
                    fabricated_fields.append(key)
        if fabricated_fields:
            _record_governance_event(
                gate="transcript_consistency",
                action="blocked",
                reason="fabricated_identifier",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                fields=fabricated_fields,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                f"Cannot proceed: {', '.join(fabricated_fields)} doesn't match anything "
                                "the caller has actually said or provided this call. Never invent a value "
                                "for this — ask the caller for it directly, confirm what they say back to "
                                "them, and only then call this tool again."
                            )
                        }
                    ],
                }
            )
            return

        phone_args = [
            (key, value) for key, value in arguments.items()
            if isinstance(value, str) and _PHONE_LIKE_PATTERN.match(value)
        ]
        stale_fields = [
            key
            for key, value in phone_args
            if value in self._tried_numbers
            and len(spoken) >= _STALE_REUSE_MIN_DIGITS
            # Containment, not equality: `spoken` is a windowed buffer that
            # can still hold digits from an earlier, unrelated turn (a dob
            # recorded a moment before) concatenated alongside a legitimate
            # re-confirmation of this same number — checking that the
            # number's own digits still appear *somewhere* in `spoken`
            # avoids flagging that as a mismatch, while a genuinely
            # different number the caller just gave won't appear at all.
            and value[1:] not in spoken
        ]
        if stale_fields:
            _record_governance_event(
                gate="transcript_consistency",
                action="blocked",
                reason="stale_reuse",
                session_id=self._session_id,
                tool_name=tool_use["name"],
                fields=stale_fields,
            )
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                f"Do not call this with {', '.join(stale_fields)} unchanged — that "
                                "exact number was already tried, and the caller has since said "
                                "different digits. Use exactly what the caller most recently said "
                                "(read it back one digit at a time and get an explicit yes first), "
                                "or if you're not sure what they said, ask them to repeat their "
                                "number in full before calling this tool again."
                            )
                        }
                    ],
                }
            )
            return

        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            # Recorded before the yield, not after the loop — post-loop code
            # never runs here (see _LoggingTool.stream), so "numbers already
            # tried this call" never accumulated and the stale-reuse check
            # below had nothing to fire on.
            if isinstance(event, ToolResultEvent):
                for _, value in phone_args:
                    self._tried_numbers.add(value)
            yield event


async def build_gateway_tools(
    mcp_client: MCPClient,
    recent_digits: _RecentUserDigits,
    on_mobile_number_confirmed: Callable[[str], Awaitable[None]],
    known_mobile_number: list[str | None],
    live_call: LiveCall,
) -> list[AgentTool]:
    """Load every tool the gateway currently exposes, unmodified.

    No tool name, parameter list, or description is hardcoded here — change
    what the gateway exposes, and this picks it up automatically next call.
    The single exception: any tool that requires a session_id gets one
    generated fresh for this call injected in (see _FixedArgumentTool),
    since that's plumbing, not something the model should invent.

    Every tool is also wrapped with _ToolOutputSanitizerTool (untrusted
    output, G2.18), _GroundingGateTool (argument provenance),
    _IdempotencyGateTool (safe-retry caching, G1.19), _HighValueClaimGateTool
    (HITL threshold, G4.19), _TranscriptConsistencyGateTool (catches both a
    digit Nova Sonic dropped in speech-to-text, and an identity-shaped
    argument — a date of birth, an account number — the model invented
    rather than one the caller actually provided), and
    _MobileNumberSyncTool (pushes a newly-confirmed mobile/phone number back
    to the browser), and _EscalationNotifierTool (a successful escalation
    opens the human handoff — Slack message, joinable agent portal) — all
    scoped to this call's
    grounded_values/cache/recent_digits/known_mobile_number, so state never
    leaks across connections.

    Every gate above also reports through _record_governance_event when it
    blocks, holds, dedupes, or neutralizes something, tagged with this call's
    session_id — see that function for where those events end up.
    """
    call_session_id = str(uuid.uuid4())
    grounded_values: dict[str, set[str]] = {}
    tried_numbers: set[str] = set()
    all_tools = await mcp_client.load_tools()
    event("gateway.tools_discovered", tools=[t.tool_name for t in all_tools])

    wrapped: list[AgentTool] = []
    for tool in all_tools:
        if _requires_argument(tool, "session_id") or (
            _bare_tool_name(tool) in _SESSION_ID_TOOL_NAMES_WITHOUT_SCHEMA_PROPERTY
        ):
            tool = _FixedArgumentTool(tool, session_id=call_session_id)
        tool = _ToolOutputSanitizerTool(tool, call_session_id)
        tool = _GroundingGateTool(tool, grounded_values, call_session_id)
        tool = _IdempotencyGateTool(tool, call_session_id)
        tool = _HighValueClaimGateTool(tool, call_session_id)
        tool = _TranscriptConsistencyGateTool(tool, recent_digits, grounded_values, tried_numbers, call_session_id)
        tool = _MobileNumberSyncTool(tool, on_mobile_number_confirmed, known_mobile_number)
        tool = _EscalationNotifierTool(tool, live_call)
        wrapped.append(_LoggingTool(tool))

    return wrapped


class _UsageCapturingNovaSonic(BidiNovaSonicModel):
    """Nova Sonic, plus the token counts Strands drops on the floor.

    Nova's `usageEvent` reports tokens split by modality, and speech and text
    are billed at very different rates — so the split is not a nicety, it's
    the difference between a cost figure and a guess. Strands'
    `_convert_nova_event` reads only `totalInputTokens`/`totalOutputTokens`
    from that event and discards `details` before anything downstream can see
    it, and the `BidiUsageEvent` it emits has a `modality_details` field that
    the Nova path never populates.

    `_convert_nova_event` is the single funnel every provider frame passes
    through, which makes it the one place to tap: one dict membership test per
    frame, and the `super()` call still returns exactly what it returned
    before, so nothing about the call's behaviour changes. Tapping
    `receive()` instead would mean re-implementing an async generator; tapping
    the agent's output callbacks would only ever see the totals.

    Overriding a private method is a real coupling to a specific Strands
    version, which is why `requirements.txt` pins `strands-agents==1.54.0`
    exactly. If a bump ever removes or renames this method the failure is
    loud (AttributeError at import, no calls served) rather than a silently
    empty cost dashboard — that is the preferable direction for this trade.
    """

    def _convert_nova_event(self, nova_event: dict[str, Any]):
        if "usageEvent" in nova_event:
            record_nova_usage(nova_event["usageEvent"])
        return super()._convert_nova_event(nova_event)


sonic_model = _UsageCapturingNovaSonic(
    model_id=MODEL_ID,
    provider_config={
        "audio": {
            "voice": "tiffany",  # "tiffany" or "matthew"
            "input_rate": INPUT_SAMPLE_RATE,
            "output_rate": OUTPUT_SAMPLE_RATE,
            "channels": 1,
            "format": "pcm",
        },
        "inference": {},
    },
    client_config={"region": BEDROCK_REGION},
)

class _XRaySigV4Auth:
    """SigV4-sign each OTLP span-export POST for the CloudWatch X-Ray endpoint.

    A ``requests.auth.AuthBase``-shaped callable (duck-typed, so ``requests``
    stays an implicit dependency rather than an imported one). ``requests``
    invokes it during ``PreparedRequest`` construction -- after the body is
    set, before the send -- which is exactly when the payload is final and
    signable, gzip already applied.

    Only ``Content-Type`` is put into the signature. The X-Ray endpoint
    validates ``SignedHeaders`` strictly, and ``requests`` appends
    ``Content-Length``/``User-Agent``/``Accept-Encoding`` *after* this hook
    runs -- signing over those would produce a signature the endpoint cannot
    reproduce.

    Credentials are re-frozen per request rather than captured once: the
    runtime's task role hands out short-lived credentials that boto3 rotates
    underneath us, and a long-running call outlives a single set.
    """

    def __init__(self, region: str) -> None:
        import boto3

        self._region = region
        self._credentials = boto3.Session().get_credentials()

    def __call__(self, request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        signed = AWSRequest(
            method=request.method,
            url=request.url,
            data=request.body,
            headers={"Content-Type": request.headers.get("Content-Type", "")},
        )
        SigV4Auth(
            self._credentials.get_frozen_credentials(), "xray", self._region
        ).add_auth(signed)
        for header in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token"):
            if header in signed.headers:
                request.headers[header] = signed.headers[header]
        return request


def _setup_telemetry() -> None:
    """Export Strands' spans to CloudWatch GenAI Observability.

    Strands already instruments the whole call on its own -- a `bidi_session`
    span per connection, an `execute_tool` child span per tool call carrying
    the tool's name, arguments and result, plus response spans and
    interruption events. What it does *not* do is decide where those go: it
    emits against whatever global TracerProvider exists, and with none
    configured that is a no-op provider that silently drops every span.

    AgentCore Runtime does *not* run an OTLP collector inside a custom
    container -- verified the hard way: pointing the exporter at the
    conventional `localhost:4318` produced `Connection refused` on every
    batch while the spans themselves were being created correctly. Traces
    instead go straight to CloudWatch's own OTLP endpoint,
    `https://xray.<region>.amazonaws.com/v1/traces`, which needs SigV4
    signing (see `_XRaySigV4Auth`) and the execution role's
    `xray:PutTraceSegments`. That endpoint is what backs the same
    GenAI Observability console the platform's own `AgentCore.Runtime.Invoke`
    span shows up in, so the agent's spans land as children of it rather
    than in a separate system -- no ADOT distro and no
    `opentelemetry-instrument` wrapper needed; the OTel SDK, the OTLP
    exporter and botocore's signer are all already installed.

    Gated on AGENT_OBSERVABILITY_ENABLED so a local run stays quiet: there
    the no-op provider is the correct behaviour, and nothing should be
    shipping a developer's test calls to CloudWatch.

    Also gated on nobody having done it already. The container's CMD runs
    under `opentelemetry-instrument`, which configures the SDK (and its
    exporter) before uvicorn imports this module, so in the deployed container
    this function normally finds a provider in place and leaves it alone —
    adding a second exporter to it would export every span twice. What it does
    instead is stay as the fallback for every other way this runs: `python
    strands_agent.py`, a plain `uvicorn` command, an image built before the
    wrapper. Same destination either way; only the wiring differs.
    """
    if os.getenv("AGENT_OBSERVABILITY_ENABLED", "").lower() not in ("true", "1", "yes"):
        event("telemetry.disabled", reason="AGENT_OBSERVABILITY_ENABLED not set")
        return

    # A real SDK provider here means the process was started under
    # `opentelemetry-instrument`: nothing in this file installs one before
    # now, and an unconfigured OTel API hands back a proxy/no-op instead.
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

        existing = otel_trace.get_tracer_provider()
        if isinstance(existing, SdkTracerProvider):
            endpoint = (os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "the distro's default")
            event("telemetry.already_configured", endpoint=endpoint,
                  by="opentelemetry-instrument")
            return
    except Exception as e:
        # Never fatal: worst case this falls through and configures tracing
        # itself, which is what every non-wrapped run does anyway.
        event("telemetry.provider_probe_failed", error=str(e))

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from strands.telemetry import StrandsTelemetry

        # GATEWAY_REGION is this deployment's AWS_REGION -- the region whose
        # CloudWatch console shows the runtime's own AgentCore.Runtime.Invoke
        # span, so the agent's spans have to land in the same one to join it.
        region = os.getenv("XRAY_REGION") or GATEWAY_REGION
        endpoint = f"https://xray.{region}.amazonaws.com/v1/traces"

        exporter = OTLPSpanExporter(endpoint=endpoint)
        # The auth hook has to hang off the exporter's own session; there is
        # no constructor argument for it.
        exporter._session.auth = _XRaySigV4Auth(region)

        # StrandsTelemetry() builds the provider with Strands' resource
        # attributes and registers it globally, which is the half that was
        # missing. Its own setup_otlp_exporter() is deliberately not used --
        # it builds an unsigned exporter from OTEL_EXPORTER_OTLP_ENDPOINT.
        telemetry = StrandsTelemetry()
        telemetry.tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        event("telemetry.configured", endpoint=endpoint, auth="sigv4")
    except Exception as e:
        # Losing traces is not worth losing calls over.
        event("telemetry.setup_failed", error=str(e))


_setup_telemetry()

app = FastAPI()

# The human handoff surface: /portal (the agent console), its call API, and
# the portal WebSocket that bridges a human agent's audio into a live call.
# AgentCore Runtime only requires /ws and /ping; these are extra routes on the
# same app, which is what lets a human join a call this process is holding.
app.include_router(handoff.router)
# Mounting the portal's routes and applying its cross-origin policy are one
# step, so a deployment that hosts portal.html elsewhere can't end up serving
# a page that loads and then silently can't fetch. A no-op unless
# PORTAL_ALLOWED_ORIGINS is set.
handoff.install_portal_cors(app)


@app.get("/ping")
async def ping():
    """Health check endpoint required by AgentCore Runtime."""
    return {"status": "Healthy", "time_of_last_update": int(datetime.now().timestamp())}


# A Runtime session id is chosen by whoever presigns the URL, so it reaches
# this process as client-controlled text and then travels on to a DynamoDB
# attribute and into a signed URL's query string. Bounded to the characters
# AgentCore itself accepts before any of that.
_RUNTIME_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:@\-]{8,200}$")


@app.websocket("/ws")
async def voice_chat(websocket: WebSocket) -> None:
    """The one WebSocket path this container publishes — and two kinds of
    client arrive on it.

    Normally a caller: _serve_caller runs their voice call, and returns None.

    But AgentCore Runtime gives a container exactly this one path and picks the
    instance from the session id in the signed URL, so a human agent joining a
    live call has no other endpoint they could possibly use. They presign this
    path with the *caller's* session id — which is what routes them into the
    container holding that call — and their first frame says which call they
    want (handoff.PORTAL_ATTACH_TYPE). _serve_caller hands that frame straight
    back here, untouched, and the connection becomes a portal instead.

    The dispatch is out here, rather than inside _serve_caller, because a
    portal socket must survive that function's teardown: its `finally` ends a
    call and closes the socket, both of which are exactly wrong for a
    connection that is only now starting to do its job.
    """
    attach = await _serve_caller(websocket)
    if attach is not None:
        await handoff.serve_portal_socket(websocket, attach)


# The whole budget for shutting Nova Sonic down when a human takes a call —
# not per step, so a slow stop() can't be followed by an equally slow wait and
# leave the human agent hanging. A live person is on the line by this point,
# so the bound matters more than the tidiness: whatever happens, the session
# is gone and the bridge is running within about this long.
#
# Nothing goes deaf while it runs, which is why a few seconds is affordable:
# run() relays the caller's audio to the portal before it consults any handoff
# gate, so the human keeps hearing the call right up until the moment run()
# returns, and the bridge takes over in the same breath.
AVA_DISCONNECT_TIMEOUT = 5


async def _disconnect_ava(voice_agent: BidiAgent, session: asyncio.Task) -> None:
    """Take the model off a live call, for a human handoff.

    Different from the gates in _serve_caller, which only stop Ava's audio
    reaching the caller: this ends the Nova Sonic session itself. Afterwards
    there is no model on the call at all — nothing hearing what the caller
    tells a person, and nothing that could resume speaking over them.

    Bounded, and never allowed to raise. A human agent has already been told
    they're on this call; a model that won't shut down cleanly must not take
    that call down with it. stop() gets the first, polite attempt; if run()
    hasn't returned by then the task is cancelled outright. Either way the
    session is finished before this returns.
    """
    deadline = time.monotonic() + AVA_DISCONNECT_TIMEOUT

    def left() -> float:
        """What's still on the budget — never zero, so each step gets a go."""
        return max(0.1, deadline - time.monotonic())

    try:
        await asyncio.wait_for(voice_agent.stop(), timeout=left())
    except Exception as exc:
        event("handoff.stop_failed", error=str(exc))

    # stop() returning is not the same as run() having returned: it may have
    # only just asked the session to close. So wait for the task itself before
    # taking it away — without this, an agent shutting down perfectly cleanly
    # still gets cancelled a scheduler tick short of the finish line.
    #
    # asyncio.wait, not `await session`, throughout: it reports how the task
    # ended without re-raising it here, where there's nothing useful to do
    # with a failure and a live bridge to get on with.
    await asyncio.wait({session}, timeout=left())
    if not session.done():
        session.cancel()
        await asyncio.wait({session}, timeout=left())

    if session.done() and not session.cancelled():
        failure = session.exception()
        if failure is not None:
            event("handoff.session_ended_with_error", error=str(failure))


async def _bounded_teardown(coro: Any, *, failed_event: str, timeout: float) -> None:
    """Run one teardown step, and abandon it rather than lose the ledger line.

    Every step in the teardown closes something that has usually already
    broken — that is generally why the call is ending — and none of them has
    a deadline of its own. `BidiAgent.stop()` is the dangerous one: it awaits
    `model.stop()`, which closes the Nova Sonic stream over awscrt, and when
    the HTTP/2 stream was cancelled rather than closed (every call here logs
    `InvalidStateError: CANCELLED` from awscrt at hangup) that await can
    simply never return.

    That mattered because `emit_call_ledger` runs after all of this. Observed
    on 2026-09-11: call 1a7d361b7d27 logged `call.caller_disconnected`, hung
    in teardown, and was killed by the runtime's ~40-minute pool recycle
    before writing a ledger line. The call cost real money, held a session,
    and appears in no metric — the one failure mode this whole module exists
    to prevent.

    So each step gets a deadline. An abandoned socket close costs nothing;
    the process is going away regardless. A missing ledger line is a call
    that, to every cost figure and error rate downstream, never happened.
    """
    try:
        await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        # Named separately from the failure case: a hang and an exception want
        # different fixes, and folding them together hides which one is happening.
        event(f"{failed_event}.timeout", level=logging.WARNING,
              timeout_seconds=timeout)
    except Exception as cleanup_error:
        event(failed_event, error=str(cleanup_error))


async def _serve_caller(websocket: WebSocket) -> dict[str, Any] | None:
    """One bidirectional voice call, or None—see voice_chat.

    A fresh BidiAgent and gateway connection are created per call, so
    concurrent callers never share state (session_id, tool results, etc).

    Returns the portal attach frame if this connection turns out to be a human
    agent rather than a caller, having left the socket open and untouched for
    voice_chat to hand on. Returns None for every real call.
    """
    mcp_client = MCPClient(
        transport_callable=lambda: aws_iam_streamablehttp_client(
            endpoint=GATEWAY_URL,
            aws_region=GATEWAY_REGION,
            aws_service="bedrock-agentcore",
        )
    )
    voice_agent: BidiAgent | None = None

    # One memory session per call: this is what the call's turns are stored
    # under, and what a session summary is later extracted from.
    memory_session_id = str(uuid.uuid4())

    # This call as the human-handoff side sees it: the transcript a human
    # agent reads, the escalation reason, and the audio bridge. Registered up
    # front (not at escalation time) because the transcript has to have been
    # accumulating all along for it to be worth reading later.
    live_call = handoff.register_call()
    live_call.customer_send = websocket.send_json

    # Everything observable about this call hangs off these four lines, and
    # they run before the first task is spawned on purpose: a ContextVar is
    # copied into a task at creation, so anything started after this point —
    # the gateway load, the model's receive loop, every tool call — inherits
    # the ids and writes into the same accumulators, while anything already
    # running (or unrelated, like /ping) keeps the defaults.
    CALL_ID.set(live_call.call_id)
    call_usage = CallUsage()
    CALL_USAGE.set(call_usage)
    COUNTERS.set({})
    call_started = time.monotonic()
    event("call.started")

    # Set when a human agent presses Join in the portal. What follows it is a
    # real disconnect, not a mute: Ava's Nova Sonic session is torn down and
    # the rest of the call is a plain audio bridge between the two browsers
    # (see the run/bridge split at the end of this function). Handed to the
    # handoff side as a bare `set`, so joining can never be blocked by — or
    # fail because of — anything this side does with it.
    human_takeover = asyncio.Event()
    live_call.request_ai_disconnect = human_takeover.set

    # Single-item mutable box (not a plain variable) so build_gateway_tools
    # -- which is already running as a background task by the time
    # mobile_number below is known -- and the seeding step further down
    # both observe the same live value rather than each getting a frozen
    # copy from whenever they captured it. Also out here so the memory
    # write at teardown can read the final confirmed number.
    known_mobile_number: list[str | None] = [None]

    # Set when the first frame reveals this isn't a caller at all. Read by the
    # teardown below, which must then leave the socket (and the call this
    # connection is joining) alone — see voice_chat.
    handed_to_portal = False

    # Set once a human handoff has already stopped the voice agent, so the
    # teardown doesn't stop it a second time.
    ava_disconnected = False
    # Whether the Nova Sonic session has already been stopped, by either
    # of the two paths that stop it. Separate from ava_disconnected,
    # which means specifically "a human took the call" and is what the
    # ledger reads as a handoff.
    ava_stopped = False

    # What the call ledger reports as this call's outcome. Overwritten by the
    # handlers below; "completed" is only correct if nothing else claims it.
    call_outcome = "completed"
    call_error: str | None = None

    # build_gateway_tools() -> load_tools() starts mcp_client's own
    # background connection lazily; wrapping this in `with mcp_client:`
    # too would start it twice and raise. mcp_client.stop() below is the
    # equivalent cleanup, called directly instead of via __exit__.
    try:
        # Load gateway tools concurrently with accepting the socket instead
        # of sequentially — the gateway round-trip otherwise added its full
        # latency on top of accept() before the caller's mic could even
        # start streaming.
        recent_digits = _RecentUserDigits()

        async def notify_mobile_number(value: str) -> None:
            # Best-effort: a send failing here (e.g. the socket closed mid-call)
            # shouldn't take down the call itself, just the UI sync.
            live_call.mobile_number = value
            try:
                await websocket.send_json({"type": "bidi_call_metadata_update", "mobile_number": value})
            except Exception as exc:
                event("mobile_sync.notify_failed", error=str(exc))

        tools_task = asyncio.create_task(
            build_gateway_tools(
                mcp_client, recent_digits, notify_mobile_number, known_mobile_number, live_call
            )
        )
        prompt_task = asyncio.create_task(get_system_prompt())
        await websocket.accept()
        event("call.socket_accepted")

        # frontend/index.html sends one JSON metadata message immediately
        # after the socket opens, before any audio — {"type":
        # "bidi_call_metadata", "mobile_number": "..."} if the caller typed
        # their number into the UI, otherwise no mobile_number key. Received
        # concurrently with the tools/prompt loads below so it doesn't add
        # latency. A client that never sends it (or sends audio first) just
        # times out here and the call proceeds exactly as before.
        #
        # Returned raw rather than filtered to bidi_call_metadata, because this
        # same frame is also how a human agent's portal identifies itself (see
        # voice_chat) — a filter here would swallow that and leave the portal
        # waiting forever for a call it never announced.
        async def receive_first_message() -> dict[str, Any]:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=3.0)
            except Exception:
                return {}
            return message if isinstance(message, dict) else {}

        first_message_task = asyncio.create_task(receive_first_message())

        # Awaited before the gateway and prompt loads, not after: those are
        # already in flight and keep loading regardless, and a portal
        # connection has no use for either — making it wait on them would add a
        # gateway round trip to every join for nothing.
        first_message = await first_message_task
        if handoff.is_portal_attach(first_message):
            handed_to_portal = True
            # Neither is needed now, and both hold a connection open.
            tools_task.cancel()
            prompt_task.cancel()
            # This connection was never a call. Left registered, the
            # placeholder above would show up on /portal/queue as a live call
            # with no caller, and _prune_calls would never collect it — it only
            # expires calls that have ENDED, which this one never will.
            handoff.unregister_call(live_call)
            return first_message

        initial_metadata = (
            first_message if first_message.get("type") == "bidi_call_metadata" else {}
        )

        # Which AgentCore Runtime session this call arrived on. A human agent
        # joining later has to presign with this exact value to be routed to
        # this container, so it goes onto the escalations row at escalation
        # time (see handoff._persist_portal_keys) — nothing else in the system
        # can discover it.
        #
        # Runtime does forward its own session header through the WebSocket
        # handshake — confirmed on the deployed runtime by a client that sent
        # no messages at all and was still logged with the right session id —
        # and that's the source used. The caller page's own report (read out of
        # the presigned URL it was handed) is a fallback for if that ever
        # stops being true: the header is the platform's view of where it
        # actually routed this connection, which beats a client's claim about
        # it, so it wins whenever both are present.
        runtime_session_id = websocket.headers.get(
            "x-amzn-bedrock-agentcore-runtime-session-id") or ""
        if not runtime_session_id:
            claimed = initial_metadata.get("runtime_session_id")
            if isinstance(claimed, str) and _RUNTIME_SESSION_ID_PATTERN.match(claimed.strip()):
                runtime_session_id = claimed.strip()
        live_call.runtime_session_id = runtime_session_id or None

        # This is the join key for everything outside this process. AgentCore
        # stamps the same value on every record it writes to
        # .../USAGE_LOGS/<agent>, which is where the call's billed vCPU-hours
        # and GB-hours live — so with it on the span and on every log line,
        # "what did this call cost" spans model tokens (the ledger) and
        # compute (the usage logs) without a correlation table anywhere.
        if runtime_session_id:
            SESSION_ID.set(runtime_session_id)
        try:
            from opentelemetry import trace as _trace

            _span = _trace.get_current_span()
            if _span is not None and _span.is_recording():
                _span.set_attribute("ava.call_id", live_call.call_id)
                if runtime_session_id:
                    _span.set_attribute("session.id", runtime_session_id)
        except Exception as exc:
            event("telemetry.span_tagging_failed", error=str(exc))

        event(
            "call.routed",
            runtime_session_known=bool(runtime_session_id),
            note=None if runtime_session_id else (
                "a portal hosted off this container will be able to read this "
                "call but not join it"),
        )

        tools = await tools_task
        system_prompt = await prompt_task
        # Neither the local SYSTEM_INSTRUCTION nor the Bedrock-managed prompt
        # has any way to know today's date on its own — without this, "the
        # incident was yesterday" has nothing real to resolve against, and
        # the model fills in a plausible-looking but fabricated date instead
        # (observed: a caller's "yesterday" turned into an unrelated date
        # from a different year). Computed fresh per call, not baked into
        # any prompt text, so it's always right regardless of when the
        # container was last deployed.
        today = datetime.now(timezone.utc)
        system_prompt = (
            f"{system_prompt}\n\n"
            f"Today's date is {today:%Y-%m-%d} ({today:%A}), UTC. Resolve any "
            "relative date the caller gives you (\"yesterday\", \"last Monday\", "
            "\"three days ago\") against this, in ISO 8601 (YYYY-MM-DD) format "
            "for any tool argument — never invent a date."
        )
        # Resolved before the agent is built, not after: the memory session
        # manager needs the caller's number as its actor id at construction
        # time, since that's the key both this call's history and any earlier
        # call's extracted memory are stored under.
        #
        # Only digits/+/space/dash, a plausible phone length: anything else
        # (including an attempted prompt injection typed into the field —
        # this reaches the model as literal text below) is dropped, and the
        # call proceeds exactly as if no number had been provided.
        raw_mobile_number = initial_metadata.get("mobile_number")
        mobile_number = (
            raw_mobile_number.strip()
            if isinstance(raw_mobile_number, str) and _PHONE_LIKE_UI_PATTERN.match(raw_mobile_number.strip())
            else None
        )
        # The other two fields the pre-call form collects. Together with the
        # number they're exactly the triple the gateway's identity check
        # wants, which is the point of asking for them up front: nobody
        # should have to recite their own date of birth down a phone line to
        # be recognised. Same treatment as the number — validated (see
        # _ui_full_name / _ui_date_of_birth) or simply absent.
        caller_name = _ui_full_name(initial_metadata.get("full_name"))
        caller_dob = _ui_date_of_birth(initial_metadata.get("date_of_birth"))

        # All three are registered as provided-but-not-spoken, so
        # _TranscriptConsistencyGateTool doesn't flag a later tool call using
        # one as fabricated just because the caller typed it instead of saying
        # it aloud — and all three go onto the live call, so a human agent
        # picking it up sees who is on the line.
        if mobile_number:
            recent_digits.seed(mobile_number)
            known_mobile_number[0] = mobile_number
            live_call.mobile_number = mobile_number
        if caller_name:
            live_call.caller_name = caller_name
        if caller_dob:
            live_call.caller_dob = caller_dob
            recent_digits.seed(caller_dob)

        if mobile_number:
            # Opening sequence for a caller who identified themselves in the
            # app: look their policy up *before* saying anything, then greet
            # them by the name on it.
            #
            # This is appended to the system prompt rather than left to the
            # kickoff text below because the base prompt (local
            # SYSTEM_INSTRUCTION or, when BEDROCK_PROMPT_ID is set, whatever
            # Bedrock Prompt Management currently holds) scripts an exact
            # opening line. A user-role nudge doesn't reliably beat that;
            # system-level text that says which of the two openings applies
            # to this call does — and it keeps working without anyone having
            # to edit the managed prompt in the console to match.
            #
            # No tool is named here (same reason as everywhere else in this
            # file): the gateway's tool set is discovered live, so this
            # describes the tool by what it does and lets the model pick it.
            system_prompt = (
                f"{system_prompt}\n\n"
                "Opening sequence for this call — this replaces any scripted "
                "opening line in the instructions above.\n"
                "The caller identified themselves in the app before the call, so:\n"
                "1. Say nothing at all yet. Your first action is to look up their "
                "policy by that mobile number. No greeting, no \"one moment\", no "
                "sound of any kind before the result comes back.\n"
                "2. When it comes back, greet them by the first name on the policy "
                "and introduce yourself in the same turn — for example: \"Hello "
                "<first name>, I'm Ava, your AI Assistant. I can help with your "
                "policy details, register a claim, or connect you with a human "
                "agent.\" Use the name exactly as the lookup gave it; the first "
                "name alone is enough. Then listen.\n"
                "3. If the lookup returns no policy, or no name you can use, open "
                "with that same line minus the name and carry on under the normal "
                "rules — never guess, shorten or invent a name.\n"
                "You already have the caller's details from this lookup, so don't "
                "ask them for the number, mention the lookup, or repeat it."
            )

        # The name and date of birth from the same form, given to the model as
        # facts it already holds rather than as questions to ask. This is what
        # makes the pre-call fields worth filling in: the identity check the
        # gateway offers takes a number, a date of birth and a full name, and
        # a caller who typed all three shouldn't be asked to say any of them
        # out loud.
        #
        # Worth being clear about what that means, because it's a policy
        # choice and not an accident: on this path the identity check is
        # satisfied by whoever is holding the device, exactly as it already
        # was for the pre-filled mobile number. That's the right shape for a
        # signed-in app and the wrong shape for an anonymous web page — if
        # this ever fronts one, the pre-call form should stop being trusted
        # for verification and go back to being a hint.
        if caller_name or caller_dob:
            provided = []
            if caller_name:
                provided.append(f'their full name, exactly as typed: "{caller_name}"')
            if caller_dob:
                provided.append(f"their date of birth, ISO format: {caller_dob}")
            system_prompt = (
                f"{system_prompt}\n\n"
                "The caller also entered these details in the app before the "
                "call, so you already have them:\n- "
                + "\n- ".join(provided)
                + "\nThey were typed rather than spoken, so there is no "
                "speech-to-text risk in them: use them exactly as given "
                "whenever a tool needs the caller's name or date of birth, "
                "and never ask the caller for either, read one back to "
                "confirm it, or say the date of birth aloud at all. Greet "
                "them by the first name the policy lookup returns when there "
                "is one; the typed name is only for when there isn't. If the "
                "caller states a different name or date of birth at any point "
                "in the call, that correction wins over what was typed — "
                "confirm the new value the normal way and use only that from "
                "then on."
            )

        # A caller who didn't pre-fill their number gets a per-call actor id
        # instead: this call still gets session memory (and shows up whole in
        # observability), but there's nothing stable to tie it to a previous
        # or future call, which is the honest outcome -- guessing an identity
        # here would attach one caller's history to another's.
        memory_actor_id = _memory_actor_id(mobile_number, memory_session_id)

        # Same keys the session manager writes under, handed to the handoff
        # side so the agent portal can read this call's conversation back out
        # of AgentCore Memory instead of relying on this process's own list.
        live_call.memory_actor_id = memory_actor_id
        live_call.memory_session_id = memory_session_id

        # --- Ava ending the call herself -----------------------------------
        #
        # Until now every call ended because the caller closed the tab, which
        # left the outcome reading `caller_disconnected` even when the call
        # had gone perfectly and both sides had said goodbye. A call that
        # reached its natural end is a different thing from one that dropped,
        # and the ledger could not tell them apart.
        #
        # Ava now asks for the hangup and the connection performs it, rather
        # than the tool closing anything itself: the tool runs inside the
        # model's event loop, and tearing that loop's socket out from under
        # it is how you lose the goodbye and the ledger line together.
        wants_to_end = asyncio.Event()
        end_call_reason: list[str] = []
        # Advanced by every audio frame Ava sends, so the farewell watcher can
        # tell "still speaking" from "finished".
        last_ava_audio = time.monotonic()

        @strands_tool
        def end_call(reason: str) -> str:
            """End the call. Use only once the caller has confirmed they need nothing further.

            Call this after the caller has said goodbye, said they have no more
            questions, or otherwise confirmed they are done — and never as a way
            to get out of a question you cannot answer, a caller who is upset, or
            a request you would otherwise have to escalate. Those go to a human.

            Say your closing line after calling this, not before: the line stays
            open until you have finished speaking, then closes on its own.

            Args:
                reason: A short note for the record of why the call is ending,
                    e.g. "caller confirmed their claim status and had nothing else".
            """
            nonlocal last_ava_audio
            if live_call.human_on_call:
                # A human took the call: it is not Ava's to end, and closing
                # the socket here would hang up on their conversation.
                return "A human agent is handling this call. Do not end it."
            end_call_reason.append(reason)
            # The grace period starts now, not at her last frame — the caller
            # has just been talking, so Ava's own last audio may be many
            # seconds old and the watcher would close before she replies.
            last_ava_audio = time.monotonic()
            wants_to_end.set()
            event("call.end_requested", reason=reason)
            return ("The line will close once you stop speaking. "
                    "Say a brief, warm goodbye now.")

        async def wait_for_farewell() -> None:
            """Resolve once Ava has asked to end the call and finished speaking."""
            await wants_to_end.wait()
            deadline = time.monotonic() + FAREWELL_MAX_SECONDS
            while time.monotonic() < deadline:
                if time.monotonic() - last_ava_audio >= FAREWELL_SILENCE_SECONDS:
                    return
                await asyncio.sleep(0.1)
            event("call.farewell_timeout", level=logging.WARNING,
                  max_seconds=FAREWELL_MAX_SECONDS)

        # Both built on a worker thread, not the event loop: creating the
        # session manager reads this actor's existing events from AWS, and
        # constructing the agent with one attached does further blocking
        # session/history reads that Strands won't dispatch asynchronously
        # (async_mode only covers the per-turn calls once the call is
        # running). There is already a caller on this socket, so a blocked
        # loop here is dead air.
        def _build_agent() -> BidiAgent:
            return BidiAgent(
                model=sonic_model,
                tools=[*tools, stop_conversation, end_call],
                system_prompt=system_prompt,
                session_manager=build_memory_session_manager(
                    memory_actor_id, memory_session_id
                ),
                # Named so the session/response spans Strands emits are
                # identifiable per call in CloudWatch rather than anonymous.
                agent_id="ava-voice-agent",
                name="Ava",
            )

        voice_agent = await asyncio.to_thread(_build_agent)

        # Nova Sonic only speaks in response to a turn it detects — without
        # this nudge Ava sits silently until the caller says something first,
        # even though the system prompt tells her to open with a greeting.
        # The very first "input" fed into run() is this synthetic text event
        # instead of a real websocket message; every call after that is the
        # real websocket.receive_json(). It's framed as a caller line only
        # because BidiAgent.send() always marks text input role=USER — the
        # system prompt's own directive to greet first takes priority over
        # reacting to this literal text.
        #
        # When the caller already typed their mobile number into the UI,
        # that text carries no speech-to-text risk (unlike a spoken number),
        # so this tells Ava to use it straight away instead of asking for it
        # and reading it back digit by digit, per SYSTEM_INSTRUCTION. This is
        # a default for the *first* lookup only, not a lock for the whole
        # call — observed bug: without the explicit override note below, Ava
        # treated "already confirmed" as permanent and kept reusing this
        # number even after the caller explicitly said it was wrong and
        # stated a different one. A caller correction must always win.
        if mobile_number:
            greeting_text = (
                "(the call has just connected. The caller already entered their "
                f"mobile number through the app, exactly as typed: {mobile_number}. "
                "It was not spoken, so there is no speech-to-text risk for this "
                "specific value — treat it as already confirmed for your first "
                "lookup only, without asking for it or reading it back to confirm. "
                "This is a default, not a lock: if the caller says this number is "
                "wrong, or states a different number at any point in the call, "
                "that correction overrides it — confirm the new number the normal "
                "way (read it back one digit at a time, get an explicit yes) and "
                "use only that confirmed number from then on, never falling back "
                "to this original one. Follow the opening sequence in your "
                "instructions: look up the policy first, say nothing until the "
                "result is back, then greet them by name — begin now)"
            )
        else:
            greeting_text = "(the call has just connected — begin now)"

        # Nothing about recalled memory is spliced in here: the session manager
        # retrieves what it needs and injects it into the model's context
        # itself, wrapped in its own tag, so the kickoff text stays purely
        # about this call.
        greeted = False

        # One receive task, reused across keepalive timeouts. Not a fresh
        # `asyncio.wait_for(websocket.receive_json(), ...)` each time: that
        # cancels the pending receive, and a message arriving in the same
        # instant is then lost. Holding the task and waiting on it again keeps
        # every caller message.
        pending_receive: asyncio.Task | None = None

        async def next_caller_event() -> dict[str, Any] | None:
            """The caller's next message, or None if KEEPALIVE_SECONDS passed."""
            nonlocal pending_receive
            if pending_receive is None:
                pending_receive = asyncio.create_task(websocket.receive_json())
            done, _ = await asyncio.wait({pending_receive}, timeout=KEEPALIVE_SECONDS)
            if pending_receive in done:
                message = pending_receive.result()
                pending_receive = None
                return message
            return None

        # The first half of the human handoff lives in these two functions:
        # `run()` pulls the caller's events through the first and pushes Ava's
        # out through the second, so gating both is what silences her the
        # instant someone presses Join — before any teardown has had time to
        # happen. The second half is the disconnect at the end of this
        # function, which then ends her session outright.
        #
        # What it must NOT do is stop feeding the model. Nova Sonic drops the
        # session with "Timed out waiting for audio bytes or interactive
        # content" after ~55s of silence on the wire, and that exception ends
        # run() -- which tears down the caller's socket mid-sentence. Three
        # ordinary things stop the audio: a human taking the call, the caller
        # pressing mute (the browser sends nothing at all while muted), and a
        # backgrounded tab throttling its timers. So whenever there's no real
        # caller audio to forward, silence is fed in its place -- which is
        # exactly what a live mic sends when nobody is talking.
        async def receive_with_greeting_kickoff():
            nonlocal greeted
            if not greeted:
                greeted = True
                return {"type": "bidi_text_input", "text": greeting_text}
            while True:
                message = await next_caller_event()
                if message is None:
                    return _silent_audio_event()  # nothing arriving; keep the session alive

                # A portal that's open hears the call whether or not its agent
                # has joined -- listening before taking over is the point.
                if message.get("type") == "bidi_audio_input" and live_call.portals:
                    await live_call.relay_caller_audio(message)

                if live_call.human_on_call:
                    # Ava is off the line: she must not hear the caller
                    # talking to a person, but her session still has to be
                    # fed, so the frame is replaced rather than dropped.
                    if message.get("type") == "bidi_audio_input":
                        return _silent_audio_event()
                    continue
                return _normalize_caller_audio(message)

        async def send_to_caller(event: dict[str, Any]) -> None:
            """Ava -> caller, unless a human has taken the call."""
            nonlocal last_ava_audio
            event_type = event.get("type")
            if event_type == "bidi_audio_stream":
                # Every frame she sends pushes the hangup further out; see
                # wait_for_farewell.
                last_ava_audio = time.monotonic()
                await live_call.relay_ava_audio(event)
            if live_call.human_on_call and event_type in {"bidi_audio_stream", "bidi_transcript_stream"}:
                return
            await websocket.send_json(event)

        async def log_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type == "bidi_transcript_stream" and event.get("is_final"):
                who = "You" if event.get("role") == "user" else "Ava"
                text = event.get("current_transcript") or event.get("text")
                if text and "the call has just connected" not in text:
                    # log.info rather than the event() helper: `event` is this
                    # function's parameter, so the name is bound locally here
                    # and the module-level logger is the only way through.
                    log.info(
                        "%s: %s", who, text,
                        extra={"event": "transcript.final",
                               "fields": {"speaker": who, "text": text}},
                    )
                    if event.get("role") == "user":
                        recent_digits.record(text)
                    # The same finished lines the caller sees, kept for
                    # whoever picks this call up in the agent portal.
                    await live_call.record_transcript(event.get("role") or "assistant", text)
            elif event_type == "bidi_interruption":
                log.info(
                    "interruption",
                    extra={"event": "call.interruption",
                           "fields": {"reason": event.get("reason")}},
                )

        async def bridge_caller_to_human() -> None:
            """The rest of the call once Ava has been disconnected.

            run() is gone, and with it the only thing that was pulling the
            caller's frames off this socket — so this does, for as long as
            they stay on the line, relaying their audio to whoever is watching
            in the portal. The other direction needs nothing here: the human's
            mic already goes straight to this socket from
            handoff.relay_human_audio.

            No silence injection, unlike receive_with_greeting_kickoff above:
            that exists to stop Nova Sonic timing out, and there is no Nova
            Sonic session left to time out. A keepalive tick with nothing on
            it is genuinely nothing to do.

            Nor is a caller message lost across the switch. next_caller_event
            holds its pending receive in an enclosing variable rather than
            re-issuing one per wait, and asyncio.wait never cancels what it
            waits on — so a frame that arrived while Ava was being torn down
            is still sitting in that same task, and the first call here
            collects it.
            """
            while True:
                message = await next_caller_event()
                if message is None:
                    continue
                if message.get("type") == "bidi_audio_input":
                    await live_call.relay_caller_audio(message)

        # Ava's session runs as its own task so that a human pressing Join can
        # *end* it, rather than only being layered on top of it. Before this
        # it stayed open for the whole call being fed silence, which meant the
        # model was still on the line — listening to a caller who thinks
        # they're talking to a person in private, and one dropped gate away
        # from speaking over them. Whichever of these three finishes first
        # decides how the rest of the call goes.
        ava_session = asyncio.create_task(voice_agent.run(
            inputs=[receive_with_greeting_kickoff],
            outputs=[send_to_caller, log_event],
        ))
        taken_over = asyncio.create_task(human_takeover.wait())
        farewell = asyncio.create_task(wait_for_farewell())
        try:
            finished, _ = await asyncio.wait(
                {ava_session, taken_over, farewell},
                return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # This handler is going away entirely (a server shutdown, say).
            # asyncio.wait never cancels what it waits on, so without this the
            # model session would be left running with nobody reading it.
            ava_session.cancel()
            raise
        finally:
            taken_over.cancel()
            farewell.cancel()

        # Which task won has to be read from `finished`, not from .done():
        # the `finally` above cancels the losers, and a cancelled task reports
        # done() too, so by this point every one of them looks finished.
        if ava_session.done():
            # The ordinary end of a call — the caller hung up, the model
            # closed the session, or it failed. result() re-raises whatever
            # run() raised, so failures still reach the handler below exactly
            # as they did when this was a bare `await`.
            ava_session.result()
        elif farewell in finished:
            # Ava asked to end the call and has finished saying goodbye.
            # Her session is stopped here rather than left to lapse: the
            # microVM is billed for as long as it lives, so the difference
            # between closing now and closing at the idle timeout is most of
            # what the call costs.
            await _disconnect_ava(voice_agent, ava_session)
            ava_stopped = True
            call_outcome = "completed"
            event("call.ended_by_agent",
                  reason=end_call_reason[0] if end_call_reason else None,
                  duration_seconds=round(time.monotonic() - call_started, 3))
            try:
                # Lets the browser say the call was completed rather than
                # dropped. Best-effort: the socket closing a moment later is
                # what actually ends the call.
                await websocket.send_json(
                    {"type": "bidi_call_ended", "reason": "completed"})
            except Exception:
                pass
        else:
            await _disconnect_ava(voice_agent, ava_session)
            ava_stopped = True
            ava_disconnected = True
            await live_call.mark_ai_disconnected()
            event("handoff.ava_disconnected", human_label=live_call.human_label)
            await bridge_caller_to_human()

    except WebSocketDisconnect:
        # The ordinary way a call ends: the caller closed the tab or hung up.
        # Not an error, and deliberately not folded into the branch below —
        # counting hangups as failures would make the error rate meaningless.
        call_outcome = "caller_disconnected"
        event("call.caller_disconnected")
    except Exception as e:
        call_outcome = "error"
        call_error = f"{type(e).__name__}: {e}"
        log.exception("call.failed", extra={"event": "call.failed"})
    finally:
        # The first thing, unconditionally. Three calls on 2026-09-11 logged
        # their last event (call.caller_disconnected, or call.ended_by_agent)
        # and then wrote no ledger, with no exception, no call.ledger_failed
        # and no traceback — and the logs could not say whether this block ran
        # at all. Since v21 suppressed the /ping access log, a container that
        # has frozen and one that is simply idle look identical from outside,
        # so "the process stopped here" is no longer inferable. This line is
        # the difference between those two, and it costs one log record.
        event("call.teardown", handed_to_portal=handed_to_portal,
              outcome=call_outcome, ava_stopped=ava_stopped)

        # Deliberately no `return` anywhere in this block: a return in a
        # `finally` replaces whatever the `try` was returning, which here would
        # silently discard the portal attach frame and drop the join.
        #
        # A connection handed on to the portal bridge is only just beginning:
        # the socket belongs to serve_portal_socket now, and the call it's
        # joining belongs to a different connection entirely. Ending that call
        # or closing that socket is exactly what must not happen — so the
        # caller teardown is skipped wholesale, and only the gateway client,
        # which really was this function's, is cleaned up.
        if handed_to_portal:
            # These two were started before the first frame revealed this
            # wasn't a call at all — the ordering that keeps a real caller's
            # gateway round trip off their critical path. Nobody wants their
            # results now.
            #
            # stop() is deliberately documented as safe on a partly-started
            # client, but it joins a background thread, and a human agent is
            # waiting on this socket to become their call — so it goes to a
            # worker thread with a deadline rather than blocking the event
            # loop for however long that join takes.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(mcp_client.stop, None, None, None), timeout=5)
            except Exception as cleanup_error:
                event("gateway.close_failed", error=str(cleanup_error))
        else:
            # First, before any of the teardown below, because everything it
            # reports is already settled: the outcome, the counters and the
            # transcript stop changing the moment the caller goes, and nothing
            # closed below can alter them. It used to run last, and that cost a
            # real call — 1a7d361b7d27 on 2026-09-11 hung in teardown and was
            # killed by the pool recycle before it wrote a line, so a call that
            # spent money and held a session appears in no metric at all.
            # Teardown is now bounded as well (see _bounded_teardown), but
            # ordering is the part that holds even if the process is killed
            # outright.
            #
            # Wrapped because a ledger line is never worth an exception
            # escaping a `finally` — that would replace the real outcome of
            # this connection with a traceback.
            #
            # Only this branch emits one. The `handed_to_portal` branch above
            # is a human agent joining someone else's call: it consumed no
            # model tokens and has no duration of its own, so a ledger row for
            # it would be a phantom call diluting every per-call average.
            try:
                escalated = live_call.escalation is not None
                if ava_disconnected:
                    outcome = "human_handoff"
                elif call_outcome == "completed" and escalated:
                    outcome = "escalated"
                else:
                    outcome = call_outcome
                counters = COUNTERS.get()
                emit_call_ledger(
                    usage=call_usage,
                    duration_seconds=time.monotonic() - call_started,
                    outcome=outcome,
                    tool_calls=counters.get("tool_calls", 0),
                    gate_blocks=counters.get("gate_blocks", 0),
                    escalated=escalated,
                    human_handoff=ava_disconnected,
                    tool_errors=counters.get("tool_errors", 0),
                    transcript_turns=len(live_call.transcript),
                    error=call_error,
                )
            except BaseException as ledger_error:
                # Deliberately wider than Exception: asyncio.CancelledError is
                # a BaseException, so a cancelled teardown would otherwise
                # lose the ledger exactly as silently as the hang this whole
                # block exists to survive. Cancellation is still re-raised —
                # reported, not swallowed.
                event("call.ledger_failed",
                      error=f"{type(ledger_error).__name__}: {ledger_error}")
                if isinstance(ledger_error, (asyncio.CancelledError,
                                             KeyboardInterrupt, SystemExit)):
                    raise

            # Tell any open portal the caller has gone, and keep the call readable
            # there for a while (handoff.RETENTION_SECONDS) so a human following a
            # Slack link a few minutes late still sees the conversation.
            await _bounded_teardown(
                live_call.mark_ended(), failed_event="handoff.end_call_failed", timeout=5)
            # No explicit memory write here: the session manager persists each turn
            # as it happens, so a call that drops mid-sentence keeps everything said
            # up to that point rather than losing it all at teardown.
            try:
                await websocket.close()
            except Exception:
                pass
            if voice_agent is not None and not ava_stopped:
                await _bounded_teardown(
                    voice_agent.stop(), failed_event="agent.stop_failed", timeout=10)
            await _bounded_teardown(
                asyncio.to_thread(mcp_client.stop, None, None, None),
                failed_event="gateway.close_failed", timeout=5)


if __name__ == "__main__":
    import uvicorn

    event(
        "server.starting",
        port=8080,
        model=MODEL_ID,
        bedrock_region=BEDROCK_REGION,
        gateway=GATEWAY_URL,
        gateway_region=GATEWAY_REGION,
    )

    host = "0.0.0.0" if os.getenv("CONTAINER_ENV") else "127.0.0.1"
    uvicorn.run(app, host=host, port=8080)
