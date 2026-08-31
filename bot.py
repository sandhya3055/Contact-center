"""Ava — a speech-to-speech voice agent built on Pipecat + AWS Bedrock Nova Sonic.

Talks to you through your microphone and speakers, using Nova Sonic's native
speech-to-speech model (no separate STT/TTS steps). Ava's tools come from an
AWS Bedrock AgentCore Gateway (MCP_SERVER_URL) — a SigV4/IAM-protected MCP
endpoint, reached through AWS's own mcp-proxy-for-aws spawned as a stdio
subprocess (see main() below). mcp_server/ still has the original local mock
server (no AWS account needed) if you want to test offline — point
MCP_SERVER_URL at it and swap the connection code back to StreamableHttpParameters.

The flow is intentionally agentic, not a scripted state machine: the system
prompt below tells Ava who she is and how to behave — but Ava decides, turn
by turn, what to say and which tool to call, learning what each tool does
and needs from that tool's own live description (nothing about a tool's
name, parameters, or description is hardcoded here, so changing the
gateway's tools needs no code changes in this file). The only deterministic
logic lives in the tools themselves (on the gateway's backend, outside this
project) plus one exception this file does add: any tool that requires a
session_id gets one generated fresh per call injected automatically, since
that's plumbing the model has no business inventing — detected generically
by inspecting each tool's schema, not by hardcoding which tool needs it (see
_instrument_tools below).

Run:
    python bot.py
"""

import asyncio
import json
import os
import sys
import uuid

from dotenv import load_dotenv
from loguru import logger
from mcp import StdioServerParameters

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnMessageAddedMessage,
)
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
from pipecat.services.mcp_service import MCPClient
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8787/mcp")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# One id per bot.py run, stands in for a real call/session id since there's
# no telephony system here. Auto-injected into whichever tool requires a
# session_id (see _instrument_tools) — Ava never sees or has to invent this.
CALL_SESSION_ID = str(uuid.uuid4())

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


def _instrument_tools(tools: ToolsSchema) -> ToolsSchema:
    """Wrap every MCP tool handler: log its call/result, fix up the result
    type, and auto-inject a session_id into whichever tool needs one.

    Nothing here is tied to a specific tool's name — a tool is detected as
    needing a session_id generically, by checking whether its own live
    schema requires one, not by a hardcoded tool-name constant. So if the
    gateway adds, removes, or reshapes tools, this keeps working unchanged.

    Three things happen here, all at the same boundary:

    1. Logging — print which tool Ava called, with what arguments, and what
       it returned, so the terminal shows exactly what's happening instead
       of a black box.
    2. JSON parsing — Pipecat's MCP client always hands back a tool's result
       as a JSON-text string (that's just how MCP content works). AWS Nova
       Sonic only JSON-encodes a tool result if it's already a Python dict —
       a string gets sent through as-is, which AWS then rejects
       ("Unsupported JSON type in Tool Result. Please provide the Tool
       Result as a JSON object"). Parsing the string back into an object
       here, once, fixes it for every tool.
    3. session_id injection — hidden from the schema the LLM sees (removed
       from both properties and required) and merged into the arguments
       right before the call, for any tool whose gateway-advertised schema
       lists session_id as required.
    """

    def with_instrumentation(schema, original_handler):
        needs_session_id = "session_id" in schema.required

        async def wrapped(params):
            if needs_session_id:
                params.arguments = {**params.arguments, "session_id": CALL_SESSION_ID}
            logger.info(f"[tool call] {params.function_name}({params.arguments})")
            original_callback = params.result_callback

            async def parsing_callback(result, **kwargs):
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        pass
                logger.info(f"[tool result] {params.function_name} -> {result}")
                await original_callback(result, **kwargs)

            params.result_callback = parsing_callback
            await original_handler(params)

        return wrapped

    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name=schema.name,
                description=schema.description,
                properties={k: v for k, v in schema.properties.items() if k != "session_id"},
                required=[r for r in schema.required if r != "session_id"],
                handler=with_instrumentation(schema, schema.handler),
            )
            for schema in tools.standard_tools
        ]
    )


async def main():
    missing = [
        var
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        if not os.getenv(var)
    ]
    if missing:
        logger.error(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

    logger.info(f"Connecting to MCP gateway at {MCP_SERVER_URL} (via mcp-proxy-for-aws) ...")
    # MCP_SERVER_URL is a SigV4-protected AWS Bedrock AgentCore Gateway endpoint,
    # not an open MCP server — Pipecat's MCPClient only speaks plain
    # StreamableHttpParameters (no request signing), so we spawn AWS's own
    # mcp-proxy-for-aws as a local stdio subprocess in front of it. The proxy
    # signs every request with local AWS credentials; MCPClient just talks
    # stdio to the proxy like it would to any other local MCP server.
    aws_env = {
        key: os.environ[key]
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION")
        if os.getenv(key)
    }
    mcp = MCPClient(
        server_params=StdioServerParameters(
            command="mcp-proxy-for-aws",
            args=[MCP_SERVER_URL, "--service", "bedrock-agentcore", "--region", AWS_REGION],
            env=aws_env,
        ),
    )
    tools = _instrument_tools(await mcp.tools())
    logger.info(
        "MCP tools loaded: "
        + ", ".join(f"{t.name} — {t.description.strip().splitlines()[0]}" for t in tools.standard_tools)
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Suppresses background/mic noise from the caller's audio before
            # it reaches Nova Sonic — runs locally, no API key needed.
            audio_in_filter=RNNoiseFilter(),
        )
    )

    llm = AWSNovaSonicLLMService(
        access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        session_token=os.getenv("AWS_SESSION_TOKEN"),
        region=AWS_REGION,
        settings=AWSNovaSonicLLMService.Settings(
            system_instruction=SYSTEM_INSTRUCTION,
            voice="tiffany",  # matthew, tiffany, amy, and others — see AWS Nova Sonic voice docs
        ),
        tools=tools,
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            llm,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )

    runner = WorkerRunner(handle_sigint=False if sys.platform == "win32" else True)
    await runner.add_workers(worker)

    @user_aggregator.event_handler("on_user_turn_message_added")
    async def on_user_turn_message_added(aggregator, message: UserTurnMessageAddedMessage):
        logger.info(f"You: {message.content}")

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        logger.info(f"Ava: {message.content}")

    async def kick_off():
        # Local mic/speaker audio has no "client connected" event to hook,
        # so nudge Ava to open with her introduction shortly after startup.
        await asyncio.sleep(1)
        context.add_message(
            {"role": "developer", "content": "Greet the caller now with your introduction."}
        )
        await worker.queue_frames([LLMRunFrame()])

    logger.info("Ava is listening. Speak into your microphone. Press Ctrl+C to stop.")
    await asyncio.gather(runner.run(), kick_off())


if __name__ == "__main__":
    asyncio.run(main())
