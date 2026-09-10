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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> dashboard"
aws cloudwatch put-dashboard --region "$REGION" \
  --dashboard-name Ava-Observability \
  --dashboard-body "file://$HERE/dashboard.json" >/dev/null
echo "    https://$REGION.console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards/dashboard/Ava-Observability"

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
  --treat-missing-data notBreaching "${alarm_actions[@]}"

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
  --treat-missing-data notBreaching "${alarm_actions[@]}"

# Calls that ended in an exception rather than a hangup. caller_disconnected
# is deliberately a separate outcome so it doesn't count here.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-CallErrors \
  --alarm-description "Calls ending with outcome=error" \
  --namespace Ava/Calls --metric-name Calls \
  --dimensions Name=service,Value="$SERVICE" Name=outcome,Value=error \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold "${ERROR_THRESHOLD:-3}" --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching "${alarm_actions[@]}"

# Silence is the failure mode this whole change exists to prevent: if the
# ledger stops arriving, every widget above goes quiet and looks calm.
aws cloudwatch put-metric-alarm --region "$REGION" \
  --alarm-name Ava-NoLedgerData \
  --alarm-description "No call ledger written in 6h — the agent is down, or observability.py stopped emitting" \
  --namespace Ava/Calls --metric-name Calls \
  --dimensions Name=service,Value="$SERVICE" \
  --statistic Sum --period 21600 --evaluation-periods 1 \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching "${alarm_actions[@]}"

echo "==> done"
aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix Ava- \
  --query 'MetricAlarms[].{Alarm:AlarmName,State:StateValue}' --output table
