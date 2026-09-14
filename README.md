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
- **`agentcore_app/handoff.py` + `portal.html`** — the human handoff: an
  escalation posts to Slack with a link to an agent portal, where a person
  reads the conversation and joins the live call, disconnecting Ava.
  See **Human handoff** below.
- **`frontend/index.html`** — a single self-contained page: a customer-facing
  support site (hero, what Ava can do, how it works, FAQ) whose every
  **Call Ava** button — nav, hero dial pad, closing CTA, and the floating
  button that follows you down the page — opens the same call panel. The panel
  asks for three things before the call — your full name, your date of birth
  and your registered mobile number — sent as one
  `{"type": "bidi_call_metadata", ...}` message right after the socket opens.
  The number is what Ava looks the policy up by; the name and date of birth
  are the pair the gateway's identity check takes on the path where a number
  alone isn't enough, so a caller who typed them never has to recite them out
  loud. All three are validated on both sides (`_ui_full_name` /
  `_ui_date_of_birth` in `strands_agent.py`) and reach the model as quoted
  facts, not as questions to ask. Worth being explicit that this makes the
  identity check satisfiable by whoever holds the device — the right shape for
  a signed-in app, the wrong one for an anonymous page.
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

With the pre-call details entered, Ava's first action on a call isn't speech — it's
the gateway's policy lookup for that number. Only once the result is back does
she speak, opening with the caller's first name from the policy and
introducing herself in the same breath ("Hello Priya, I'm Ava, your AI
Assistant. I can help with…"), then handling whatever they need.

Two details worth knowing if you change this:

- It's appended to the **system prompt** per call (`strands_agent.py`, right
  after the pre-call details are resolved — as is a second block naming the
  typed name and date of birth), not to the kickoff text. The base
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

### Human handoff (Slack → agent portal → live call)

When Ava escalates, a person can take over the call she's already on — the
caller stays on the same line and just starts hearing a human instead.

```
Ava escalates ──> Slack message (reason, summary, last lines, link)
                      │  human clicks
                      ▼
              /portal?call=…&token=…    ← full transcript + escalation reason
                      │  Join call
                      ▼
  caller browser <── audio ──> /ws <── audio ──> /portal/ws <── audio ──> human browser
                                 │
                                 └─ Nova Sonic disconnected the moment a human joins
```

- **`agentcore_app/handoff.py`** — the whole handoff: the per-call registry,
  the Slack post, and the portal's routes (`/portal`, `/portal/api/call/{id}`,
  `/portal/ws/{id}`), mounted on the same FastAPI app that serves `/ws`.
- **`agentcore_app/portal.html`** — the agent console. Opens on one call:
  why it escalated, Ava's summary, every field the escalate tool was called
  with, and the conversation so far, still updating live. The human **hears
  the call from the moment they open the link** — listening before taking over
  is the point — and **Join call** puts them through and disconnects Ava, with
  mute and leave. Leaving puts the caller back in the queue for another agent
  rather than back to Ava, since her session is gone by then.

  Two panels read the durable records rather than trusting only what this
  process is holding, both fetched after the page opens (so neither delays it)
  and re-fetchable with **Refresh**:

  - **Conversation** comes from **AgentCore Memory** (short-term/session) —
    the turns the agent itself persisted, keyed by the same actor id and
    session id `strands_agent.py` writes under. Memory is written
    asynchronously, so the newest turn or two usually isn't there yet: those
    come from the live stream and are tagged `live`, and the panel's header
    says which store it's showing. Memory unset or unreadable degrades to the
    live stream with a note, never an error page.
  - **Escalation record** is the row the gateway's escalate tool wrote to the
    **`escalations` DynamoDB table** (`ESCALATIONS_TABLE`), fetched by
    `escalationId` — status, `queueKey` priority, `createdAt`, TTL, captured
    slots, `assignedAgentId`. That's what the rest of the contact centre acts
    on, and it's rendered from whatever keys the row actually has, so a schema
    change on the table side needs no change here.
- **`_EscalationNotifierTool`** in `strands_agent.py` is what notices. Like
  every other wrapper there it hardcodes no tool name: a successful result
  carrying an escalation id *is* an escalation. Slack is posted from a
  background task, so the caller isn't left in silence waiting on an HTTP
  round trip.

> **Writing an `AgentTool` wrapper: act on the result *inside* the loop.**
> Strands' tool executor `return`s as soon as it sees a `ToolResultEvent`
> (`strands/tools/executors/_executor.py`), which abandons the wrapper's async
> generator — so anything written after the `async for` never runs. It fails
> silently: the tool still works, the model still gets its result, and only
> the wrapper's own effect goes missing. Every wrapper here therefore does its
> work on the `ToolResultEvent` before yielding it.

**Ava is disconnected, not muted**, and that takes two steps rather than one,
because they answer different questions: *how fast* and *how completely*.

*Immediately*, two functions in `_serve_caller` stop her being heard — not a
mode flag threaded through the app. `run()` pulls the caller's events in
through one and pushes Ava's out through the other, so the instant the call's
state flips to `human` the first sends the caller's audio to the portal
instead of to the model and the second drops Ava's audio and transcript. This
is what makes Join feel instant; it needs no teardown to have finished.

*Then* her session actually ends. `voice_agent.run()` is started as a task
alongside a `human_takeover` event, so whichever finishes first decides the
rest of the call: normally `run()` (the caller hung up), but on a handoff the
event, and then `_disconnect_ava()` stops Nova Sonic and — if `stop()` doesn't
end `run()` within the budget — cancels it outright. From that point there is
no model on the call at all: nothing listening to what a caller tells a
person, and nothing that could resume speaking over them. `AVA_DISCONNECT_TIMEOUT`
bounds the whole shutdown, and nobody goes deaf while it runs, because `run()`
relays the caller to the portal before consulting any handoff gate.

Ending `run()` used to be exactly what couldn't happen — it's what reads the
caller's socket, so its `finally` would tear the call down under the human who
just joined. `bridge_caller_to_human()` is what makes it safe: once Ava is
gone it takes over reading that socket and relays the caller's audio to the
portal for as long as they stay on the line (the human's mic already goes the
other way, straight to the caller's socket, from `relay_human_audio`). No
message is lost across the switch — `next_caller_event` holds its pending
receive in an enclosing variable, and `asyncio.wait` never cancels what it
waits on, so a frame that arrived mid-teardown is still there for the first
read.

The consequence worth knowing: **leaving no longer hands the call back to
Ava**, because there is nothing to hand it back to. The call returns to
`escalated` and the caller is told a specialist has left and another is
coming — `ai_available: false` on the `bidi_handoff` event is how the page
knows which of those two situations that state means.

**While Ava is on the call, she is fed silence, not nothing**
(`KEEPALIVE_SECONDS`). Nova Sonic ends a session after ~55s without audio,
with `Timed out waiting for audio bytes or interactive content`, and that
exception ends `run()` and drops the caller mid-call. Two ordinary things stop
the audio: the caller pressing mute (the browser then sends nothing at all),
and the caller's tab going to the background, where the throttled main thread
stops flushing mic frames — easy to trigger by opening the portal link in
another tab of the same browser. So whenever no real caller audio is arriving,
a 100ms frame of silence goes in its place: exactly what a live mic sends in a
quiet room. The receive task is held across those timeouts rather than
re-created, so a caller message arriving as one expires isn't lost. The bridge
after a handoff needs none of this — there's no session left to keep alive.

Configure it in `.env` (all optional — with no Slack token the portal link is
printed to the server log instead, which is enough to work locally):

```bash
SLACK_BOT_TOKEN=xoxb-…                            # needs chat:write
SLACK_ESCALATION_CHANNEL=#contact-center-escalations
PORTAL_BASE_URL=http://127.0.0.1:8080             # origin the Slack link points at
HANDOFF_RETENTION_SECONDS=1800                    # how long an ended call stays readable
HANDOFF_ADMIN_TOKEN=                              # unset = /portal/queue disabled
ESCALATIONS_TABLE=escalations                     # the queue table the portal reads
# MEMORY_ID / MEMORY_REGION are the same ones the agent already uses.
```

### What a finished call leaves behind

Every ending — the caller closing the tab, Ava calling `end_call`, a human
agent taking the call over, or the connection failing — goes through one
`finally` in `strands_agent.serve`, and writes two records there:

- **A ledger line** (`observability.py`): what the call *cost* and how it
  ended. Two log events, one of them CloudWatch EMF. Nothing to provision.
- **A case row** (`cases.py` → the `cases` DynamoDB table): what the caller
  *wanted* and whether they got it — `customerId`, `policyId`, `claimId`,
  `customerIntent`, `resolutionSummary`, `resolutionStatus` — so the contact
  centre can sort and report on calls instead of reading transcripts.

The case is written in two halves. The facts land first, as a single
`PutItem` right after the ledger, because a container killed mid-teardown must
still leave a row. Then, once the socket and the model session are closed and
nobody is waiting any more, Bedrock reads the transcript and fills in the three
judged fields. The identifiers come from what a **completed tool call actually
returned**, never from what the caller said or the model passed in — the same
provenance rule the grounding gate enforces during the call. The model owns
`resolutionStatus`; what that was derived from (`callOutcome`, `humanHandoff`,
and whether there is an `escalationId`) stays under names it never writes, so
the machinery's own version is always recoverable and comparable. Timestamps
are Sydney-local strings, as everywhere else here.

```bash
CASES_TABLE=cases                                 # one row per finished call
CASE_SUMMARY_MODEL_ID=us.amazon.nova-lite-v1:0    # empty = facts only, no summary
# CASES_REGION / CASE_SUMMARY_REGION default to AWS_REGION.
```

The table is created out of band like every other table here; the `create-table`
call, the two GSIs, the one IAM statement and the log query to check it are in
[agentcore_app/DEPLOY.md](agentcore_app/DEPLOY.md) under "The case record".
Without the IAM statement each call logs one `case.write_failed` and nothing
else changes.

**Where the link is.** An escalation puts it in two places: the Slack message,
and one line on the agent's stdout —

```
[handoff] escalation on call 7d3e9486460e — portal: http://127.0.0.1:8080/portal?call=…&token=…
```

Lose both and the call is unreachable, because the token can't be
reconstructed. So set `HANDOFF_ADMIN_TOKEN` to any secret string and
`/portal/queue?token=<that>` lists this instance's calls with a link into
each escalated one. It's off unless that variable is set — the page shows
every call's bearer token at once, which is strictly more exposure than a
single link.

Two limits to know before this fronts real callers:

- **Access is one per-escalation token**, minted when the call escalates and
  sent only to Slack. Both portal endpoints require it, and an unauthorized
  socket is refused before it's accepted. But anyone holding that Slack
  message — or the URL after someone forwards it — can listen to and speak
  with the caller. A real deployment wants an identity check in front of
  `/portal`.
- **State is in this process's memory**, which is the honest scope: one
  container. A portal link only resolves on the instance holding that call, so
  more than one instance behind the same endpoint needs sticky routing to it
  (or this state moved somewhere shared, with a relay both instances reach).
  Ended calls are kept `HANDOFF_RETENTION_SECONDS` so a late click still shows
  the conversation, then dropped; AgentCore Memory remains the durable record.
- **On AgentCore Runtime the portal needs its own way in.** Callers reach the
  runtime through `InvokeAgentRuntimeWithWebSocketStream` (a signed WebSocket
  invocation), not as an ordinary HTTP origin — so a plain `https://…/portal`
  GET isn't served by that path at all. The escalation reason, the queue row
  and the conversation don't need the container (see the Amplify section:
  [`lambda/portal_context_handler.py`](lambda/portal_context_handler.py) reads
  them from DynamoDB and AgentCore Memory), but **joining the live call does**,
  because the caller's socket is an object in that process's memory. For that,
  `PORTAL_BASE_URL` has to point at something that routes to the container: an
  ALB in front of port 8080, or a tunnel while developing. Locally, where
  `strands_agent.py` is just a uvicorn server, `http://127.0.0.1:8080` works
  as-is.

### Hosting the pages on AWS Amplify

[`amplify.yml`](amplify.yml) at the repo root builds both pages. There's no
build tooling — the only step is writing the `config.js` each page reads,
which `.env` supplies locally and Amplify environment variables supply here.

**Customer page** — set one variable and deploy:

```
CALL_ENDPOINT = https://<presign-lambda-url>/       # or wss://<...>
```

It must be `https://` or `wss://`: an Amplify page is served over HTTPS, and a
secure page can't open a `ws://` socket or `fetch` over `http://` — the
browser blocks it as mixed content and the call silently fails to connect. The
build fails early on a `ws://localhost` value rather than shipping a dead call
button. The presign Lambda also needs CORS for the Amplify origin, since the
page fetches it cross-origin. (Serving over HTTPS has a side benefit: it's a
secure context, so the microphone works — a `file://` page can't load the
AudioWorklet module in current Chrome.)

**Agent portal** — the portal needs two unrelated things, and only one of them
needs the agent container. Sorting that out is what makes it hostable at all:

| What the page shows | Where it comes from | Needs the container? |
| --- | --- | --- |
| Escalation reason, queue row, conversation | the escalations table + AgentCore Memory | **No** |
| Live call state and the audio bridge (Join) | the process holding the caller's socket | **Yes** |

The second row is the awkward one on AgentCore Runtime, because that container
has no browser-reachable origin: Runtime publishes one WebSocket path per
container behind a SigV4-signed endpoint, and picks the instance from the
session id in the signed URL. There is no host a portal could point at.

So the portal doesn't try. It asks the portal-context Lambda for a **presigned
socket onto the caller's own Runtime session** — the session id being exactly
what routes it into the container holding that call — and then names the call in
its first frame instead of in a URL path. Same bridge on the other side of that
handshake; see [Joining a Runtime-hosted call](#joining-a-runtime-hosted-call).

That makes `PORTAL_CONTEXT_URL` alone enough for a fully working portal, join
included. `PORTAL_API_BASE` remains for the case where the agent *does* have a
reachable origin (a local run, or ECS behind an ALB), and is then used directly:

```
# Amplify — one of these is enough
PORTAL_CONTEXT_URL     = https://<portal-context-lambda-url>   # records + a way in
PORTAL_API_BASE        = https://<agent-origin>                # only if the agent has one
# agent
PORTAL_ALLOWED_ORIGINS = https://<amplify-domain>      # CORS + WS origin check
PORTAL_BASE_URL        = https://<amplify-domain>      # so Slack links point here
```

With both unset the portal is skipped entirely, which is right when the agent
serves it itself at `GET /portal`. With either set, the page ships as
`dist/portal/index.html`, so the `/portal` path the Slack link already builds
resolves with no Amplify rewrite rule to maintain.

#### `PORTAL_CONTEXT_URL` — the records, and a way into the call

[`lambda/portal_context_handler.py`](lambda/portal_context_handler.py) behind a
Function URL. It reads the escalations row and AgentCore Memory directly, so it
answers whether or not a container is still holding the call — which also means
the portal keeps working after the call ends, where the agent's own registry
drops it after `HANDOFF_RETENTION_SECONDS`.

It also mints the presigned WebSocket URL the page joins with, when
`AGENT_RUNTIME_ARN` is set. Both jobs start from the same check, which is why
they're one endpoint rather than two.

It shares
[`agentcore_app/portal_context.py`](agentcore_app/portal_context.py) with the
container verbatim (`lambda/portal_context.py` is a symlink) so both parse
memory events the same way. Deploy it with:

```
ESCALATIONS_TABLE = escalations                    # same table the escalate tool writes
MEMORY_ID         = <the agent's memory id>
AGENT_RUNTIME_ARN = <the agent runtime's ARN>      # omit for a read-only portal
ALLOWED_ORIGIN    = https://<amplify-domain>       # defaults to "*"
```

Set `ALLOWED_ORIGIN`: with `AGENT_RUNTIME_ARN` configured this endpoint hands
out a credential, not just a transcript.

and an execution role with `dynamodb:GetItem` on the table,
`bedrock-agentcore:ListEvents` on the memory resource, and
`bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` on the runtime — that
last one only for the join URL, and specifically **not** `InvokeAgentRuntime`,
which covers `/invocations` and leaves a presigned `/ws` refused with a bare
`HTTP 403` at the handshake. Without it the records still work and the page says
why Join is unavailable. Package
`boto3>=1.40` with it (`lambda/portal_context_requirements.txt`) — the
runtime's bundled copy generally doesn't know the `bedrock-agentcore` client,
and without it the row still loads while the conversation comes back as a note.

Authorization is the same per-escalation token from the Slack link, and the
Lambda shares no secret with the container to check it: when a call escalates,
the agent stamps `sha256(token)` onto the escalations row along with the memory
session to read and the call's start time, and the Lambda compares against that
hash. So a leaked table row grants nothing, and the agent needs
**`dynamodb:UpdateItem`** on the table for the stamp (without it everything
else still works and only this hosted portal can't resolve the call — the log
says so, once per escalation).

#### `PORTAL_API_BASE` — the live call

Scheme + host (+ port) only, no path: the page appends `/portal/api/...` and
`/portal/ws/...` itself. It must be the origin of the `strands_agent.py`
process itself. What it can be:

| Where the agent runs | value |
| --- | --- |
| Your machine, through a tunnel (ngrok, Cloudflare Tunnel) | `https://a1b2c3.ngrok-free.app` |
| A container (ECS/Fargate/EC2) behind an **ALB** with an ACM cert | `https://agent.yourdomain.com` |
| CloudFront in front of that ALB | `https://agent.yourdomain.com` |

Not usable: `http://127.0.0.1:8080` (an HTTPS page can't call `http://` or
`ws://` — mixed content), a raw `*.elb.amazonaws.com` name (no cert), the
AgentCore Runtime ARN (a signed invocation, not an HTTP origin), or — the easy
mistake — **the presign API Gateway from `CALL_ENDPOINT`**. That one answers
every path with `{"url": …}` and permissive CORS, so the page's snapshot fetch
appears to succeed and used to render a call with "Invalid Date" and an empty
transcript. The page now validates the shape and names the misconfiguration
instead.

**An ALB, specifically** — not API Gateway. Its HTTP APIs don't proxy
WebSocket connections, and its WebSocket APIs use a route plus `@connections`
model rather than a transparent proxy, so the audio bridge would have to be
rewritten around it. An ALB passes WebSockets straight through.

With `PORTAL_CONTEXT_URL` set and the agent unreachable, the page renders the
records and disables Join with the reason, rather than failing whole — the
commonest cause is benign (the call ended and the agent released it).

#### Joining a Runtime-hosted call

Nothing to configure beyond `AGENT_RUNTIME_ARN` above, but it's worth knowing
what happens, because the shape is unusual and it's the only shape that can
work on Runtime:

```
portal page ──fetch──► portal-context Lambda   (checks sha256(token) on the row,
                                                reads portalRuntimeSessionId,
                                                signs a wss:// for that session)
            ──wss────► AgentCore Runtime ──► the container holding the call
            ──frame──► {"type":"portal_attach","call":…,"token":…}
```

The agent's `/ws` branches on that first frame: a caller starts a voice call, a
`portal_attach` becomes a portal bridge — the same one `/portal/ws/{call_id}`
serves for a directly reachable agent, so joining behaves identically either
way. Runtime accepts the handshake before the container has looked the call up,
so an open socket proves nothing; the page treats the first `call_state` as the
proof and otherwise says the call couldn't be found.

Two things have to be true, and the container's log says so once per call:

- **the container knows the Runtime session id.** Runtime forwards it on the
  handshake as `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`, and that's what
  is used; the caller's page also reports it, read out of the presigned URL it
  was handed, as a fallback. A call that logs `runtime session: unknown` will
  serve records and refuse joins.
- **the escalations row carries `portalRuntimeSessionId`**, which needs the
  agent role's `dynamodb:UpdateItem`.

The URL is a real credential while it lives: scoped to one session id, expiring
(`JOIN_URL_TTL_SECONDS`, default 15 minutes), minted only after the token check
passes, and never logged.

There is deliberately no `Origin` check on the agent's side of this path, and
adding one would break every join: Runtime terminates the browser's WebSocket
and opens its own to the container, carrying its own origin
(`https://cell01.<region>.prod.arp.kepler-analytics.aws.dev`). The browser's
origin is never visible there. `PORTAL_ALLOWED_ORIGINS` still applies to the
direct `/portal/ws/{call_id}` route, where the header really is the browser's.

And once the portal is internet-facing, put real authentication in front of it
(Amplify access control, Cognito, or an authenticated ALB rule); the
per-escalation token is a reasonable gate on localhost and a thin one on the
public internet.

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
Enter your full name, date of birth and registered mobile number, then click
**Call Ava** (any call button on the page opens the call panel).

### Run it after deploying

Once `agentcore_app/DEPLOY.md`'s steps are done, you'll have an Agent
Runtime ARN and a Lambda Function URL in front of it:

1. Set `CALL_ENDPOINT` in `.env` to your Lambda's Function URL (or API
   Gateway URL) — the one whose only job is minting a presigned WebSocket URL
   for your runtime ARN — then run `python frontend/make_config.py`.
2. Open `frontend/index.html` (same file, no separate build/hosting step
   required — or serve it, plus the generated `config.js`, from
   S3/CloudFront/any static host).
3. Enter the caller's name, date of birth and registered mobile number, then
   click **Call Ava**.

**Full deployment steps (ECR, IAM roles, `CreateAgentRuntime`, Lambda,
Function URL): see [`agentcore_app/DEPLOY.md`](agentcore_app/DEPLOY.md).**
