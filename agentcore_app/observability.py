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
    # lines come out in the same JSON shape as ours instead of two formats
    # interleaved in one log group.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True

    # ...but not the access log, which on this service is almost entirely
    # AgentCore's `/ping`. Measured after the first deploy: uvicorn.access was
    # 10,791 lines/hour — 85% of all lines and 93% of all bytes in the runtime
    # log group, about 54 MB/day, none of it about a phone call.
    #
    # Routing it through the JSON formatter is what made it expensive: every
    # health check picked up a ~200-byte envelope of call_id/trace_id fields
    # that are all "-" for a request with no call behind it. So this is a
    # regression introduced by setup_logging itself, not a pre-existing one.
    #
    # WARNING rather than removing the logger: a genuine uvicorn access-layer
    # problem still surfaces. Set UVICORN_ACCESS_LOG_LEVEL=INFO to get the
    # request-by-request view back while debugging a routing issue.
    logging.getLogger("uvicorn.access").setLevel(
        os.getenv("UVICORN_ACCESS_LOG_LEVEL", "WARNING").upper()
    )


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


# How many usage events get logged verbatim per process. Enough to see
# whether the numbers grow (cumulative) or jitter (deltas) without turning the
# log group into a payload store.
RAW_USAGE_SAMPLES = 3


class CallUsage:
    """Token counts for one call, accumulated as Nova Sonic reports them.

    **The reporting mode is detected, not assumed**, and that is the whole
    design of this class. Strands gets this wrong for Nova: its event loop
    does ``self._accumulated_input_tokens += event.input_tokens`` (bidi
    ``agent/loop.py``) on an event whose fields are named ``totalInputTokens``
    and whose payload offers both a ``details.delta`` and a ``details.total``
    — i.e. a running total, summed as though it were a delta. That is why
    ``gen_ai.usage.*`` on the ``bidi_session`` span reads roughly two orders of
    magnitude above what the account is actually billed for, and why those
    span attributes must not be used for cost. (The ``+=`` is presumably
    correct for Gemini/OpenAI Realtime, which is how it survives.)

    Guessing the other way would be just as bad, so neither is hardcoded:

    * Totals that never decrease across a call are cumulative → keep the last.
    * A value that drops has to be a per-interval delta (a running total
      cannot go backwards) → sum from then on, including what came before.

    The conclusion is recorded as `mode` and reported on every ledger line, so
    a wrong inference is visible in the data rather than silently priced.

    The exact key names inside ``usageEvent.details`` are the remaining
    unknown: Strands discards that sub-object, and DEBUG has never run in this
    container, so no sample of it exists anywhere in CloudWatch. The first few
    events of each process are logged verbatim as ``nova.usage.raw`` — read one
    after deploying and tighten the parsing below against it.
    """

    def __init__(self) -> None:
        self.tokens: dict[str, int] = {bucket: 0 for bucket in TOKEN_BUCKETS}
        self.total_input = 0
        self.total_output = 0
        self.events = 0
        # "cumulative" until a decrease proves otherwise.
        self.mode = "cumulative"
        self._last: dict[str, int] = {}
        # Running sum of every figure ever reported for a key, maintained
        # unconditionally. It is the answer under the delta reading, and it
        # has to be kept from the first event because the switch can happen
        # at any point — reconstructing it afterwards from `_last` alone
        # would lose every value before the most recent one.
        self._sum: dict[str, int] = {}

    def _apply(self, key: str, value: int) -> int:
        """Fold one reported figure in, under whichever mode is in force."""
        previous = self._last.get(key)
        if previous is not None and value < previous and self.mode == "cumulative":
            # A running total cannot go backwards, so every figure so far was
            # a per-interval delta that happened to be increasing. Switch the
            # reading for the whole call, not just from here on.
            self.mode = "delta"
            event(
                "nova.usage.mode_switch",
                level=logging.WARNING,
                field=key,
                previous=previous,
                received=value,
                note="values decreased, so these are deltas, not running totals",
            )
        self._last[key] = value
        self._sum[key] = self._sum.get(key, 0) + value
        return self._sum[key] if self.mode == "delta" else value

    def update(self, usage: dict[str, Any]) -> None:
        self.events += 1
        if self.events <= RAW_USAGE_SAMPLES:
            # Truncated: a diagnostic, not a payload store — an unbounded
            # field is how a log group turns into a bill.
            event(
                "nova.usage.raw",
                sample=self.events,
                payload=json.dumps(usage, default=str)[:2000],
            )

        if usage.get("totalInputTokens") is not None:
            self.total_input = self._apply("in", int(usage["totalInputTokens"] or 0))
        if usage.get("totalOutputTokens") is not None:
            self.total_output = self._apply("out", int(usage["totalOutputTokens"] or 0))

        # Per-modality counts live under details.total.{input,output}. Read
        # defensively: an unexpected shape must degrade to "no modality split"
        # (the totals are still usable) rather than raise inside the model's
        # receive loop and drop a live call.
        try:
            total = (usage.get("details") or {}).get("total") or {}
            for side, suffix in (("input", "in"), ("output", "out")):
                counts = total.get(side) or {}
                if not isinstance(counts, dict):
                    continue
                for key, value in counts.items():
                    lowered = key.lower()
                    if "speech" in lowered or "audio" in lowered:
                        bucket = f"speech_{suffix}"
                    elif "text" in lowered:
                        bucket = f"text_{suffix}"
                    else:
                        continue
                    self.tokens[bucket] = self._apply(bucket, int(value or 0))
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
    """Record a finished call, as **two** log lines.

    ``call.ledger`` is the one to read: every fact about the call, no
    machinery. ``call.metrics`` is the CloudWatch Embedded Metric Format
    event — the ``_aws`` block plus the bare numbers it names — and exists
    only to be parsed.

    They were one line until the ``_aws`` block turned out to dominate it.
    EMF requires that block at the *root* of the same event as the values it
    publishes, and it has to enumerate every metric by name and unit, so a
    ledger event carried ~35 fields of `_aws.CloudWatchMetrics.0.Metrics.N.*`
    around 20 of actual content. Expanding a call in the console meant
    scrolling past the machinery to reach the call, and Logs Insights offered
    those fields for autocomplete ahead of the real ones.

    Splitting costs one extra log line per call and duplicates about ten
    numbers. It buys a ledger row that reads like a record of a phone call.
    The numbers stay on ``call.ledger`` too, so every query, dashboard widget
    and the reconcile script go on working against it alone — only the metric
    *extraction* happens on the other line.

    `outcome` is a metric dimension because it is the cut that actually gets
    asked for: "escalated calls cost 3x and there are more this week" should
    be a dashboard line, not a query.

    CallCostUsd is published **only when it could be computed**. An unpriced
    call still records every token count and `cost_priced: false`; what it
    must not do is publish a $0, which reads as "calls are cheap" rather than
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
        # Which way the provider's figures were read. Reported on every line
        # because a wrong inference here scales the whole cost, and this is
        # the field that makes that visible instead of silent.
        "usage_mode": usage.mode,
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

    # Line 1 — the readable record. No `_aws`, so nothing here exists for
    # CloudWatch's benefit.
    print(json.dumps(body, default=str))

    # Line 2 — the EMF event. Carries the dimensions and only the values it
    # publishes; every descriptive field above is left off, since nothing
    # reads this line.
    emf: dict[str, Any] = {
        "event": "call.metrics",
        "service": SERVICE,
        "outcome": outcome,
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    # Both cuts: the unconditioned total, and the per-outcome
                    # split. A metric is only queryable along dimension sets
                    # declared when it was written, so dropping ["service"]
                    # would mean no way to ask for total spend.
                    "Dimensions": [["service"], ["service", "outcome"]],
                    "Metrics": metrics,
                }
            ],
        },
    }
    for metric in metrics:
        emf[metric["Name"]] = body[metric["Name"]]

    # print, not the logger: EMF requires this object at the root of the log
    # event, and the JSON formatter would nest it inside its own envelope
    # where CloudWatch will not look for it.
    print(json.dumps(emf, default=str))
