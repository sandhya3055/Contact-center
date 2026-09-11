#!/usr/bin/env bash
# Create or update the daily cost publisher: the Lambda that turns Cost
# Explorer categories into CloudWatch `Ava/Cost` metrics so they can appear
# on a dashboard at all.
#
# Idempotent — safe to re-run. Creates what's missing, updates what exists.
#
# The IAM role, the Lambda and the code update all work with the permissions
# this repo is normally driven with. **The schedule does not**: events:PutRule
# and scheduler:CreateSchedule are both denied, so the last step will fail
# until someone grants them. Everything before it still applies, and the
# function can be invoked by hand in the meantime:
#
#   aws lambda invoke --region us-west-2 --function-name ava-cost-publisher \
#     --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
set -euo pipefail

REGION="${REGION:-us-west-2}"
ACCOUNT="${ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
FUNCTION="${FUNCTION:-ava-cost-publisher}"
ROLE="${ROLE:-ava-cost-publisher-role}"
RULE="${RULE:-${FUNCTION}-daily}"
# 06:30 UTC: after Cost Explorer's overnight settle, before anyone looks.
SCHEDULE="${SCHEDULE:-cron(30 6 * * ? *)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> role"
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "    $ROLE exists"
else
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --description "Daily Cost Explorer -> CloudWatch Ava/Cost metrics" >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "    created $ROLE"
  sleep 10   # IAM propagation, or the first create-function races it
fi

# PutMetricData is scoped by namespace condition: this role should never be
# able to write anywhere but Ava/Cost, whatever the code does.
aws iam put-role-policy --role-name "$ROLE" --policy-name ava-cost-publisher --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"CostExplorer","Effect":"Allow","Action":["ce:GetCostAndUsage"],"Resource":"*"},
    {"Sid":"PublishMetrics","Effect":"Allow","Action":["cloudwatch:PutMetricData"],"Resource":"*",
     "Condition":{"StringEquals":{"cloudwatch:namespace":"Ava/Cost"}}},
    {"Sid":"EcrFootprint","Effect":"Allow","Action":["ecr:DescribeImages","ecr:DescribeRepositories"],"Resource":"*"}
  ]}'
echo "    policy applied"

echo "==> package"
ZIP="$(mktemp -d)/cost_publisher.zip"
# Only the two modules; boto3 is in the Lambda runtime already.
(cd "$HERE" && zip -q "$ZIP" cost_publisher.py cost_categories.py)
echo "    $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') files"

echo "==> function"
if aws lambda get-function --region "$REGION" --function-name "$FUNCTION" >/dev/null 2>&1; then
  aws lambda update-function-code --region "$REGION" --function-name "$FUNCTION" \
    --zip-file "fileb://$ZIP" >/dev/null
  aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION"
  echo "    code updated"
else
  aws lambda create-function --region "$REGION" --function-name "$FUNCTION" \
    --runtime python3.13 --architectures arm64 \
    --role "arn:aws:iam::${ACCOUNT}:role/${ROLE}" \
    --handler cost_publisher.lambda_handler --timeout 120 --memory-size 256 \
    --zip-file "fileb://$ZIP" \
    --description "Daily: Cost Explorer by category -> CloudWatch Ava/Cost" >/dev/null
  aws lambda wait function-active --region "$REGION" --function-name "$FUNCTION"
  echo "    created"
fi

echo "==> smoke test"
aws lambda invoke --region "$REGION" --function-name "$FUNCTION" --payload '{}' \
  --cli-binary-format raw-in-base64-out /tmp/cost_publisher_out.json \
  --query '{status:StatusCode,error:FunctionError}' --output json
cat /tmp/cost_publisher_out.json; echo

echo "==> schedule (needs events:PutRule — denied for the usual IAM user)"
aws events put-rule --region "$REGION" --name "$RULE" \
  --schedule-expression "$SCHEDULE" \
  --description "Daily: publish Cost Explorer categories into CloudWatch Ava/Cost" >/dev/null
aws lambda add-permission --region "$REGION" --function-name "$FUNCTION" \
  --statement-id "$RULE" --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT}:rule/${RULE}" >/dev/null 2>&1 || true
aws events put-targets --region "$REGION" --rule "$RULE" \
  --targets "Id=1,Arn=arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FUNCTION}" >/dev/null
echo "    scheduled: $SCHEDULE"

echo "==> done"
