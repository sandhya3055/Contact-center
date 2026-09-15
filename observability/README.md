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

## Applying the dashboards and alarms

```bash
ALARM_TOPIC_ARN=arn:aws:sns:us-west-2:...:ops ./observability/apply.sh
```

Applied 2026-09-11: the `Ava-Observability` dashboard and five alarms
(`Ava-DailyModelCost-High`, `Ava-CostPerCall-High`, `Ava-CallErrors`,
`Ava-NoLedgerData`, `Ava-LedgerLost`) exist in us-west-2, along with the
`Ava-CallsDisconnected` and `Ava-CallsEnded` metric filters.

The dashboard is in two sections, and the split is the point: everything
above **Account spend** moves within a minute of a call ending, and the four
widgets below it never do — they are Cost Explorer's daily figures, settled
about three days late. Reading a stale cost widget as "my call didn't
register" is the mistake that separation exists to prevent.

### `Ava-KPIs` — the contact-centre dashboard

`kpis_dashboard.json`, applied by the same script: abandonment, escalation,
engagement, containment, success, FCR, AHT, intent mix and unit cost.
A second dashboard rather than more rows on the first, because CloudWatch has
no tabs and the two get read by different people — `Ava-Observability` answers
"is it working and what did it cost", `Ava-KPIs` answers "how is the contact
centre doing". Both read the same ledger, so they cannot disagree.

Every rate is counted **two ways on purpose**, and the two are allowed to
differ:

| | Source | Asks | Lag |
|---|---|---|---|
| Connection view | `call.ledger` EMF metrics | how did the *connection* end? | ~1 min |
| Need view | `case.summarized` log line | did the *caller* get what they rang for? | minutes |

A caller who got their answer and hung up satisfied leaves a socket identical
to an abandoned call, so the connection view scores them the same and the need
view can upgrade one of them (`cases.py`, `_RESOLVED_UPGRADE_FROM`). The
headline abandonment tile is therefore an **upper bound**; the need-view
widgets lower down are the ones to quote. The "connection view vs need view"
table shows every disagreement, which is also how you audit the upgrade.

Escalation, engagement and the quality proxies are **Logs Insights queries,
not metrics** — `escalated`, `transcript_turns` and `tool_errors` are fields
on the ledger line, and the EMF block publishes only Calls, duration, tokens,
tool calls, gate blocks and cost. So they work retroactively over log
retention but cannot be alarmed on. Promoting them means adding to the
`metrics` list in `emit_call_ledger`, which is not retroactive.

**There is no average quality score, and nothing on the dashboard substitutes
for one.** The gate-block and tool-error proxies measure discipline, not
whether a call was any good. `evals/golden/` is an offline pre-ship regression
gate over 40 fixed cases — it cannot score production calls. A real score
needs a judge reading transcripts at teardown and publishing `QualityScore`
through `emit_call_ledger`: per-call model cost, added teardown latency, and a
sampling decision. That is a feature, not a widget.

#### Why no two widgets show the same count

They do not cover the same calls, and that is not a fault. Two separate
effects stack up:

1. **Order of writes.** `call.teardown` -> `call.ledger` -> `case.written` ->
   *a model reads the transcript* -> `case.summarized`. Each step can fail on
   its own, and the later ones cost time and money.
2. **Logs are not retroactive.** Each line exists only from the moment the
   code that writes it shipped. Calls older than that are permanently missing
   from the newer widgets; waiting does not fill them in.

Measured over the 30 days to 2026-09-14: 21 ledger lines (from 09-11 05:37),
18 of them priced, 10 teardown lines (added 09-11, `34fb35d`), 7 case records
(`cases.py` shipped 09-14, `4b9dde9`). A widget showing 7 against another
showing 21 is saying "instrumented since Sunday", not "fourteen calls broke".

The coverage row near the top of the dashboard exists to make that legible
before anyone compares two numbers further down: a bar chart of records per
source, a daily line per source (a line that starts late was instrumented
late), and the priced/unpriced split. Read those three first.

#### Every rate is `sum(x)/count(*)`, never `avg(x)`

`avg()` in Logs Insights **skips rows where the field is absent**, while
`count(*)` keeps them. On a partly-populated field `avg` therefore shrinks its
own denominator and reports a rate over a different population than the count
sitting next to it.

This was live on the dashboard: the FCR widget read **7 cases, 3 resolved,
60%**. 60% is 3/5 -- only five of the seven `case.summarized` rows carried
`caller_need_met`, so `avg` quietly divided by 5 while `count(*)` said 7. The
true figure is 3/7 = 42.9%.

So every share-of-calls rate uses `sum(x)*100.0/count(*)`, which pins the
denominator and reads a missing field as "not X" -- a row that never recorded
the judgement is not evidence the caller's need was met. Per-call counts
(turns, tool calls, gate blocks, tool errors) use `sum(x)/count(*)` for the
same reason: absent means none occurred.

Two `avg()` calls survive on purpose, because there absent means *unknown*
rather than *zero*: `avg(CallCostUsd)` is the average over **priced** calls
(the priced/unpriced widget exposes that gap, and inventing a $0 is the thing
`emit_call_ledger` refuses to do), and `avg(CallDurationSeconds)` should not
treat a call of unknown length as instantaneous.

The FCR widget carries `rows_judged` beside `cases` so a diluted rate is
visible rather than inferred.

#### A `not recorded` slice is a schema change, not a category

A null group key renders in CloudWatch as a bare `--`, which reads like a
resolution status and is not one. Every `stats ... by` on an optional field
wraps it in `coalesce(field, "not recorded")` so those rows are counted --
they are real calls -- and labelled.

Here they are rows written before the field existed. `case.summarized` gained
`resolution_status`, `derived_status`, `caller_need_met` and `upgraded` in a
deploy on 2026-09-14 around 08:48; the two rows from 07:27 and 08:11 carry
only `case_id` and `customer_intent`. Note those fields are in the **working
tree** copy of `cases.py` and in no commit -- the container is running
uncommitted code, which is why the shape changed mid-morning with nothing in
the history to explain it.

#### Periods are pinned; `periodOverride` is `inherit`, not `auto`

`auto` would rewrite every widget's period from the selected time range, and
on the `Ava/Cost` widgets the `Maximum` @ `86400` pair is load-bearing:
`cost_publisher` re-writes past days as Cost Explorer settles them, so a day's
true cost is the **maximum** published for that day, never the sum or the
average of sub-day buckets. Auto periods would silently corrupt every cost
figure on the dashboard, so the body sets `"periodOverride": "inherit"`.

Trend charts are plotted **daily**. At the current volume hourly buckets are
mostly empty and read as an outage rather than a quiet afternoon. If volume
grows, raise the period per widget -- do not switch the dashboard to `auto`.

#### The dashboard is zeroed at 2026-09-14T12:12:19Z

`"start"` is that absolute timestamp with no `"end"`, so the window is always
"everything since the reset" and every widget began at 0 and grows.

**Nothing was deleted.** CloudWatch metrics cannot be deleted -- there is no
API for it and they age out over 15 months -- so a data-level wipe is not
available, and deleting the log group instead would have been strictly worse:
the metric tiles would have kept reading 21 calls while every Logs Insights
widget dropped to zero. Widening the range still shows the pre-reset history,
including the 2026-09-11 lost-ledger incident.

The reset earns its keep because **every instrument was already live before
that moment** -- `call.ledger` since 09-11, `call.teardown` since 09-11 11:38,
the case events since the 09-14 08:48 deploy. So the coverage widgets now
count the same population and should agree. That inverts what a short count
means: before the reset it meant "instrumented later", and from here it can
only mean calls are losing their ledger. The coverage section says so on the
dashboard.

If a true data-level reset is ever wanted, it is a namespace cutover, not a
deletion: `EMF_NAMESPACE` is env-configurable (`observability.py`), so a new
value plus an agent redeploy starts the metrics genuinely empty. It also blanks
`Ava-Observability` and sends all five alarms and both metric filters to
INSUFFICIENT_DATA until they are repointed, so it is a deliberate migration
rather than a tidy-up.

#### FCR is two halves, reported separately

A call can be resolved perfectly and still fail FCR, because the caller rang
again the next day. So the dashboard reports both halves and refuses to
average them into one number:

- **Resolved on the call** — `caller_need_met` from `case.summarized`, the
  transcript's own judgement.
- **No repeat contact** — whether that `policy_id` appears more than once.

The second half is `count_distinct(policy_id) / count(*)` rather than the
textbook "policies that rang exactly once / policies", because Logs Insights
has no nested aggregation and that definition needs two levels. The widget
next to it lists every policy that rang more than once with its first and
last outcome, which is the evidence the ratio is summarising — read them
together. Calls with no `policyId` are excluded from both (`case.written`
logs the field only when it is non-empty), so its `contacts` count is
legitimately lower than `Calls` up top.

#### Cost: three layers that must never be summed

| Layer | Metric | What it is | Lag |
|---|---|---|---|
| Per call | `Ava/Calls CallCostUsd` | Nova Sonic, priced from the call's own tokens | ~1 min |
| Per service | `Ava/Cost DailyCostUsd` by `category` | what AWS **billed**, split by `cost_categories.py` | ~3 days |
| Runtime compute | AgentCore usage logs | vCPU/GB-hours a session held open | ~1 min |

`ModelProduction` in the service breakdown **is the same money as
`CallCostUsd`** — our estimate from tokens versus the actual bill.
`cost_categories.py` says as much in `CATEGORIES` (`"ledger: CallCostUsd"`).
Adding them double-counts; comparing them is what `reconcile.py` does monthly.

No cost tile uses `FILL(…,0)`, on purpose. `emit_call_ledger` withholds
`CallCostUsd` entirely when a call could not be priced rather than publishing
a zero, because a zero reads as "calls are cheap" instead of "cost is not
configured" — and a dashboard that fills the gap back in undoes that. An
unpriced range shows **No data**. The "priced vs unpriced" widget is how you
tell the two apart: if `unpriced` is not zero, every cost tile is understated,
since cost averages over priced calls while `Calls` counts all of them.

The daily stacked chart *does* use `FILL`, because there a zero is true — a
service with no spend that day cost nothing — and stacking needs aligned
points. It reads `Maximum` at a 86400 period, not `Sum`: the publisher
re-writes past days as Cost Explorer settles them, so summing would count the
same day twice.

`Ava-NoLedgerData` uses `--treat-missing-data breaching` on purpose — silence
is the failure this whole change exists to catch — so it will go to ALARM
before the first deploy. That is correct behaviour, not a bug, and it is
harmless while `ALARM_TOPIC_ARN` is unset and no action is attached.

`Ava-LedgerLost` is the one that catches a subtler failure, and it exists
because the obvious alarm missed a real outage. `Ava-NoLedgerData` counts
ledger lines alone, so "nobody rang" and "three people rang and all three
vanished" look identical to it. On 2026-09-11 two calls in a row ran fine,
cost money, and wrote no ledger; every widget stayed calm and no alarm moved.

So two metric filters count endings straight out of the log stream — a
metric filter, because the EMF block only exists *on* a ledger line, which
makes a missing ledger invisible to every metric in `Ava/Calls` by definition.
`Ava-CallsEnded` counts `call.teardown`, which is logged unconditionally on
every path a call can end by, and is what the alarm watches:
`FILL(ended,0) - FILL(ledg,0)`. Every ending owes a ledger line, and the gap
between them is the number of calls that disappeared.

`Ava-CallsDisconnected` counts caller hangups alone. It came first and is
kept as the "who ended it" cut, but it is the wrong thing to alarm on: it
would have missed both calls Ava ended herself on 2026-09-11, which are
exactly the ones that lost their ledger.

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
