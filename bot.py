"""Ava — a speech-to-speech voice agent built on Pipecat + AWS Bedrock Nova Sonic.

Talks to you through your microphone and speakers, using Nova Sonic's native
speech-to-speech model (no separate STT/TTS steps). Ava's tools come from an
MCP server (mcp_server/server.py by default — swap MCP_SERVER_URL for a real
one any time).

The flow is intentionally agentic, not a scripted state machine: the system
prompt below tells Ava who she is, what her tools do, and when escalation
generally makes sense — but Ava decides, turn by turn, what to say and which
tool (if any) to call. The only deterministic logic lives inside the tools
themselves (see mcp_server/server.py), where it belongs: validating data,
not directing conversation.

Run:
    python bot.py

Requires mcp_server/server.py to already be running in another terminal.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger
from mcp.client.session_group import StreamableHttpParameters

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
from pipecat.services.mcp_service import MCPClient
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8787/mcp")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

SYSTEM_INSTRUCTION = """
You are Ava, the AI assistant for the contact center. You speak with callers
over voice, so keep every response short, natural, and conversational — a
sentence or two at a time. Never use lists, headings, or symbols that can't
be spoken aloud.

At the very start of the conversation, introduce yourself in one short
sentence, then let the caller respond:
"Hi, I'm Ava, your AI Assistant. I can help you with your policy details,
register a claim, or connect you with a human agent — how can I help?"

You have three tools. Decide for yourself, from what the caller actually
says, which one to use and when — there is no fixed script to follow.

1. get_policy_details(policy_number)
   Use this whenever the caller wants to know anything about their policy:
   coverage, premium, status, renewal date. Ask for their policy number if
   you don't already have it. Never guess or invent policy information —
   only state what the tool returns.

2. register_claim(policy_number, incident_date, incident_description, contact_phone)
   Use this when the caller wants to file a claim. Before calling it, make
   sure you have the policy number (look it up with get_policy_details first
   if you haven't already, so you know it's valid), what happened, when it
   happened, and a phone number to reach them. Read the details back to
   confirm before submitting. If the tool reports the policy wasn't found or
   isn't active, explain that clearly and offer to escalate to a human agent.

3. escalate_to_human(reason, conversation_summary, urgency, customer_name, policy_number, claim_id)
   Use this to hand the caller off to a human agent. Use your judgment —
   typical reasons include: the caller directly asks for a person, their
   request is outside policy lookups and claims, a tool keeps failing, or the
   caller sounds frustrated or unsatisfied after a couple of attempts. Always
   write conversation_summary yourself, in a few sentences, covering what the
   caller wants, what you've already found out (policy number, claim ID, key
   facts), and why you're escalating — so the human agent has full context
   and the caller never has to repeat themselves. Set urgency to low, normal,
   high, or urgent based on how you read the situation. After calling this
   tool, tell the caller — in your own words — that you're connecting them
   with a teammate who already has the full picture.

Never fabricate policy, claim, or account information. If you're unsure what
the caller means, ask a clarifying question rather than guessing.
""".strip()


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

    logger.info(f"Connecting to MCP server at {MCP_SERVER_URL} ...")
    mcp = MCPClient(server_params=StreamableHttpParameters(url=MCP_SERVER_URL))
    tools = await mcp.tools()
    logger.info("MCP tools loaded.")

    transport = LocalAudioTransport(
        LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True)
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
