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

## Notes

- Nothing in `bot.py` hardcodes a gateway tool's name, parameters, or
  description — the system prompt and tool wiring are both fully dynamic,
  driven by whatever the gateway currently exposes (`bot.py` logs the live
  tool list at startup). The one exception, `session_id` on the escalate
  tool, is also detected generically (by checking which tool's schema
  requires a `session_id`, not by a hardcoded tool-name constant) — see
  `_instrument_tools` in `bot.py`. Recreating the gateway/target needs zero
  code changes here.
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

## Browser UI (AWS Bedrock AgentCore Runtime + Lambda)

`bot.py` is a local terminal app — talking to Ava from a browser instead
needs a long-running server to hold the Nova Sonic session open for the
call's duration, which plain Lambda can't do (its invocations are
short-lived and event-driven). Three new pieces cover this:

```
browser ── fetch ──> Lambda (presigned_url_handler.py) ── returns a signed wss:// URL ──> browser
browser ── connects directly with that URL ──> agentcore_app/strands_agent.py (AWS Bedrock AgentCore Runtime)
                                                          │
                                                          └── AgentCore Gateway (same MCP_SERVER_URL)
```

- **`agentcore_app/`** — the actual voice agent, hosted on **AWS Bedrock
  AgentCore Runtime** (not Lambda — it's a long-running WebSocket container,
  the AWS-native way to host exactly this kind of session). Same Ava
  persona and the same gateway tools as `bot.py`, but built on the
  [Strands Agents SDK](https://strandsagents.com)'s `BidiAgent` +
  `BidiNovaSonicModel` instead of Pipecat, since that's what AgentCore
  Runtime's WebSocket contract (a container serving `/ws` and `/ping` on
  port 8080) is designed around. Talks to the gateway directly via
  `mcp-proxy-for-aws`'s `aws_iam_streamablehttp_client` — no subprocess
  proxy needed here, unlike `bot.py`, since Strands' MCP client accepts an
  arbitrary async transport factory. Just like `bot.py`, every tool is
  loaded live from the gateway with nothing hardcoded — the one
  `session_id` exception is detected generically too (whichever tool's
  schema requires it gets one injected and hidden from the model), so
  changing the gateway's tools needs no code changes here either.
  See [`agentcore_app/DEPLOY.md`](agentcore_app/DEPLOY.md) for the full
  deployment steps — you'll get back an agent runtime ARN
  (`arn:aws:bedrock-agentcore:<region>:<account>:runtime/<name>-<id>`).
- **`lambda/presigned_url_handler.py`** — the piece you asked for. Its only
  job is minting a short-lived, SigV4-presigned WebSocket URL for that
  runtime ARN, using the `bedrock-agentcore` Python SDK's
  `AgentCoreRuntimeClient.generate_presigned_url()` — it never touches
  audio. Needs `AGENT_RUNTIME_ARN` as an environment variable, and its
  execution role needs `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`
  on that ARN. Package `bedrock-agentcore` (see `lambda/requirements.txt`)
  into the deployment zip or a layer — it isn't in the base Lambda runtime.
- **`frontend/index.html`** — a single self-contained page: a customer-facing
  support site (hero, what Ava can do, how it works, FAQ) whose every
  **Call Ava** button — nav, hero dial pad, closing CTA, and the floating
  button that follows you down the page — opens the same call panel. The panel
  asks for one thing before the call: your registered mobile number, sent as a
  `{"type": "bidi_call_metadata", ...}` message right after the socket opens.
  That's what lets Ava look your policy up *before* she speaks and open with
  your name (see the opening sequence below), so unlike previous versions it's
  required, not optional. Where the call goes isn't asked at all — it comes
  from `CALL_ENDPOINT` in `.env` via `frontend/config.js` (see
  **Configuring the endpoint**). Hit **Call
  Ava** and it fetches the presigned URL (skipped for a `ws://`/`wss://`
  endpoint — see below), opens the WebSocket, streams mic audio as base64
  PCM16/16kHz (`{"type": "bidi_audio_input", ...}`), and plays back Ava's
  audio (`{"type": "bidi_audio_stream", ...}`). During the call you get a
  live scrollable transcript, a call timer, an avatar that pulses with Ava's
  speech and a level meter for your own mic (both fed by `AnalyserNode`s),
  plus two controls: **Mute** — which drops captured audio instead of sending
  it, so nothing recorded while muted is flushed on unmute — and **End call**.
  No build step — open it directly, or serve it from anywhere static.

### Configuring the endpoint

The page has no connection-settings UI: which endpoint a call goes to is
deployment config, not something a caller should type in. It lives in `.env`
with everything else:

```bash
# .env — a presign Lambda Function URL when deployed, or this when running
# strands_agent.py locally:
CALL_ENDPOINT=ws://127.0.0.1:8080/ws
```

`index.html` is opened straight from disk, and a `file://` page can't read
`.env` (or `fetch()` anything beside it), so one small generator bridges the
gap — run it after any change to `CALL_ENDPOINT`:

```bash
python frontend/make_config.py   # writes frontend/config.js from .env
```

That writes `window.AVA_CONFIG.callEndpoint` into `frontend/config.js`, which
the page loads. `config.js` is generated and gitignored; until it exists the
call button is disabled and the panel says which command to run.

### The opening sequence (lookup, then greet by name)

With a mobile number entered, Ava's first action on a call isn't speech — it's
the gateway's policy lookup for that number. Only once the result is back does
she speak, opening with the caller's first name from the policy and
introducing herself in the same breath ("Hello Priya, I'm Ava, your AI
Assistant. I can help with…"), then handling whatever they need.

Two details worth knowing if you change this:

- It's appended to the **system prompt** per call (`strands_agent.py`, right
  after the mobile number is resolved), not to the kickoff text. The base
  prompt scripts an exact opening line, and a user-role nudge doesn't reliably
  beat that — system-level text saying which of the two openings applies does.
  It also means this keeps working when the prompt comes from Bedrock Prompt
  Management (`BEDROCK_PROMPT_ID`), with no console edit needed to match.
- No tool is named in it, in keeping with the rest of this project: the
  addendum describes the lookup by what it does and lets the model pick from
  whatever the gateway currently exposes.

If the lookup finds no policy, or no usable name, Ava opens with the same line
minus the name — she never guesses one. A caller without a number never
reaches this path at all (the UI requires one), but the backend still handles
it the old way if a different client connects without metadata.

### Run it locally (no AWS deploy needed)

Runs `strands_agent.py` on your machine and points the browser UI straight
at it, skipping the Lambda/presign step entirely:

```bash
cd agentcore_app
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# reuse the same .env values as bot.py (MCP_SERVER_URL, AWS_REGION, AWS_ACCESS_KEY_ID, ...)
python strands_agent.py
```

Point the page at it and open it:

```bash
# in .env: CALL_ENDPOINT=ws://127.0.0.1:8080/ws
python frontend/make_config.py
open frontend/index.html
```

The frontend detects a `ws://`/`wss://` endpoint and connects to it
directly instead of treating it as a presign Lambda to `fetch()` first.
Enter your registered mobile number and click **Call Ava** (any call button
on the page opens the call panel).

### Run it after deploying

Once `agentcore_app/DEPLOY.md`'s steps are done, you'll have an Agent
Runtime ARN and a Lambda Function URL in front of it:

1. Set `CALL_ENDPOINT` in `.env` to your Lambda's Function URL (or API
   Gateway URL) — the one whose only job is minting a presigned WebSocket URL
   for your runtime ARN — then run `python frontend/make_config.py`.
2. Open `frontend/index.html` (same file, no separate build/hosting step
   required — or serve it, plus the generated `config.js`, from
   S3/CloudFront/any static host).
3. Enter the caller's registered mobile number, then click **Call Ava**.

**Full deployment steps (ECR, IAM roles, `CreateAgentRuntime`, Lambda,
Function URL): see [`agentcore_app/DEPLOY.md`](agentcore_app/DEPLOY.md).**
