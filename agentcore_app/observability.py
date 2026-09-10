"""One place for this agent's logs, metrics and cost.

Everything here writes to **stdout**, and nothing here opens a connection.
That's deliberate: AgentCore Runtime already ships this container's stdout to
`/aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT` with no configuration, so
a print is already a durable, queryable log line — it just wasn't *structured*,
and so wasn't joinable to anything else. Three things were missing, and this
module supplies all three without a second exporter or a new endpoint:

1. **A shape.** JSON per line, so CloudWatch Logs Insights can filter on
   fields (`filter event = "gate.blocked"`) instead of regexing free text.
2. **Correlation.** Every line carries `call_id`, `session_id`, and the
   current `trace_id`/`span_id`. `session_id` is the AgentCore Runtime session
   — the same value AgentCore stamps on every record in the
   `.../USAGE_LOGS/<agent>` group — so a call's logs, its trace, and its
   billed vCPU/GB-hours are one join away from each other. `xray_trace_id` is
   the same trace id in the dashed form the CloudWatch console links on.
3. **Metrics, for free.** `emit_call_ledger` writes CloudWatch Embedded Metric
   Format. CloudWatch Logs parses `_aws` out of any log group and publishes
   the named fields as real metrics — no PutMetricData call, so no API
   throttling on a busy container and no credentials needed beyond the log
   write the container already does.

Why cost needs code at all: Bedrock does not bill per call, per caller, or per
session anywhere you can query. Cost Explorer is account-and-day granular at
best. So the only way to answer "what did that call cost" is to count the
tokens as they arrive and multiply — which is what `CallUsage` and
`price_usd` below do.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any

SERVICE = os.getenv("OTEL_SERVICE_NAME", "ava-voice-agent")

# Set once per call by strands_agent._serve_caller. ContextVar rather than a
# module global because every task created after the set inherits a copy —
# which covers the model's own receive loop and each tool call, both of which
# are spawned from inside the call's task — while a genuinely unrelated task
# (the health check, say) keeps the default instead of picking up whichever
# call happened to run last.
CALL_ID: ContextVar[str] = ContextVar("call_id", default="-")
SESSION_ID: ContextVar[str] = ContextVar("session_id", default="-")


def _trace_ids() -> dict[str, str]:
    """The current span's ids, or nothing at all.

    Returns empty when tracing is off (local dev, AGENT_OBSERVABILITY_ENABLED
    unset) rather than emitting the all-zero invalid span id, so a local log
    line doesn't carry a trace id that resolves to nothing in the console.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return {}
        trace_id = f"{ctx.trace_id:032x}"
        return {
            "trace_id": trace_id,
            "span_id": f"{ctx.span_id:016x}",
            # X-Ray's own rendering of the same id. The console's deep links
            # and the Traces search box both want this form, not the raw hex.
            "xray_trace_id": f"1-{trace_id[:8]}-{trace_id[8:]}",
        }
    except Exception:
        return {}


class JsonFormatter(logging.Formatter):
    """Render a log record as one JSON object per line.

    Keys with a None value are dropped rather than serialized, so a line
    stays readable in the console and Logs Insights doesn't offer fields that
    are empty on most records.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
            "service": SERVICE,
            "call_id": CALL_ID.get(),
            "session_id": SESSION_ID.get(),
            **_trace_ids(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        # default=str so a stray datetime/UUID in a field can never turn a log
        # call into an exception inside the logging machinery.
        return json.dumps(
            {k: v for k, v in payload.items() if v is not None}, default=str
        )


def setup_logging() -> None:
    """Install the JSON formatter on the root logger.

    Call this before anything else in the process, and in particular before
    _setup_telemetry() — the OTel SDK logs its own startup and export
    failures through the standard logging module, and those lines are worth
    having in the same shape as everything else.

    Existing ``print()`` calls keep working and keep landing in the same log
    group; they just stay unstructured. Converting them is a per-line change,
    not a prerequisite for any of this.

    STRANDS_LOG_LEVEL is broken out separately because Strands at DEBUG logs
    every Nova Sonic frame including base64 audio — useful for one
    reproduction, ruinous as a standing setting on a voice agent.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logging.getLogger("strands").setLevel(
        os.getenv("STRANDS_LOG_LEVEL", "INFO").upper()
    )
    # uvicorn installs its own handlers at import; clearing them makes its
    # access and error lines come out in the same JSON shape as ours instead
    # of two formats interleaved in one log group.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


log = logging.getLogger("ava")


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event.

    ``event("memory.ready", actor=actor_id)`` rather than
    ``print(f"[memory] session manager ready (actor={actor_id})")`` — same
    information, but `actor` is a queryable field and `memory.ready` is
    countable.
    """
    log.log(level, name, extra={"event": name, "fields": fields})


# --------------------------------------------------------------------------
# Token usage and cost
# --------------------------------------------------------------------------

# USD per 1,000,000 tokens, as a JSON object in the environment:
#
#   NOVA_SONIC_PRICES_JSON='{"speech_in":X,"text_in":X,"speech_out":X,"text_out":X}'
#
# Not hardcoded, and deliberately with no default: prices differ by region and
# change over time, and a wrong-but-plausible constant baked into an image is
# worse than no number at all — it produces a cost dashboard everyone believes.
# With this unset the ledger still records every token count and simply reports
# `cost_priced: false`, publishing no CallCostUsd metric rather than a $0 one.
#
# Prefer setting it from SSM Parameter Store at deploy time over an inline
# literal, so a price change is a parameter edit rather than a redeploy.
_PRICES: dict[str, float] | None = None


def prices() -> dict[str, float]:
    """The price table, parsed once on first use.

    Lazy rather than evaluated at import: a malformed NOVA_SONIC_PRICES_JSON
    is worth an ERROR line, and at import time setup_logging() has not run
    yet, so that line would come out as bare unformatted text ahead of every
    structured log in the group. Deferring it to the first call means the
    complaint arrives in the same shape as everything else.
    """
    global _PRICES
    if _PRICES is not None:
        return _PRICES

    raw = os.getenv("NOVA_SONIC_PRICES_JSON", "").strip()
    if not raw:
        _PRICES = {}
        return _PRICES
    try:
        parsed = json.loads(raw)
        _PRICES = {str(k): float(v) for k, v in parsed.items()}
    except Exception as exc:
        event("pricing.config_invalid", level=logging.ERROR, error=str(exc))
        _PRICES = {}
    return _PRICES

# The four buckets Nova Sonic bills separately. Kept as an explicit tuple
# rather than iterating whatever the price map happens to contain, so a typo
# in the env var shows up as an unpriced bucket instead of silently pricing
# nothing.
TOKEN_BUCKETS = ("speech_in", "text_in", "speech_out", "text_out")


class CallUsage:
    """Token counts for one call, accumulated as Nova Sonic reports them.

    Nova's ``usageEvent`` carries running totals for the session, not deltas —
    so this **replaces** rather than adds. Summing them would multiply the
    bill by the number of usage events in the call, which on a two-minute call
    is a large factor in the wrong direction.

    `raw_seen` exists because the exact key names inside `usageEvent.details`
    are the one thing here not verified against live traffic: Strands throws
    that sub-object away (`nova_sonic._convert_nova_event` keeps only
    `totalInputTokens`/`totalOutputTokens`), and DEBUG logging has never been
    on in this container, so no sample of it exists anywhere in CloudWatch.
    The first event of each process is therefore logged verbatim; read one out
    of the log group after the next deploy and tighten `update()` against what
    it actually says.
    """

    def __init__(self) -> None:
        self.tokens: dict[str, int] = {bucket: 0 for bucket in TOKEN_BUCKETS}
        self.total_input = 0
        self.total_output = 0
        self.events = 0
        self.raw_seen = False

    def update(self, usage: dict[str, Any]) -> None:
        self.events += 1
        if not self.raw_seen:
            self.raw_seen = True
            # Truncated: this is a diagnostic, not a payload store, and an
            # unbounded field is how a log group turns into a bill.
            event("nova.usage.raw", payload=json.dumps(usage, default=str)[:2000])

        self.total_input = int(usage.get("totalInputTokens", self.total_input) or 0)
        self.total_output = int(usage.get("totalOutputTokens", self.total_output) or 0)

        # Nova reports per-modality counts under details.total.{input,output}.
        # Read defensively: an unexpected shape must degrade to "no modality
        # split" (the totals are still right) rather than raise inside the
        # model's receive loop and drop the call.
        try:
            total = (usage.get("details") or {}).get("total") or {}
            for side, suffix in (("input", "in"), ("output", "out")):
                counts = total.get(side) or {}
                if not isinstance(counts, dict):
                    continue
                for key, value in counts.items():
                    lowered = key.lower()
                    if "speech" in lowered or "audio" in lowered:
                        self.tokens[f"speech_{suffix}"] = int(value or 0)
                    elif "text" in lowered:
                        self.tokens[f"text_{suffix}"] = int(value or 0)
        except Exception as exc:
            event("nova.usage.parse_failed", level=logging.WARNING, error=str(exc))

    @property
    def split_available(self) -> bool:
        """Whether a modality breakdown was actually found.

        False means the totals are trustworthy but the cost is not: speech and
        text are priced very differently, so a blended guess off the totals can
        be out by a large multiple. The ledger reports this rather than hiding
        it behind a confident-looking number.
        """
        return any(self.tokens.values())

    def cost_usd(self) -> float | None:
        """Cost for this call, or None when it can't be computed honestly."""
        table = prices()
        if not table or not self.split_available:
            return None
        missing = [b for b in TOKEN_BUCKETS if self.tokens[b] and b not in table]
        if missing:
            event("pricing.bucket_missing", level=logging.WARNING, buckets=missing)
            return None
        return sum(self.tokens[b] * table.get(b, 0.0) for b in TOKEN_BUCKETS) / 1_000_000


# One per call, set by _serve_caller. The default instance is a throwaway that
# absorbs writes from anything running outside a call rather than raising.
CALL_USAGE: ContextVar[CallUsage] = ContextVar("call_usage", default=CallUsage())


def record_nova_usage(usage: dict[str, Any]) -> None:
    """Hook for the model subclass — never allowed to raise.

    This runs inside Nova Sonic's receive loop, on the path every audio frame
    takes. An exception here would surface as a dropped call, which is not a
    trade anyone would make for a cost metric.
    """
    try:
        CALL_USAGE.get().update(usage)
    except Exception as exc:
        event("nova.usage.record_failed", level=logging.WARNING, error=str(exc))


# --------------------------------------------------------------------------
# The call ledger
# --------------------------------------------------------------------------

# Per-call tallies that aren't token counts: tool invocations, gate blocks.
# A plain dict behind a ContextVar rather than fields on CallUsage, so adding
# a new thing to count is a `bump("whatever")` at the site that knows about
# it and a line in the ledger — no signature to thread through.
COUNTERS: ContextVar[dict[str, int]] = ContextVar("counters", default={})


def bump(name: str, amount: int = 1) -> None:
    """Increment a per-call counter; never raises."""
    try:
        counters = COUNTERS.get()
        counters[name] = counters.get(name, 0) + amount
    except Exception:
        pass


EMF_NAMESPACE = os.getenv("EMF_NAMESPACE", "Ava/Calls")


def emit_call_ledger(
    *,
    usage: CallUsage,
    duration_seconds: float,
    outcome: str,
    tool_calls: int = 0,
    gate_blocks: int = 0,
    escalated: bool = False,
    human_handoff: bool = False,
    **extra: Any,
) -> None:
    """One line per finished call: what happened, what it consumed, what it cost.

    Written as CloudWatch EMF, so the numeric fields become metrics in
    ``Ava/Calls`` automatically. `outcome` is a dimension because that is the
    cut that actually gets asked for — "escalated calls cost 3x and there are
    more of them this week" is a dashboard line rather than a query.

    CallCostUsd is included in the metric list **only when it could be
    computed**. An unpriced call still emits every token count and a
    `cost_priced: false` field; what it must not do is publish a $0 cost, which
    would drag the average down and read as "calls are cheap" rather than
    "cost is not configured".
    """
    cost = usage.cost_usd()

    metrics: list[dict[str, str]] = [
        {"Name": "Calls", "Unit": "Count"},
        {"Name": "CallDurationSeconds", "Unit": "Seconds"},
        {"Name": "InputTokens", "Unit": "Count"},
        {"Name": "OutputTokens", "Unit": "Count"},
        {"Name": "ToolCalls", "Unit": "Count"},
        {"Name": "GateBlocks", "Unit": "Count"},
    ]
    body: dict[str, Any] = {
        "service": SERVICE,
        "outcome": outcome,
        "call_id": CALL_ID.get(),
        "session_id": SESSION_ID.get(),
        "event": "call.ledger",
        "Calls": 1,
        "CallDurationSeconds": round(duration_seconds, 3),
        "InputTokens": usage.total_input,
        "OutputTokens": usage.total_output,
        "ToolCalls": tool_calls,
        "GateBlocks": gate_blocks,
        "escalated": escalated,
        "human_handoff": human_handoff,
        "usage_events": usage.events,
        "modality_split_available": usage.split_available,
        "cost_priced": cost is not None,
        **_trace_ids(),
        **extra,
    }

    if usage.split_available:
        for bucket in TOKEN_BUCKETS:
            name = "".join(part.capitalize() for part in bucket.split("_")) + "Tokens"
            metrics.append({"Name": name, "Unit": "Count"})
            body[name] = usage.tokens[bucket]

    if cost is not None:
        metrics.append({"Name": "CallCostUsd", "Unit": "None"})
        body["CallCostUsd"] = round(cost, 6)

    body["_aws"] = {
        "Timestamp": int(time.time() * 1000),
        "CloudWatchMetrics": [
            {
                "Namespace": EMF_NAMESPACE,
                # Both cuts: the unconditioned total, and the per-outcome
                # split. A metric is only queryable along dimension sets that
                # were declared when it was written, so leaving ["service"]
                # out would mean no way to ask for total spend.
                "Dimensions": [["service"], ["service", "outcome"]],
                "Metrics": metrics,
            }
        ],
    }

    # print, not the logger: EMF requires this exact object at the root of the
    # log event, and the JSON formatter would nest it under its own envelope
    # where CloudWatch will not look for it.
    print(json.dumps(body, default=str))
