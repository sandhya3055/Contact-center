# Deploying Ava to AWS Bedrock AgentCore Runtime + Lambda

This is the setup path for the browser UI (`frontend/index.html` →
`lambda/presigned_url_handler.py` → `agentcore_app/strands_agent.py`). It's
separate from `bot.py`'s local terminal setup — do this once you're ready to
try the browser version.

Everything below was verified directly against this AWS account: the exact
`CreateAgentRuntime` API shape (via `boto3`), and the working configuration
from AWS's own reference sample
([`sample-nova-sonic-websocket-agentcore`](https://github.com/aws-samples/sample-nova-sonic-websocket-agentcore)),
not guessed from docs alone. Skip the `agentcore` CLI's `create`/`deploy`
workflow — it scaffolds its own generated project structure and doesn't
have a `--protocol WebSocket` option, so it doesn't cleanly fit an existing
hand-written Docker container like this one. The steps below talk to AWS
directly instead.

## 0. Prerequisites

- Docker installed and running, with `buildx` (for the ARM64 build —
  AgentCore Runtime only runs ARM64 images)
- Your AWS credentials (the same ones in `.env`) have permission to: create
  an ECR repository, push images, create/pass an IAM role, and call
  `bedrock-agentcore:CreateAgentRuntime`
- Same region as your AgentCore Gateway (`us-west-2` in `.env`'s
  `AWS_REGION`, unless you're deploying the runtime somewhere else)

## 1. Build and push the container image

```bash
cd agentcore_app

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2
REPO=ava-voice-agent

aws ecr create-repository --repository-name "$REPO" --region "$REGION"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

docker buildx build --platform linux/arm64 \
  -t "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:latest" \
  --push .
```

## 2. Create the execution role

The runtime needs one IAM role covering: pulling the image from ECR,
invoking Nova Sonic, writing logs, and calling your AgentCore Gateway.

```bash
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name ava-agentcore-runtime-role \
  --assume-role-policy-document file://trust-policy.json

cat > permissions-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRPull",
      "Effect": "Allow",
      "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "*"
    },
    {
      "Sid": "NovaSonic",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModelWithBidirectionalStream", "bedrock:InvokeModel"],
      "Resource": "*"
    },
    {
      "Sid": "InvokeGateway",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "arn:aws:bedrock-agentcore:us-west-2:128486226972:gateway/gateway-quick-start-ad8a4f-evcjac2ebc"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name ava-agentcore-runtime-role \
  --policy-name ava-agentcore-runtime-permissions \
  --policy-document file://permissions-policy.json
```

If the deployed agent later fails to call the gateway or Nova Sonic, the
CloudWatch log group `/aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT`
will show the exact missing action — same pattern we used to fix the
gateway's own DynamoDB permissions earlier; just add the missing action to
`permissions-policy.json` and re-run `put-role-policy`.

## 3. Create the AgentCore Runtime

```bash
ROLE_ARN=$(aws iam get-role --role-name ava-agentcore-runtime-role --query 'Role.Arn' --output text)

aws bedrock-agentcore-control create-agent-runtime \
  --region us-west-2 \
  --agent-runtime-name ava_voice_agent \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:latest\"}}" \
  --role-arn "$ROLE_ARN" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"HTTP"}' \
  --environment-variables "{
    \"MCP_SERVER_URL\": \"$(grep ^MCP_SERVER_URL ../.env | cut -d= -f2-)\",
    \"AWS_REGION\": \"us-west-2\",
    \"BEDROCK_REGION\": \"us-east-1\",
    \"MODEL_ID\": \"amazon.nova-2-sonic-v1:0\",
    \"MEMORY_ID\": \"$(grep ^MEMORY_ID ../.env | cut -d= -f2-)\",
    \"ESCALATIONS_TABLE\": \"escalations\",
    \"PORTAL_BASE_URL\": \"https://<portal-origin>\",
    \"PORTAL_ALLOWED_ORIGINS\": \"https://<portal-origin>\"
  }"
```

**`.env` is not in the container.** The Dockerfile copies only the `.py` and
`.html` files, and `python-dotenv` finds nothing at runtime — so every setting
the agent needs has to be listed here. This is worth stating plainly because
the failure is silent and confusing: with `MEMORY_ID` missing the agent runs
perfectly and simply never persists a conversation, so the portal's transcript
is empty for reasons that look like a portal bug. Same for `SLACK_BOT_TOKEN`
(escalations reach the log but never Slack) and `PORTAL_BASE_URL` (Slack links
point at `http://127.0.0.1:8080`). Anything you set in `.env` for a local run,
set here too, or accept that the deployed agent doesn't have it.

`serverProtocol: HTTP` is correct here — per AWS's own reference sample,
"HTTP includes both REST (`/invocations`) and WebSocket (`/ws`) endpoints."
There's no separate `WEBSOCKET` protocol value.

`BEDROCK_REGION` is set to `us-east-1` above (Nova 2 Sonic's default
region) independently of `AWS_REGION` (your gateway's region, `us-west-2`)
— they don't have to match; `strands_agent.py` already reads them as
separate variables.

The command's output includes `agentRuntimeArn` — save it:

```bash
export AGENT_RUNTIME_ARN="<value from agentRuntimeArn above>"
```

You can re-check it any time with:
```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2
```

### Redeploying after a code change

The runtime pins its image **by digest**, not by tag — `get-agent-runtime`
shows `...ava-voice-agent@sha256:...`. So pushing over a tag deploys nothing;
tag versions exist to identify a build, and the update call is what deploys it.
Use a new version each time (`v9`, `v10`, … is the convention in this repo)
rather than `latest`, so a rollback is a digest you can still name.

```bash
TAG=v13   # next unused version
docker buildx build --platform linux/arm64 --provenance=false \
  -t "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG" --push .
```

`--provenance=false` matters: with attestations on, buildx pushes an image
*index* and the tag's digest is the index's, not the arm64 manifest's. Then
read the digest and update, passing the full spec — `update-agent-runtime`
replaces rather than merges, so anything you omit (role, network, protocol,
environment variables) is dropped:

```bash
DIGEST=$(aws ecr describe-images --repository-name "$REPO" --region "$REGION" \
  --image-ids imageTag=$TAG --query 'imageDetails[0].imageDigest' --output text)
```

Then call `update-agent-runtime` with `containerUri` set to `<repo>@$DIGEST`
plus the same `--role-arn`, `--network-configuration`,
`--protocol-configuration` and `--environment-variables` the runtime already
has (read them back with `get-agent-runtime` first). Poll `status` until
`READY`, then confirm the container came up:

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region us-west-2 --since 5m --format short | grep -i "startup\|error\|memory\]"
```

`Application startup complete` plus `[memory] session manager ready` on the
first call is what a good deploy looks like.

### Observability (tool calls in the trace)

Out of the box a trace contains exactly one span, `AgentCore.Runtime.Invoke`
— the platform's own. Getting the orchestration flow (`bidi_session`, and an
`execute_tool` child per tool call carrying its arguments) needs three things:

1. **`AGENT_OBSERVABILITY_ENABLED=true`** in the runtime's environment
   variables — this is what `_setup_telemetry()` in `strands_agent.py` gates
   on. Also set `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`
   (puts tool arguments in the queryable `gen_ai.tool.call.arguments`
   attribute) and `OTEL_SERVICE_NAME=ava-voice-agent`.
2. **`xray:PutTraceSegments`** on the execution role (the `Observability`
   statement in `permissions-policy.json`).
3. **Transaction Search enabled** in CloudWatch. Check with
   `aws xray get-trace-segment-destination --region us-west-2`; it must
   report `"Destination": "CloudWatchLogs", "Status": "ACTIVE"`.

**How the SDK gets configured.** The container's `CMD` runs under
`opentelemetry-instrument` (with `aws-opentelemetry-distro` in
`requirements.txt`), which is AWS's documented entry point for this. It sets
the SDK up before uvicorn imports the app, and its auto-instrumentation adds
the spans the agent's own code never emitted: the FastAPI request, the
botocore calls to Bedrock and DynamoDB, the gateway's MCP round trips.

`_setup_telemetry()` is still there and still needed — it detects that the
wrapper already configured a provider and leaves it alone, rather than adding
a second exporter and shipping every span twice. It's what configures tracing
for every other way this runs: `python strands_agent.py`, a plain `uvicorn`
command, an image built before the wrapper. Same destination either way.

**The lock moved to make this installable, and both downgrades are load-bearing.**
`aws-opentelemetry-distro==0.19.0` (the newest release) pins its whole
dependency set exactly, so `requirements.lock.txt` now holds
`protobuf==6.33.5` (was `7.36.1`) and `cachetools==6.2.4` (was `7.1.8`).
Installing the distro without moving them is `ResolutionImpossible`. Both are
safe against the rest of the graph — every `protobuf` requirer here accepts
`>=6.33.5,<8.0.0`, and `py-key-value-aio` is the only package that wants
`cachetools` at all (`>=5.0.0`) — and `pip check` is clean on the result. If
you ever regenerate the lock and the distro drops out, the `CMD` has to go
back to plain `uvicorn` in the same change, or tracing dies silently (see
below).

**You must also fix `OTEL_EXPORTER_OTLP_ENDPOINT` on the runtime.** This is
the one setting that fails silently, and this runtime has it set to
`http://localhost:4318` — harmless while tracing was in-process, live breakage
under the wrapper. AgentCore Runtime does not run an OTLP collector inside a
custom container, so every batch gets `Connection refused` and the spans are
dropped after being correctly created. Either unset it, so the distro applies
its own AgentCore default, or set it to CloudWatch's OTLP endpoint,
`https://xray.<region>.amazonaws.com/v1/traces`, which needs SigV4 signing —
the distro does that itself. A runtime's environment variables override the
image's `ENV`, so this can only be fixed on the runtime, not in the
Dockerfile. To drop it, pass the full `environmentVariables` map back minus
that key on the next `update-agent-runtime` (the call replaces rather than
merges, so omitting a key is how you delete it).

**Why the failure mode is silence.** `opentelemetry-instrument` ships with
`opentelemetry-instrumentation`, which was already in the lock, so the wrapper
exists and the container starts normally whether or not the distro is
installed and whether or not the endpoint is sane. Miss either and you get a
healthy `/ping`, a working call, and no spans.

Spans land in the `aws/spans` log group. To confirm delivery without opening
the console:

```bash
aws logs filter-log-events --region us-west-2 --log-group-name aws/spans \
  --start-time $(python3 -c "import time;print(int((time.time()-900)*1000))") \
  --filter-pattern '"ava-voice-agent"' --max-items 5 --query 'events[].message' --output text
```

Container-side problems show up in the runtime log group — grep it for
`[telemetry]` (setup outcome) and `export span batch` (delivery failures).

### Structured logs, and the per-call cost ledger

`observability.py` is the second half of the picture: traces tell you what
happened inside one call, this tells you which call, what it consumed, and
what it cost. Everything it writes goes to stdout, which AgentCore already
ships to the runtime log group — there is no second exporter and no new
endpoint to configure.

Three things it adds, and why each one was missing:

1. **JSON log lines with a correlation key.** Every line carries `call_id`,
   `session_id`, `trace_id` and `xray_trace_id`. `session_id` is the
   AgentCore Runtime session — the *same* value AgentCore stamps on every
   record in `/aws/vendedlogs/.../USAGE_LOGS/<agent>`, which is where the
   call's billed vCPU-hours and GB-hours are. That single shared key is what
   turns eight unrelated log groups into one queryable story.
2. **Per-modality token counts.** Nova Sonic reports speech and text tokens
   separately and bills them at very different rates; Strands'
   `_convert_nova_event` keeps only the two totals and drops the breakdown
   before anything downstream can see it. `_UsageCapturingNovaSonic` in
   `strands_agent.py` overrides that one method to tap the raw event on its
   way past. This is a real coupling to `strands-agents==1.54.0` — if a
   version bump removes the method, the container fails at import rather
   than quietly costing nothing.
3. **A ledger line per finished call, as CloudWatch EMF.** CloudWatch parses
   `_aws` out of any log group and publishes the fields as real metrics, so
   `Ava/Calls` gets `CallCostUsd`, `CallDurationSeconds`, `InputTokens`,
   `OutputTokens`, `ToolCalls`, `GateBlocks` and the four per-modality
   counts with no `PutMetricData` call — no API throttling on a busy
   container. `outcome` (`completed` / `caller_disconnected` / `escalated` /
   `human_handoff` / `error`) is a metric dimension, so "escalated calls cost
   3× and there are more this week" is a dashboard line rather than a query.

**Set `NOVA_SONIC_PRICES_JSON` or there is no cost.** USD per 1M tokens,
per modality:

```
NOVA_SONIC_PRICES_JSON={"speech_in":X,"text_in":X,"speech_out":X,"text_out":X}
```

It deliberately has no default. Unset, every token count is still recorded
and the ledger says `cost_priced: false`, and **no** `CallCostUsd` metric is
published — publishing a $0 would read as "calls are cheap" rather than
"cost is not configured", which is the worse failure. Prefer holding the
value in SSM Parameter Store and injecting it at deploy time, so a price
change isn't a redeploy.

**Verify the token field names on the first deploy.** The exact keys inside
`usageEvent.details` are the one thing here never confirmed against live
traffic — Strands discards that sub-object, and DEBUG logging has never been
on in this container, so no sample of it exists anywhere in CloudWatch.
`CallUsage.update()` reads it defensively (an unexpected shape degrades to
"totals only, no cost", never to an exception in the model's receive loop),
and the first usage event of each process is logged verbatim as
`nova.usage.raw`. After the next deploy, make one call and read it:

```bash
aws logs filter-log-events --region us-west-2 \
  --log-group-name /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --start-time $(python3 -c "import time;print(int((time.time()-900)*1000))") \
  --filter-pattern '"nova.usage.raw"' --query 'events[0].message' --output text
```

If `modality_split_available` is `false` on the ledger lines, that payload is
what to tighten `update()` against.

**Cost per call = model + compute.** The ledger covers Nova Sonic tokens. The
runtime's own compute is in the usage logs, joinable on `session_id`:

```
fields @timestamp, attributes.session.id as session_id,
       metrics.`agent.runtime.vcpu.hours.used` as vcpu_h,
       metrics.`agent.runtime.memory.gb_hours.used` as gb_h
| stats sum(vcpu_h) as vcpu, sum(gb_h) as gb by session_id
```

Both are estimates. Reconcile monthly against Cost Explorer — which needs
`ce:GetCostAndUsage` granted to whoever checks, and a `Project=ava-voice-agent`
cost allocation tag activated in Billing. More than ~10% drift means the price
table or the modality mapping is wrong.

### Bedrock model invocation logging is per-region

Nova Sonic runs in `BEDROCK_REGION` (`us-east-1`), not `AWS_REGION`
(`us-west-2`) — so invocation logging configured in us-west-2 captures none of
this agent's model traffic. It is now enabled in **both**; us-east-1 needed its
own delivery role (`bedrock-model-logging-us-east-1`), because the us-west-2
one is hard-scoped to that region in both its trust policy's `aws:SourceArn`
and its permissions policy's log-group ARN.

Note that bidirectional-streaming coverage in invocation logs is thinner than
for `InvokeModel`, so treat an empty `model-invocation-logs` group in us-east-1
as expected rather than broken — `observability.py`'s in-process capture is the
authoritative source either way. Also note this group receives caller
transcripts: names, dates of birth, policy numbers. It carries a 30-day
retention for that reason.

## 4. Deploy the Lambda

```bash
cd ../lambda

pip install -r requirements.txt -t package
cp presigned_url_handler.py package/
cd package && zip -r ../lambda.zip . && cd ..

aws iam create-role \
  --role-name ava-presign-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }'

aws iam attach-role-policy \
  --role-name ava-presign-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam put-role-policy \
  --role-name ava-presign-lambda-role \
  --policy-name invoke-agent-runtime \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream\",
      \"Resource\": \"$AGENT_RUNTIME_ARN\"
    }]
  }"

LAMBDA_ROLE_ARN=$(aws iam get-role --role-name ava-presign-lambda-role --query 'Role.Arn' --output text)

# IAM role propagation can take ~10s the first time; retry create-function if it fails.
aws lambda create-function \
  --function-name ava-presigned-url \
  --runtime python3.12 \
  --handler presigned_url_handler.handler \
  --role "$LAMBDA_ROLE_ARN" \
  --zip-file fileb://lambda.zip \
  --timeout 10 \
  --environment "Variables={AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN,AWS_REGION=us-west-2,ALLOWED_ORIGIN=*}"

# Function URL — simplest way for the browser to call this without API Gateway.
# AllowOrigins here and ALLOWED_ORIGIN above both default to "*" for local
# testing; set both to your actual frontend origin (e.g. https://ava.example.com)
# before this is anything but a demo — see presigned_url_handler.py's docstring.
aws lambda create-function-url-config \
  --function-name ava-presigned-url \
  --auth-type NONE \
  --cors '{"AllowOrigins":["*"],"AllowMethods":["GET"]}'

aws lambda add-permission \
  --function-name ava-presigned-url \
  --statement-id AllowPublicFunctionUrl \
  --action lambda:InvokeFunctionUrl \
  --principal "*" \
  --function-url-auth-type NONE
```

`--auth-type NONE` + wide-open CORS is fine for testing; tighten
`AllowOrigins` to your actual frontend domain (and consider `AWS_IAM` auth
type with a Cognito-fronted proxy) before this is anything but a demo.

Get the Function URL:
```bash
aws lambda get-function-url-config --function-name ava-presigned-url --query FunctionUrl --output text
```

## 5. Open the frontend

Open `frontend/index.html` (double-click it, or serve it from any static
host), paste the Function URL from step 4 into the endpoint field, and
click **Talk to Ava**.

## 6. The agent portal's records and its way into a call (Lambda + IAM)

Needed only if the portal is hosted off the container (Amplify). See the
README's Amplify section for why the portal has two independent sources.

This one Lambda covers both of the portal's needs, because both start from the
same check — the per-escalation token's hash on the escalations row:

- **the records** (the reason, the queue row, the conversation) it serves
  itself, from DynamoDB and AgentCore Memory;
- **a way into the live call** it can't serve, but it can sign one: a presigned
  WebSocket URL onto the agent runtime, for the Runtime session the caller's own
  connection arrived on. Session id is what AgentCore routes on, so that URL is
  what puts the human agent's browser in the container holding the call. Set
  `AGENT_RUNTIME_ARN` to enable it; leave it unset and the portal is read-only
  and says so on the page.

**The Lambda.** `lambda/portal_context.py` is a symlink to the container's copy
and `zip -r` follows it, so there's one file to keep correct:

```bash
cd lambda
rm -rf build && mkdir build
python3.12 -m pip install --only-binary :all: -r portal_context_requirements.txt -t build
cp portal_context_handler.py portal_context.py build/
(cd build && find . -name '__pycache__' -type d -exec rm -rf {} + ; zip -qr ../portal_context.zip .)

aws lambda create-function --function-name ava-portal-context \
  --runtime python3.12 --handler portal_context_handler.handler \
  --role arn:aws:iam::<account>:role/ava-portal-context-lambda-role \
  --zip-file fileb://portal_context.zip --timeout 20 --memory-size 512 \
  --environment 'Variables={ESCALATIONS_TABLE=escalations,MEMORY_ID=<memory-id>,\
AGENT_RUNTIME_ARN=<runtime-arn>,ALLOWED_ORIGIN=https://<portal-origin>}' \
  --region us-west-2
```

Set `ALLOWED_ORIGIN` rather than leaving it at `*`: this endpoint now hands out
a credential, not just a transcript.

Its role needs `AWSLambdaBasicExecutionRole` plus `dynamodb:GetItem` on the
escalations table, `bedrock-agentcore:ListEvents` on the memory resource (and
the memory ARN with `/*`), and — only for the join URL — this:

```json
{"Sid":"PresignAJoinOntoTheAgentRuntime","Effect":"Allow",
 "Action":"bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
 "Resource":["arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>",
             "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>/*"]}
```

**Not `InvokeAgentRuntime`.** That action authorizes `/invocations`; the
WebSocket path is a separate action, and a URL presigned with only
`InvokeAgentRuntime` is refused with a bare `HTTP 403` at the handshake — which
reads as a signing problem, not a missing permission. Without the right grant
the records still work and the portal shows why Join is unavailable.

**A front door.** A public Lambda Function URL is the obvious choice and may be
blocked: an org guardrail can make one 403 at request time even with a correct
`Principal: "*"` policy and `AuthType: NONE`. Test with `curl` before believing
it works. If it 403s, front the function with an API Gateway HTTP API — a
Lambda proxy integration on `ANY /`, `$default` stage with auto-deploy, and
configure CORS **on the API**:

```bash
aws apigatewayv2 update-api --api-id <id> --region us-west-2 \
  --cors-configuration AllowOrigins=https://<portal-origin>,AllowMethods=GET,OPTIONS,AllowHeaders=content-type
```

An HTTP API strips `Access-Control-*` out of an integration's response, so the
handler's own headers never reach the browser and the fetch fails as a CORS
error with the Lambda returning 200. `PORTAL_CONTEXT_URL` is then the invoke
URL.

**The agent's role** needs two additions for any of this to work, and the
second one is what makes AgentCore Memory work at all:

```json
{"Sid":"PortalEscalationRow","Effect":"Allow",
 "Action":["dynamodb:GetItem","dynamodb:UpdateItem"],
 "Resource":"arn:aws:dynamodb:<region>:<account>:table/escalations"},
{"Sid":"AgentCoreMemorySession","Effect":"Allow",
 "Action":["bedrock-agentcore:CreateEvent","bedrock-agentcore:ListEvents"],
 "Resource":["arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>",
             "arn:aws:bedrock-agentcore:<region>:<account>:memory/<memory-id>/*"]}
```

`UpdateItem` is how the agent stamps a call's portal keys onto the escalations
row — the memory session to read, a hash of the portal token, and the Runtime
session id a join has to be signed for — so a portal outside the container can
find, authorize and reach the call. Without it everything else still works and
only that hosted portal can't resolve a call — one log line per escalation says
so.

**Joining a Runtime-hosted call.** Runtime publishes one WebSocket path per
container (`/ws`) and picks the instance from the session id in the signed URL,
so there is no URL that could address the portal's own route. The portal
therefore presigns `/ws` with the *caller's* session id and names the call in
its first frame (`{"type":"portal_attach","call":…,"token":…}`); the agent
branches on that frame and hands the connection to the same bridge the direct
route uses. Nothing to configure — but two things have to be true, and both are
reported in the container's log per call (`runtime session: …`, or `unknown`,
which serves records and refuses joins):

- the container has to learn the Runtime session id. Runtime forwards it on the
  handshake as `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`, and that's the
  source used — confirmed on the deployed runtime by a client that sent no
  messages at all and was still logged with the right id. `frontend/index.html`
  also reports it, read out of the presigned URL it was handed, as a fallback
  for if that ever stops being true.
- the escalations row must carry `portalRuntimeSessionId`, which needs the
  `UpdateItem` above.

One thing worth knowing before adding an origin check to that path: Runtime
terminates the browser's WebSocket and opens its own to the container, carrying
its own `Origin` (`https://cell01.<region>.prod.arp.kepler-analytics.aws.dev`).
The browser's origin is never visible there, so checking it refuses every
legitimate join and protects nothing. `PORTAL_ALLOWED_ORIGINS` still governs
the direct `/portal/ws/{call_id}` route, where the header is the browser's;
on this path the presigned URL, the token, and the Lambda's CORS are the gate.

## Local testing before any of this

You can run `strands_agent.py` locally and skip Lambda/AgentCore Runtime
entirely for a first check:

```bash
cd agentcore_app
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# reuse the same values as bot.py's .env: MCP_SERVER_URL, AWS_REGION,
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, BEDROCK_REGION
python strands_agent.py
```

Then in `frontend/index.html`, temporarily change the connect logic to skip
the presign fetch and connect straight to `ws://127.0.0.1:8080/ws` — this
tells you whether the agent itself works before adding AWS deployment,
Lambda, and SigV4 signing into the mix.
