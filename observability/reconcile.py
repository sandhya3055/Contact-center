#!/usr/bin/env python
"""Check the per-call cost ledger against what AWS actually billed.

    ./agentcore_app/.venv/bin/python observability/reconcile.py --days 3

The ledger's `CallCostUsd` is an estimate built from two things that can each
be wrong on their own: the token counts Nova reports, and the price table in
`NOVA_SONIC_PRICES_JSON`. This is the only check that catches either.

Two gotchas it handles, both of which silently produce a wrong answer if you
query Cost Explorer by hand:

1. **Credits.** This account's spend is fully offset, so net `UnblendedCost`
   grouped by service reads `-0.00` across the board. Everything here filters
   to `RECORD_TYPE = Usage` to get gross spend.
2. **Cost Explorer lags.** Today and often yesterday are incomplete, so a
   fresh day will always look like the ledger over-reports. The window ends
   two days back by default for that reason.

Not covered, on purpose: AgentCore Runtime compute. That is the larger half
of the bill (roughly 6.5:1 against model tokens here), but it is billed per
vCPU-hour and GB-hour rather than per call, so the ledger makes no claim
about it. It is printed alongside for context only.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json

import boto3

REGION = "us-west-2"
LOG_GROUP = "/aws/bedrock-agentcore/runtimes/ava_voice_agent-TVSWmk7feq-DEFAULT"
# The four usage types the ledger's CallCostUsd is supposed to add up to.
# us-east-1 equivalents are deliberately excluded: those come from local runs
# and evals, which emit no ledger line and so cannot be reconciled.
SONIC_TYPES = [
    "USW2-NovaSonic2.0-speech-input-tokens",
    "USW2-NovaSonic2.0-speech-output-tokens",
    "USW2-NovaSonic2.0-text-input-tokens",
    "USW2-NovaSonic2.0-text-output-tokens",
]
COMPUTE_TYPES = [
    "USW2-Runtime:Consumption-based:vCPU",
    "USW2-Runtime:Consumption-based:Memory",
]


def ledger_by_day(days: int, end: dt.date) -> dict[str, dict]:
    """Sum every call ledger line per UTC day, straight from the log group."""
    logs = boto3.client("logs", region_name=REGION)
    start = end - dt.timedelta(days=days)
    start_ms = int(dt.datetime.combine(start, dt.time.min, dt.timezone.utc).timestamp() * 1000)
    end_ms = int(dt.datetime.combine(end, dt.time.max, dt.timezone.utc).timestamp() * 1000)

    per = collections.defaultdict(lambda: {
        "calls": 0, "cost": 0.0, "unpriced": 0, "no_split": 0, "delta_mode": 0,
        "speech_in": 0, "speech_out": 0, "text_in": 0, "text_out": 0,
    })
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=LOG_GROUP, startTime=start_ms, endTime=end_ms,
        filterPattern='"call.ledger"',
    )
    for page in pages:
        for e in page.get("events", []):
            try:
                row = json.loads(e["message"])
            except ValueError:
                continue
            if row.get("event") != "call.ledger":
                continue
            day = dt.datetime.fromtimestamp(
                e["timestamp"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
            d = per[day]
            d["calls"] += 1
            d["cost"] += float(row.get("CallCostUsd") or 0.0)
            d["unpriced"] += 0 if row.get("cost_priced") else 1
            d["no_split"] += 0 if row.get("modality_split_available") else 1
            d["delta_mode"] += 1 if row.get("usage_mode") == "delta" else 0
            for bucket, key in (("speech_in", "SpeechInTokens"), ("speech_out", "SpeechOutTokens"),
                                ("text_in", "TextInTokens"), ("text_out", "TextOutTokens")):
                d[bucket] += int(row.get(key) or 0)
    return per


def billed_by_day(days: int, end: dt.date, usage_types: list[str]) -> dict[str, float]:
    ce = boto3.client("ce", region_name="us-east-1")
    start = end - dt.timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": (end + dt.timedelta(days=1)).isoformat()},
        Granularity="DAILY", Metrics=["UnblendedCost"],
        Filter={"And": [
            {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            {"Dimensions": {"Key": "USAGE_TYPE", "Values": usage_types}},
        ]},
    )
    return {t["TimePeriod"]["Start"]: float(t["Total"]["UnblendedCost"]["Amount"])
            for t in resp["ResultsByTime"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="days to compare (default 3)")
    ap.add_argument("--through", default=None, metavar="YYYY-MM-DD",
                    help="last day to include (default: two days ago, since "
                         "Cost Explorer lags and a fresh day reads as a drift)")
    args = ap.parse_args()

    end = (dt.date.fromisoformat(args.through) if args.through
           else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2))

    ledger = ledger_by_day(args.days, end)
    billed = billed_by_day(args.days, end, SONIC_TYPES)
    compute = billed_by_day(args.days, end, COMPUTE_TYPES)

    print(f"Nova Sonic model cost — ledger estimate vs billed, through {end}\n")
    print(f"{'day':12} {'calls':>6} {'ledger $':>10} {'billed $':>10} {'drift':>9}   flags")
    total_l = total_b = 0.0
    for day in sorted(set(ledger) | set(billed)):
        d = ledger.get(day, {})
        lc, bc = d.get("cost", 0.0), billed.get(day, 0.0)
        total_l += lc
        total_b += bc
        drift = f"{(lc - bc) / bc * 100:+7.1f}%" if bc else "      n/a"
        flags = []
        if d.get("unpriced"):
            flags.append(f"{d['unpriced']} unpriced")
        if d.get("no_split"):
            flags.append(f"{d['no_split']} no-split")
        if d.get("delta_mode"):
            flags.append(f"{d['delta_mode']} delta-mode")
        print(f"{day:12} {d.get('calls', 0):6} {lc:10.4f} {bc:10.4f} {drift}   "
              f"{', '.join(flags)}")

    print(f"\n{'TOTAL':12} {sum(v['calls'] for v in ledger.values()):6} "
          f"{total_l:10.4f} {total_b:10.4f}", end="")
    if total_b:
        pct = (total_l - total_b) / total_b * 100
        print(f" {pct:+7.1f}%")
        print("\n" + (
            "Within 10% — the price table and token parsing agree with the bill."
            if abs(pct) <= 10 else
            "Off by more than 10%. In order of likelihood: the price table is wrong,\n"
            "`usage_mode` was inferred wrongly, or the modality mapping is off.\n"
            "Check the flags column, then a `nova.usage.raw` sample."))
    else:
        print("\n\nNo billed Nova Sonic usage in this window — nothing to compare against.")

    comp = sum(compute.values())
    print(f"\nFor context, AgentCore Runtime compute over the same window: ${comp:.4f}"
          + (f" ({comp / total_b:.1f}x the model cost)" if total_b else ""))
    print("Not part of the reconciliation — it is billed per vCPU/GB-hour, not per call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
