# Ava — Voice Contact Center Agent

A speech-to-speech voice agent built on [Pipecat](https://pipecat.ai) and
AWS Bedrock Nova Sonic. Talk to Ava through your microphone; she answers with
real-time generated speech (no separate STT/TTS steps — Nova Sonic handles
both ends of the audio directly).

Ava can:
- Look up policy details
- Register an insurance claim
- Escalate to a human agent, with a full context summary handed off automatically

## How it's built

```
you (mic/speaker) ──── bot.py (Pipecat + Nova Sonic) ──── MCP tools ──── mcp_server/server.py (mock data)
```

- **bot.py** — the voice agent. Connects your local mic/speaker to AWS
  Bedrock's Nova Sonic model and gives it Ava's persona plus the three MCP tools.
- **mcp_server/server.py** — a small MCP server exposing
  `get_policy_details`, `register_claim`, and `escalate_to_human`, backed by
  in-memory mock data (`mcp_server/data.py`).

### Agentic, not scripted

There's no state machine deciding "if user says X, go to step Y." Ava's
system prompt (in `bot.py`) explains who she is, what each tool does, and
roughly when escalation makes sense — then the model decides, turn by turn,
what to say and which tool to call. That's what makes this agentic: the
flow can adapt to however the conversation actually goes.

The only things that are deterministic live inside the tools themselves
(`mcp_server/server.py`) — plain data validation like "does this policy
exist," "is it active enough to file a claim against," "is this urgency
value one of the four allowed values." Facts and rules are deterministic;
how to talk to the caller is agentic. Keeping that split clean is
deliberate: it's the part of the system that must always behave the same
way, so it isn't left to a model's judgment.

## Setup

**Requirements:**
- Python 3.12+ (the AWS Nova Sonic SDK requires it)
- An AWS account/IAM user with **Bedrock model access enabled for Nova Sonic**
  in your chosen region (enable it in the Bedrock console under "Model access")
- An AWS access key for that account (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`)

macOS also needs PortAudio for local microphone access:
```bash
brew install portaudio
```

Install dependencies:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configure your credentials:
```bash
cp .env.example .env
# then edit .env and fill in your AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
```

## Run it

Two terminals, both from the project root (with the venv activated):

```bash
# Terminal 1 — the mock MCP server (policy/claims tools)
python mcp_server/server.py

# Terminal 2 — the voice agent
python bot.py
```

Speak into your microphone once Ava introduces herself. Try things like:
- "What's the status of policy POL-10234?"
- "I want to file a claim on POL-55891, my car was hit yesterday."
- "Can I talk to a real person?"

When Ava escalates, the mock server prints a structured handoff block
(reason, urgency, what's known so far, conversation summary) to the terminal
running `mcp_server/server.py` — that's the "context" a human agent would
receive in a real system.

## Pointing at a real MCP server

`mcp_server/server.py` is a mock so the whole thing runs end-to-end without
any backend of your own. To use your real policy/claims MCP server instead,
just change `MCP_SERVER_URL` in `.env` to its address — nothing else in
`bot.py` needs to change, as long as your server exposes tools named
`get_policy_details`, `register_claim`, and `escalate_to_human` with the same
arguments (see the docstrings in `mcp_server/server.py` for the exact
signatures). Pipecat's `MCPClient` also supports stdio and SSE MCP servers,
not just streamable HTTP — ask if you want `bot.py` wired up for one of
those instead.

## Notes

- Claims and handoffs only live in memory for the lifetime of
  `mcp_server/server.py` — restarting it resets everything.
- The voice used is Nova Sonic's "tiffany" — change `voice=` in `bot.py` to
  `matthew`, `amy`, or another supported voice
  ([voice list](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html)).
- Nova Sonic (Nova 2 Sonic, the default model) is currently available in
  `us-east-1`, `us-west-2`, and `ap-northeast-1`.
- AWS sessions are capped at ~8 minutes; Pipecat's `session_continuation`
  (on by default) rotates the session in the background before that limit,
  with no audible interruption — nothing you need to configure.
