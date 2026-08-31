"""Lambda: issues a short-lived, SigV4-presigned WebSocket URL for the Ava
AgentCore Runtime agent. This Lambda never touches audio — its only job is
signing a URL the browser can then connect directly to.

Flow:
    browser -> (calls this Lambda) -> presigned wss:// URL
    browser -> connects directly with that URL -> agentcore_app/strands_agent.py

Required execution role permission:
    bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream
    on resource: AGENT_RUNTIME_ARN

Required environment variable:
    AGENT_RUNTIME_ARN — the ARN printed after deploying agentcore_app
    (looks like arn:aws:bedrock-agentcore:<region>:<account>:runtime/<name>-<id>)

Optional environment variables:
    AWS_REGION            — defaults to the Lambda's own region
    URL_EXPIRES_SECONDS   — presigned URL validity window (default 300 = 5 min)

Package `bedrock-agentcore` (see requirements.txt) into the deployment
zip/layer — it isn't part of the default Lambda Python runtime.
"""

import json
import os
import uuid

from bedrock_agentcore.runtime import AgentCoreRuntimeClient

REGION = os.environ.get("AWS_REGION", "us-west-2")
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
URL_EXPIRES_SECONDS = int(os.environ.get("URL_EXPIRES_SECONDS", "300"))

_client = AgentCoreRuntimeClient(region=REGION)

# Tighten this to your actual frontend origin before going anywhere near production.
_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def handler(event, context):
    """Lambda entry point. Works behind a Function URL or API Gateway (HTTP or REST)."""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS_HEADERS, "body": ""}

    session_id = str(uuid.uuid4())

    url = _client.generate_presigned_url(
        runtime_arn=AGENT_RUNTIME_ARN,
        session_id=session_id,
        expires=URL_EXPIRES_SECONDS,
    )

    return {
        "statusCode": 200,
        "headers": _CORS_HEADERS,
        "body": json.dumps({"url": url, "sessionId": session_id}),
    }
