# Observability

Where everything this system emits ends up, and how to read it.

## One place

CloudWatch, in `us-west-2`. Not because it's the nicest LLM-observability UI —
it isn't — but because the traces already went there and the AgentCore
platform's own spans and usage records can't be sent anywhere else. Adding a
second destination would have meant two places to look, which is the problem,
not the fix.

| What | Where | Emitted by |
|---|---|---|
| Agent logs (JSON) | `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` | `agentcore_app/observability.py` |
| Per-call ledger + `Ava/Calls` metrics | same group, as EMF | `emit_call_ledger` |
| Traces / spans | `aws/spans`, and the GenAI Observability console | `opentelemetry-instrument` + `_setup_telemetry` |
| Runtime compute usage | `/aws/vendedlogs/.../USAGE_LOGS/<agent>` | AgentCore, automatically |
| Gateway tool Lambda | `/aws/lambda/insurarncae-voice-agent-mcp-tool` | `MCP/mcp_tools.py` |
| Model invocations | `model-invocation-logs` in **both** `us-east-1` and `us-west-2` | Bedrock |

They join on **`session_id`** — the AgentCore Runtime session id, which is on
every agent log line, on the `bidi_session` span, and on every AgentCore usage
record. `call_id` narrows further within the agent, and `trace_id` links a log
line to its span.

## Cost

There is no AWS API that reports what one call cost, so it's computed:

- **Model** — `observability.py` counts Nova Sonic's tokens per modality as
  they arrive and multiplies by `NOVA_SONIC_PRICES_JSON`. Speech and text are
  billed very differently, which is why the split matters; Strands drops it,
  so `_UsageCapturingNovaSonic` in `strands_agent.py` taps the raw provider
  event. Lands as `CallCostUsd` in `Ava/Calls`.
- **Compute** — AgentCore's own `USAGE_LOGS`, already per-session. Query in
  the dashboard's bottom-left widget.
- **Truth** — Cost Explorer, monthly, reconciled against the sum of the
  above. Drift over ~10% means the price table or the modality mapping is
  wrong, not that the bill is.

Set `NOVA_SONIC_PRICES_JSON` on the runtime or there is no cost figure at all
— by design. See `.env.example`.

## Applying the dashboard and alarms

```bash
ALARM_TOPIC_ARN=arn:aws:sns:us-west-2:...:ops ./observability/apply.sh
```

Both `cloudwatch:PutDashboard` and `cloudwatch:PutMetricAlarm` are currently
**denied** for the repo's usual IAM user, which is why this is a script rather
than part of the deploy. The same user also lacks `ce:GetCostAndUsage`, so the
monthly reconciliation above can't be run by them either. Three permissions to
grant whoever owns this account:

```
cloudwatch:PutDashboard
cloudwatch:PutMetricAlarm
ce:GetCostAndUsage
```

Alarm thresholds in `apply.sh` are placeholders. Set them from a week of real
ledger data — a guessed threshold either never fires or always does, and both
teach people to ignore it.

## Useful queries

Everything a single call did, across the agent and the tool Lambda:

```
SOURCE '/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT'
| SOURCE '/aws/lambda/insurarncae-voice-agent-mcp-tool'
| fields @timestamp, service, event, msg, tool, error
| filter session_id = "<session id>"
| sort @timestamp asc
```

The most expensive calls yesterday:

```
fields call_id, session_id, outcome, CallCostUsd, CallDurationSeconds, ToolCalls
| filter event = "call.ledger"
| sort CallCostUsd desc
| limit 20
```

Gate activity — what Ava tried that was refused:

```
fields @timestamp, call_id, gate, action, reason, tool, fields
| filter governance_event = "ava_gate"
| stats count() by gate, action, reason
```
