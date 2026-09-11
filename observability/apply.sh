#!/usr/bin/env bash
# Create/update the Ava observability dashboard and its alarms.
#
# Split out of the deploy because neither is deployable by the IAM user this
# repo is normally driven from: cloudwatch:PutDashboard and
# cloudwatch:PutMetricAlarm are both denied for it today. Grant those (plus
# ce:GetCostAndUsage for the monthly reconciliation) and this becomes a
# one-liner; until then it's a runbook you can hand to whoever has them.
#
# Safe to re-run: both APIs are upserts.
set -euo pipefail

REGION="${REGION:-us-west-2}"
SERVICE="${SERVICE:-ava-voice-agent}"
# Where an alarm sends notifications. Leave unset to create alarms with no
# action — they still show state in the console, they just won't page anyone.
ALARM_TOPIC_ARN="${ALARM_TOPIC_ARN:-}"
LOG_GROUP="${LOG_GROUP:-/aws/bedrock-agentcore/runtimes/ava_voice_agent-TVSWmk7feq-DEFAULT}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> dashboard"
aws cloudwatch put-dashboard --region "$REGION" \
  --dashboard-name Ava-Observability \
  --dashboard-body "file://$HERE/dashboard.json" >/dev/null
echo "    https://$REGION.console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards/dashboard/Ava-Observability"

# Expanded below with the ${x[@]+...} guard rather than bare "${x[@]}":
# macOS ships bash 3.2, where expanding an empty array under `set -u` is an
# "unbound variable" error rather than nothing at all.
alarm_actions=()
[ -n "$ALARM_TOPIC_ARN" ] && alarm_actions=(--alarm-actions "$ALARM_TOPIC_ARN")

echo "==> alarms"

# Daily model spend. Threshold is a placeholder — set it from a week of real
# ledger data rather than guessing, or it will either never fire or always fire.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-DailyModelCost-High \
  --alarm-description "Nova Sonic spend for the day is above budget (from the per-call EMF ledger)" \
  --namespace Ava/Calls --metric-name CallCostUsd \
  --dimensions Name=service,Value="$SERVICE" \
  --statistic Sum --period 86400 --evaluation-periods 1 \
  --threshold "${DAILY_COST_THRESHOLD:-50}" --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching ${alarm_actions[@]+"${alarm_actions[@]}"}

# A single runaway call — a session that never closed, a barge-in loop.
# Catching this per-call matters more than the daily total: the daily alarm
# only fires after the money is spent.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-CostPerCall-High \
  --alarm-description "Average cost per call is well above baseline — likely a session that isn't ending" \
  --namespace Ava/Calls --metric-name CallCostUsd \
  --dimensions Name=service,Value="$SERVICE" \
  --statistic Average --period 3600 --evaluation-periods 1 \
  --threshold "${COST_PER_CALL_THRESHOLD:-2}" --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching ${alarm_actions[@]+"${alarm_actions[@]}"}

# Calls that ended in an exception rather than a hangup. caller_disconnected
# is deliberately a separate outcome so it doesn't count here.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-CallErrors \
  --alarm-description "Calls ending with outcome=error" \
  --namespace Ava/Calls --metric-name Calls \
  --dimensions Name=service,Value="$SERVICE" Name=outcome,Value=error \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold "${ERROR_THRESHOLD:-3}" --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching ${alarm_actions[@]+"${alarm_actions[@]}"}

# Silence is the failure mode this whole change exists to prevent: if the
# ledger stops arriving, every widget above goes quiet and looks calm.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-NoLedgerData \
  --alarm-description "No call ledger written in 6h — the agent is down, or observability.py stopped emitting" \
  --namespace Ava/Calls --metric-name Calls \
  --dimensions Name=service,Value="$SERVICE" \
  --statistic Sum --period 21600 --evaluation-periods 1 \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching ${alarm_actions[@]+"${alarm_actions[@]}"}

# Counts hangups straight out of the log stream, so it can be compared against
# the ledger the hangup was supposed to produce. There is no such metric
# otherwise: the EMF block only exists on a ledger line, so a call whose ledger
# never got written is invisible to every metric in Ava/Calls by definition.
#
# Not retroactive — a metric filter only sees log events written after it
# exists, so the series starts empty and the alarm below sits in
# INSUFFICIENT_DATA until the next call.
echo "==> metric filter"
aws logs put-metric-filter --region "$REGION" \
  --log-group-name "$LOG_GROUP" \
  --filter-name Ava-CallsDisconnected \
  --filter-pattern '{ $.event = "call.caller_disconnected" }' \
  --metric-transformations \
    metricName=CallsDisconnected,metricNamespace=Ava/Calls,metricValue=1,defaultValue=0
echo "    Ava/Calls CallsDisconnected"

# Every caller hangup owes a ledger line. When that stops being true, the call
# still happened, still cost money, and shows up in nothing — which is exactly
# what happened on 2026-09-11, twice, before v21: teardown froze the event loop
# and emit_call_ledger never ran.
#
# Ava-NoLedgerData above cannot catch this. It watches the ledger count alone,
# so "nobody rang" and "three people rang and all three vanished" look
# identical to it. This one watches the gap between the two.
echo "==> lost-ledger alarm"
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-LedgerLost \
  --alarm-description "A call disconnected but wrote no ledger line: the call happened, cost money, and appears in no metric" \
  --metrics "[
    {\"Id\":\"disc\",\"MetricStat\":{\"Metric\":{\"Namespace\":\"Ava/Calls\",\"MetricName\":\"CallsDisconnected\"},\"Period\":3600,\"Stat\":\"Sum\"},\"ReturnData\":false},
    {\"Id\":\"ledg\",\"MetricStat\":{\"Metric\":{\"Namespace\":\"Ava/Calls\",\"MetricName\":\"Calls\",\"Dimensions\":[{\"Name\":\"service\",\"Value\":\"$SERVICE\"}]},\"Period\":3600,\"Stat\":\"Sum\"},\"ReturnData\":false},
    {\"Id\":\"lost\",\"Expression\":\"FILL(disc,0) - FILL(ledg,0)\",\"Label\":\"calls that ended with no ledger line\",\"ReturnData\":true}
  ]" \
  --comparison-operator GreaterThanThreshold --threshold 0 \
  --evaluation-periods 1 --datapoints-to-alarm 1 \
  --treat-missing-data notBreaching ${alarm_actions[@]+"${alarm_actions[@]}"}

echo "==> done"
aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix Ava- \
  --query 'MetricAlarms[].{Alarm:AlarmName,State:StateValue}' --output table
