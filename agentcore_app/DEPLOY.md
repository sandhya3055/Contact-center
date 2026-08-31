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
    \"MODEL_ID\": \"amazon.nova-2-sonic-v1:0\"
  }"
```

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

To redeploy after a code change: rebuild + push the image (step 1), then
`aws bedrock-agentcore-control update-agent-runtime` with the same
`--agent-runtime-artifact`.

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
