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
- **Truth** — Cost Explorer, monthly. **Filter to `RECORD_TYPE = Usage`** or
  the answer is always zero: this account's spend is fully offset by credits,
  so net cost grouped by service reads `-0.00` while gross usage is real.

Two things worth knowing before reading any of these numbers:

- **Compute outweighs model tokens ~6.5:1 here.** Gross usage for
  2026-09-01→11 was $21.83 Amazon Bedrock AgentCore against $3.35 Amazon
  Bedrock. `CallCostUsd` is the smaller half of the bill.
- **The `gen_ai.usage.*` attributes on `bidi_session` spans are inflated by
  roughly 100x and must not be used for cost.** Strands' bidi event loop sums
  Nova's running totals as if they were deltas
  (`strands/experimental/bidi/agent/loop.py`). `CallUsage` in
  `observability.py` detects which reading applies rather than assuming, and
  reports its conclusion as `usage_mode` on every ledger line.

Set `NOVA_SONIC_PRICES_JSON` on the runtime or there is no cost figure at all
— by design. See `.env.example`.

## Daily cost metrics (`Ava/Cost`)

A CloudWatch dashboard can only render CloudWatch metrics and Logs Insights
queries — there is no widget that talks to Cost Explorer. So container image
storage, AgentCore Memory, eval traffic and portal hosting could not appear
on the dashboard at all. `cost_publisher.py` closes that: a Lambda that reads
CE once a day and writes each category into `Ava/Cost` as a real metric.

```bash
./observability/deploy_cost_publisher.sh      # idempotent; creates or updates
```

**The x-axis is when the figure was learned, not when the money was spent**,
and the two differ by ~3 days. That is a workaround, not a choice: backdated
PutMetricData is accepted here and then never readable — ListMetrics shows
the metric, GetMetricStatistics returns nothing for it, permanently. Verified
with a probe (current timestamp reads back in under a minute, three days back
never does). Two consequences: a spike on the chart means spend spiked ~3
days earlier, and **history cannot be backfilled** — the series starts at
first run. Use `cost_report.py` for anything before that.

The schedule step needs `events:PutRule`, which is denied for the usual IAM
user. Until it is granted the Lambda exists but nothing triggers it; invoke
by hand with:

```bash
aws lambda invoke --region us-west-2 --function-name ava-cost-publisher \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
```

## Applying the dashboard and alarms

```bash
ALARM_TOPIC_ARN=arn:aws:sns:us-west-2:...:ops ./observability/apply.sh
```

Applied 2026-09-11: the `Ava-Observability` dashboard and five alarms
(`Ava-DailyModelCost-High`, `Ava-CostPerCall-High`, `Ava-CallErrors`,
`Ava-NoLedgerData`, `Ava-LedgerLost`) exist in us-west-2, along with the
`Ava-CallsDisconnected` metric filter.

`Ava-NoLedgerData` uses `--treat-missing-data breaching` on purpose — silence
is the failure this whole change exists to catch — so it will go to ALARM
before the first deploy. That is correct behaviour, not a bug, and it is
harmless while `ALARM_TOPIC_ARN` is unset and no action is attached.

`Ava-LedgerLost` is the one that catches a subtler failure, and it exists
because the obvious alarm missed a real outage. `Ava-NoLedgerData` counts
ledger lines alone, so "nobody rang" and "three people rang and all three
vanished" look identical to it. On 2026-09-11 two calls in a row ran fine,
cost money, and wrote no ledger; every widget stayed calm and no alarm moved.

So `Ava-CallsDisconnected` counts hangups straight out of the log stream — a
metric filter, because the EMF block only exists *on* a ledger line, which
makes a missing ledger invisible to every metric in `Ava/Calls` by definition
— and the alarm watches `FILL(disc,0) - FILL(ledg,0)`. Every hangup owes a
ledger line; the gap between them is the number of calls that disappeared.

A metric filter is not retroactive: it only sees events written after it was
created, so the series starts empty and the alarm sits in `INSUFFICIENT_DATA`
until the next call.

Alarm thresholds are placeholders. Set them from a week of real ledger data —
a guessed threshold either never fires or always does, and both teach people
to ignore it.

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
