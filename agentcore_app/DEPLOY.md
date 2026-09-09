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

Do **not** set `OTEL_EXPORTER_OTLP_ENDPOINT`. AgentCore Runtime does not run
an OTLP collector inside a custom container — pointing the exporter at the
conventional `localhost:4318` gets `Connection refused` on every batch, and
the spans are silently dropped after being correctly created. Traces go
straight to CloudWatch's own OTLP endpoint,
`https://xray.<region>.amazonaws.com/v1/traces`, which `_setup_telemetry()`
builds itself and SigV4-signs with botocore. No ADOT distro and no
`opentelemetry-instrument` wrapper: the OTel SDK, the OTLP exporter and the
signer are already installed.

Spans land in the `aws/spans` log group. To confirm delivery without opening
the console:

```bash
aws logs filter-log-events --region us-west-2 --log-group-name aws/spans \
  --start-time $(python3 -c "import time;print(int((time.time()-900)*1000))") \
  --filter-pattern '"ava-voice-agent"' --max-items 5 --query 'events[].message' --output text
```

Container-side problems show up in the runtime log group — grep it for
`[telemetry]` (setup outcome) and `export span batch` (delivery failures).

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
the memory ARN with `/*`), and — only for the join URL —
`bedrock-agentcore:InvokeAgentRuntime` on the runtime:

```json
{"Sid":"PresignAJoinOntoTheAgentRuntime","Effect":"Allow",
 "Action":"bedrock-agentcore:InvokeAgentRuntime",
 "Resource":["arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>",
             "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>/*"]}
```

Without it the records still work; the portal shows why Join is unavailable.

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
reported in the container's log per call:

- the caller's page must report its session id, which it reads out of the
  presigned URL it was handed (`frontend/index.html`). A container that logs
  `runtime session: unknown` will serve records and refuse joins.
- the escalations row must carry `portalRuntimeSessionId`, which needs the
  `UpdateItem` above.

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
