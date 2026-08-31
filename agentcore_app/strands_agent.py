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

Local dev:
    python strands_agent.py
    # then, from another terminal:
    python -m websockets ws://127.0.0.1:8080/ws   (or see frontend/index.html)

Deploy: see DEPLOY.md.
"""

import asyncio
import os
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.tools import stop_conversation
from strands.tools.mcp.mcp_client import MCPClient
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

GATEWAY_URL = os.environ.get("MCP_SERVER_URL")
GATEWAY_REGION = os.environ.get("AWS_REGION", "us-west-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", GATEWAY_REGION)
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-2-sonic-v1:0")
INPUT_SAMPLE_RATE = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
OUTPUT_SAMPLE_RATE = int(os.getenv("OUTPUT_SAMPLE_RATE", "16000"))

SYSTEM_INSTRUCTION = """
You are Ava, the AI assistant for the contact center. You speak with callers
over voice, so keep every response short, natural, and conversational — a
sentence or two at a time. Never use lists, headings, or symbols that can't
be spoken aloud.

At the very start of the conversation, introduce yourself in one short
sentence, then ask for the caller's mobile number:
"Hi, I'm Ava, your AI Assistant. I can help you with your policy details,
register a claim, or connect you with a human agent. First, could I get
your mobile number?"

Your tools come from the gateway, and each one's own description tells you
exactly what it does and what it needs — read those descriptions, don't
assume. In general terms, you can look up a caller's policy by mobile
number, register a claim against a verified policy, and escalate to a human
agent. Decide for yourself, from what the caller actually says, which tool
to use and when — there is no fixed script to follow.

Looking up a policy by mobile number doubles as your identity check —
there's no separate verification step or date of birth needed: a caller who
confirms a mobile number matching an active policy is treated as verified
for that policy. Long digit sequences spoken one at a time are easy to
mishear (a trailing digit especially can get dropped), so read the number
back digit by digit and get an explicit yes before calling the lookup tool,
the same way you'd confirm any other critical detail (a claim's incident
date, a vehicle registration) before submitting it. If the caller doesn't
clearly say yes but instead corrects you or restates the number, that
restatement is itself unconfirmed — read the new version back and ask again,
and keep looping until you get an explicit yes. Never call the lookup tool
on a number you haven't had confirmed back to you. Look up the policy before
discussing policy details or accepting a claim, and don't ask the caller to
re-verify later in the same call. If no policy is found, double-check the
number was heard correctly before telling the caller. If a caller has more
than one policy or vehicle, ask which one the call is about before
continuing.

Any identifier you say aloud — a policy number, claim number, vehicle
registration, or similar code — should be spoken one character at a time
(e.g. "three two three one four", not "three thousand two hundred and
thirty-one"), the same way you already do for mobile numbers. This applies
whether you're reading one back to confirm it or stating one to the caller.

When registering a claim, use the policy id and vehicle details from the
lookup — don't take a policy id the caller just states blind. Read the
details back to confirm before submitting.

Use your judgment on when to escalate to a human agent: the caller directly
asks for a person, their request is outside what your tools can help with,
a tool keeps failing, or the caller sounds frustrated or unsatisfied after
a couple of attempts. When you do, summarize what the caller wants and what
you've already found out so the human agent has full context and the
caller never has to repeat themselves. After escalating, tell the caller —
in your own words — that you're connecting them with a teammate who has the
full picture.

Never fabricate policy, claim, or account information. If you're unsure what
the caller means, ask a clarifying question rather than guessing.
""".strip()


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
        print(f"[tool call] {tool_use['name']}({tool_use.get('input')})")
        result = None
        async for event in self._wrapped.stream(tool_use, invocation_state, **kwargs):
            result = event
            yield event
        print(f"[tool result] {tool_use['name']} -> {result}")


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


async def build_gateway_tools(mcp_client: MCPClient) -> list[AgentTool]:
    """Load every tool the gateway currently exposes, unmodified.

    No tool name, parameter list, or description is hardcoded here — change
    what the gateway exposes, and this picks it up automatically next call.
    The single exception: any tool that requires a session_id gets one
    generated fresh for this call injected in (see _FixedArgumentTool),
    since that's plumbing, not something the model should invent.
    """
    call_session_id = str(uuid.uuid4())
    all_tools = await mcp_client.load_tools()
    print(f"[gateway] tools discovered: {[t.tool_name for t in all_tools]}")

    wrapped: list[AgentTool] = []
    for tool in all_tools:
        if _requires_argument(tool, "session_id"):
            tool = _FixedArgumentTool(tool, session_id=call_session_id)
        wrapped.append(_LoggingTool(tool))

    return wrapped


sonic_model = BidiNovaSonicModel(
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

app = FastAPI()


@app.get("/ping")
async def ping():
    """Health check endpoint required by AgentCore Runtime."""
    return {"status": "Healthy", "time_of_last_update": int(datetime.now().timestamp())}


@app.websocket("/ws")
async def voice_chat(websocket: WebSocket) -> None:
    """WebSocket endpoint for one bidirectional voice call.

    A fresh BidiAgent and gateway connection are created per call, so
    concurrent callers never share state (session_id, tool results, etc).
    """
    mcp_client = MCPClient(
        transport_callable=lambda: aws_iam_streamablehttp_client(
            endpoint=GATEWAY_URL,
            aws_region=GATEWAY_REGION,
            aws_service="bedrock-agentcore",
        )
    )
    voice_agent: BidiAgent | None = None

    # build_gateway_tools() -> load_tools() starts mcp_client's own
    # background connection lazily; wrapping this in `with mcp_client:`
    # too would start it twice and raise. mcp_client.stop() below is the
    # equivalent cleanup, called directly instead of via __exit__.
    try:
        # Load gateway tools concurrently with accepting the socket instead
        # of sequentially — the gateway round-trip otherwise added its full
        # latency on top of accept() before the caller's mic could even
        # start streaming.
        tools_task = asyncio.create_task(build_gateway_tools(mcp_client))
        await websocket.accept()
        print("WebSocket connection accepted")

        tools = await tools_task
        voice_agent = BidiAgent(
            model=sonic_model,
            tools=[*tools, stop_conversation],
            system_prompt=SYSTEM_INSTRUCTION,
        )

        # Nova Sonic only speaks in response to a turn it detects — without
        # this nudge Ava sits silently until the caller says something first,
        # even though the system prompt tells her to open with a greeting.
        # The very first "input" fed into run() is this synthetic text event
        # instead of a real websocket message; every call after that is the
        # real websocket.receive_json(). It's framed as a caller line only
        # because BidiAgent.send() always marks text input role=USER — the
        # system prompt's own directive to greet first takes priority over
        # reacting to this literal text.
        greeted = False

        async def receive_with_greeting_kickoff():
            nonlocal greeted
            if not greeted:
                greeted = True
                return {"type": "bidi_text_input", "text": "(the call has just connected — begin now)"}
            return await websocket.receive_json()

        async def log_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type == "bidi_transcript_stream" and event.get("is_final"):
                who = "You" if event.get("role") == "user" else "Ava"
                text = event.get("current_transcript") or event.get("text")
                if text and "the call has just connected" not in text:
                    print(f"{who}: {text}")
            elif event_type == "bidi_interruption":
                print(f"[interruption] {event.get('reason')}")

        await voice_agent.run(
            inputs=[receive_with_greeting_kickoff],
            outputs=[websocket.send_json, log_event],
        )

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Error in voice chat: {e}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        if voice_agent is not None:
            try:
                await voice_agent.stop()
            except Exception as cleanup_error:
                print(f"Error stopping voice agent: {cleanup_error}")
        try:
            mcp_client.stop(None, None, None)
        except Exception as cleanup_error:
            print(f"Error closing gateway connection: {cleanup_error}")


if __name__ == "__main__":
    import uvicorn

    print("Starting Ava voice agent server on port 8080...")
    print(f"Model: {MODEL_ID} (Bedrock region: {BEDROCK_REGION})")
    print(f"Gateway: {GATEWAY_URL} (signing region: {GATEWAY_REGION})")

    host = "0.0.0.0" if os.getenv("CONTAINER_ENV") else "127.0.0.1"
    uvicorn.run(app, host=host, port=8080)
