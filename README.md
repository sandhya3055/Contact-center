# Ava — Voice Contact Center Agent

A speech-to-speech voice agent built on [Pipecat](https://pipecat.ai) and
AWS Bedrock Nova Sonic. Talk to Ava through your microphone; she answers with
real-time generated speech (no separate STT/TTS steps — Nova Sonic handles
both ends of the audio directly). Her tools come from a live AWS Bedrock
AgentCore Gateway.

Ava can:
- Look up a caller's policy by mobile number (this also doubles as her identity check)
- Register an insurance claim (cashless or refund)
- Escalate to a human agent, with context handed off automatically

## How it's built

```
you (mic/speaker) ── bot.py (Pipecat + Nova Sonic) ── mcp-proxy-for-aws (stdio, signs requests) ── AgentCore Gateway (MCP_SERVER_URL)
```

- **bot.py** — the voice agent. Connects your local mic/speaker to AWS
  Bedrock's Nova Sonic model and gives it Ava's persona plus whatever tools
  the gateway exposes. Your mic input runs through
  [RNNoise](https://github.com/xiph/rnnoise) noise suppression before it
  reaches Nova Sonic — runs locally, no API key needed.
- **The gateway** (`MCP_SERVER_URL`) is an AWS Bedrock AgentCore Gateway —
  a SigV4/IAM-protected MCP endpoint, not an open one. Pipecat's `MCPClient`
  only speaks plain HTTP with static headers, no request signing, so
  `bot.py` spawns AWS's own [`mcp-proxy-for-aws`](https://pypi.org/project/mcp-proxy-for-aws/)
  as a local stdio subprocess in front of it — the proxy signs every
  request with your local AWS credentials (from `.env`), and `MCPClient`
  just talks stdio to it like any other local MCP server.
- **mcp_server/** — the original local mock server (mock policy/claims data,
  no AWS account needed) from before this project pointed at a real
  gateway. Still here for offline testing; not used by `bot.py` by default
  anymore. To go back to it: point `MCP_SERVER_URL` at
  `http://127.0.0.1:8787/mcp` and swap `bot.py`'s `StdioServerParameters`
  connection back to a plain `StreamableHttpParameters(url=MCP_SERVER_URL)`.

### Agentic, not scripted

There's no state machine deciding "if user says X, go to step Y." Ava's
system prompt (in `bot.py`) explains who she is, what each tool does, and
roughly when escalation makes sense — then the model decides, turn by turn,
what to say and which tool to call. That's what makes this agentic: the
flow can adapt to however the conversation actually goes.

The things that are deterministic live in the tools themselves, on the
gateway's backend (outside this project) — does this policy exist, is it
active, does this vehicle already have an open claim. `bot.py` adds exactly
one deterministic override on top: `session_id` on the escalate tool is
auto-injected (see `CALL_SESSION_ID`) rather than left for the model to
invent, since it's plumbing, not a judgment call. Everything else — when to
ask for a mobile number, how to react to no policy found, how to summarize
a conversation for a human agent — is agentic.

## Setup

**Requirements:**
- Python 3.12+ (the AWS Nova Sonic SDK requires it)
- An AWS account/IAM user with **Bedrock model access enabled for Nova Sonic**
  in your chosen region (enable it in the Bedrock console under "Model access")
- An AWS access key (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) belonging
  to a principal your AgentCore Gateway's IAM policy allows to invoke it

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
# then edit .env: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION /
# MCP_SERVER_URL (your gateway's MCP endpoint)
```

## Run it

```bash
python bot.py
```

On startup, `bot.py` logs every tool it discovered from the gateway (name +
description) — check that log line if anything about the tool set has
changed since this was last updated (e.g. you recreated the gateway/target).

Once Ava introduces herself, give her a mobile number in E.164 format
(e.g. "plus one four one five five five five one two three four") — that's
both how she looks up your policy and how she treats you as verified.

⚠️ **Known issue as of this writing:** `get_policy` currently fails for
every caller with `AccessDeniedException: ... not authorized to perform:
dynamodb:Query on resource: .../policies/index/mobileNo-index`. This is an
IAM permissions gap on the gateway's Lambda execution role
(`insurarncae-voice-agent-mcp-tool-role-t6n9accl`), not a bug in this
project — that role needs a `dynamodb:Query` permission added for the
`mobileNo-index` GSI on the `policies` table before Ava can look anyone up.

## Notes

- `ESCALATE_TOOL_NAME` in `bot.py` hardcodes the gateway's current tool name
  for escalation (`target-quick-start-t58h6f___escalate`) so `session_id`
  can be injected automatically. If you recreate the gateway/target, this
  name will change — update the constant to match (check the startup tool
  list log for the new name).
- The voice used is Nova Sonic's "tiffany" — change `voice=` in `bot.py` to
  `matthew`, `amy`, or another supported voice
  ([voice list](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html)).
- Nova Sonic (Nova 2 Sonic, the default model) is currently available in
  `us-east-1`, `us-west-2`, and `ap-northeast-1`.
- AWS sessions are capped at ~8 minutes; Pipecat's `session_continuation`
  (on by default) rotates the session in the background before that limit,
  with no audible interruption — nothing you need to configure.
- Noise suppression only applies to your mic input (there's no background
  noise to remove from Ava's synthesized speech). To turn it off, remove
  `audio_in_filter=RNNoiseFilter()` in `bot.py`.
- Wear headphones or earbuds. With open speakers, your mic picks up Ava's
  own voice and Nova Sonic transcribes that as new caller speech — you'll
  see "You:" lines that are really just Ava's last sentence echoing back.
  Headphones remove that acoustic path entirely.
