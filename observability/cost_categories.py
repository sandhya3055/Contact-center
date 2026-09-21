"""How a Cost Explorer line maps to one of this agent's cost categories.

Shared by `cost_report.py` (the on-demand terminal view) and
`cost_publisher.py` (the daily Lambda that turns the same numbers into
CloudWatch metrics). One definition, because two copies of a classifier
drift and then the dashboard and the report quietly disagree about what
anything costs.
"""

from __future__ import annotations

# Rates derived from this account's own bill (cost / UsageQuantity), used for
# per-session compute estimates. Every figure in the reports themselves is
# what AWS actually charged, not these.
VCPU_RATE, GB_RATE = 0.0895, 0.00945

# Bedrock model families that are not Ava's voice path — evals, judges,
# prompt tooling. Kept separate so experiment spend never reads as production.
OTHER_MODEL_HINTS = (
    "nova2.0lite", "novalite", "novapro", "llama4", "kimi", "gpt-oss",
    "nemotron", "glm", "minimax", "qwen", "apo-v1", "titan", "claude",
)

RUNTIME = "AgentCore Runtime (compute)"
MODEL_PROD = "Nova Sonic — production (us-west-2)"
MODEL_DEV = "Nova Sonic — dev/eval (us-east-1)"
MEMORY = "AgentCore Memory"
GATEWAY = "AgentCore Gateway"
OTHER_MODELS = "Other Bedrock models (evals, prompt tooling)"
ECR = "Container images (ECR)"
OBSERVABILITY = "Observability (CloudWatch, X-Ray)"
AMPLIFY = "Portal hosting (Amplify)"
GLUE = "Glue (Lambda, DynamoDB, API GW, S3)"

# (category, "what already covers it") in display order.
CATEGORIES = [
    (RUNTIME, "dashboard: compute widget"),
    (MODEL_PROD, "ledger: CallCostUsd"),
    (MODEL_DEV, "nothing"),
    (MEMORY, "nothing"),
    (GATEWAY, "nothing"),
    (OTHER_MODELS, "nothing"),
    (ECR, "nothing"),
    (OBSERVABILITY, "nothing"),
    (AMPLIFY, "nothing"),
    (GLUE, "nothing"),
]

# CloudWatch dimension values have to survive being typed into a query, so the
# display names above (em dashes, parentheses) get a flat slug for metrics.
METRIC_NAMES = {
    RUNTIME: "RuntimeCompute",
    MODEL_PROD: "ModelProduction",
    MODEL_DEV: "ModelDevEval",
    MEMORY: "AgentCoreMemory",
    GATEWAY: "AgentCoreGateway",
    OTHER_MODELS: "OtherBedrockModels",
    ECR: "ContainerImages",
    OBSERVABILITY: "Observability",
    AMPLIFY: "PortalHosting",
    GLUE: "Glue",
}


def classify(service: str, usage_type: str) -> str | None:
    """Bucket one CE line, or None if it belongs to another project.

    Order matters: the AgentCore `*:Consumption-based:*` usage types all sit
    under the same service, and the Nova Sonic check has to run before the
    generic "anything Bedrock" fallback or production spend lands in the
    evals bucket.
    """
    ut = usage_type.lower()
    svc = service.lower()

    if "runtime:consumption-based" in ut:
        return RUNTIME
    if "memory:consumption-based" in ut:
        return MEMORY
    if "gateway:consumption-based" in ut:
        return GATEWAY
    if "novasonic" in ut:
        # Region decides production vs dev: the deployed runtime calls
        # us-west-2 (BEDROCK_REGION), while local runs and evals use
        # us-east-1 via the repo's .env. If BEDROCK_REGION ever moves, these
        # two categories swap meaning — that is the one assumption here.
        return MODEL_DEV if ut.startswith("use1") else MODEL_PROD
    if any(h in ut for h in OTHER_MODEL_HINTS):
        return OTHER_MODELS
    if "bedrock" in svc:
        return OTHER_MODELS
    if "container registry" in svc or svc.endswith("ecr)"):
        return ECR
    if "cloudwatch" in svc or "x-ray" in svc:
        return OBSERVABILITY
    if "amplify" in svc:
        return AMPLIFY
    if svc in ("aws lambda", "amazon dynamodb", "amazon api gateway",
               "amazon simple storage service"):
        return GLUE
    return None
