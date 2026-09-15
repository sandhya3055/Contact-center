#!/usr/bin/env python
"""Publish yesterday's real AWS spend into CloudWatch, once a day.

Runs as a Lambda on an EventBridge schedule; also runnable by hand:

    ./agentcore_app/.venv/bin/python observability/cost_publisher.py --dry-run
    ./agentcore_app/.venv/bin/python observability/cost_publisher.py --backfill 14

A CloudWatch dashboard can only render CloudWatch metrics and Logs Insights
queries — there is no widget that talks to Cost Explorer. So most of what this
agent costs (container image storage, AgentCore Memory, eval traffic, portal
hosting) could not appear on the dashboard at all, no matter how it was
configured. This closes that gap by turning each CE category into a real
metric under `Ava/Cost`, which a widget *can* chart and an alarm *can* watch.

`AWS/Billing EstimatedCharges` is not an alternative: it is account-wide, and
this account also hosts an unrelated ELB/RDS/WAF stack that is roughly three
times larger than the agent. Charting it would answer a question nobody asked.

**The chart's x-axis is when the figure was learned, not when the money was
spent, and the two differ by COST_LAG_DAYS.** That is a workaround for a hard
constraint, not a design choice: backdating was tried first and silently does
not work here. PutMetricData accepts a timestamp up to two weeks old and
returns success, the metric shows up in ListMetrics — and GetMetricStatistics
returns nothing for it, forever. Verified with a probe: same call, same
dimensions, current timestamp reads back in under a minute, three days back
never reads back at all. So every point is written at publish time and the
`cost_date` it describes is logged alongside.

Consequences worth knowing before reading the dashboard:

* A spike on the chart means spend spiked ~3 days earlier.
* **History cannot be backfilled.** The series starts the first time this
  runs and grows one point per day. For anything before that, use
  `cost_report.py`, which queries Cost Explorer directly and can see the
  whole retention window.
* It publishes for a day Cost Explorer has settled on. Today and usually
  yesterday are still moving, so the default lag is three days and a figure
  is written once rather than revised.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cost_categories import CATEGORIES, METRIC_NAMES, classify  # noqa: E402

NAMESPACE = os.getenv("COST_NAMESPACE", "Ava/Cost")
SERVICE = os.getenv("SERVICE_NAME", "ava-voice-agent")
ECR_REPOS = [r for r in os.getenv("ECR_REPOS", "ava-voice-agent,insurance-voice-agent").split(",") if r]
# How many days back to treat as final. CE is usually settled by 2; 3 buys a
# margin so the number written is the number that stays.
LAG_DAYS = int(os.getenv("COST_LAG_DAYS", "3"))


def daily_costs(start: dt.date, end: dt.date) -> dict[dt.date, dict[str, float]]:
    """Gross spend per day per category, plus the not-ours total.

    RECORD_TYPE = Usage because this account's spend is fully offset by
    credits: the net figure grouped by service is -0.00 across the board, so
    a publisher without this filter would faithfully chart zero forever.
    """
    ce = boto3.client("ce", region_name="us-east-1")
    out: dict[dt.date, dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": (end + dt.timedelta(days=1)).isoformat()},
            Granularity="DAILY", Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"},
                     {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp["ResultsByTime"]:
            day = dt.date.fromisoformat(period["TimePeriod"]["Start"])
            for g in period["Groups"]:
                service, usage_type = g["Keys"]
                cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if cost <= 0:
                    continue
                cat = classify(service, usage_type)
                out[day]["__other__" if cat is None else cat] += cost
        token = resp.get("NextPageToken")
        if not token:
            break
    return out


def ecr_footprint() -> tuple[int, float]:
    """Image count and gigabytes across the agent's repositories.

    Published alongside cost because the dollar figure alone doesn't tell you
    why it is rising — image storage only ever grows, one build at a time,
    and the count is the number that makes the cause obvious.
    """
    ecr = boto3.client("ecr", region_name=os.getenv("AWS_REGION", "us-west-2"))
    images = 0
    size = 0
    for repo in ECR_REPOS:
        try:
            for page in ecr.get_paginator("describe_images").paginate(repositoryName=repo):
                for img in page["imageDetails"]:
                    images += 1
                    size += img.get("imageSizeInBytes", 0)
        except Exception as exc:  # a repo that's gone shouldn't fail the run
            print(f"[cost_publisher] skipping ECR repo {repo}: {exc}")
    return images, size / 1e9


def build_metrics(day: dt.date, cats: dict[str, float], stamp: dt.datetime) -> list[dict]:
    """Datapoints for one cost day, stamped `stamp` (publish time, not `day`).

    See the module docstring: backdating to `day` is accepted by the API and
    then unreadable, so the timestamp has to be now.
    """
    data: list[dict] = []
    agent_total = 0.0
    for cat, _covered in CATEGORIES:
        cost = cats.get(cat, 0.0)
        agent_total += cost
        data.append({
            "MetricName": "DailyCostUsd", "Timestamp": stamp, "Value": round(cost, 6),
            "Unit": "None",
            "Dimensions": [{"Name": "service", "Value": SERVICE},
                           {"Name": "category", "Value": METRIC_NAMES[cat]}],
        })
    # The agent's own total, on the service dimension alone, so a widget can
    # ask "what did this agent cost" without summing ten series by hand.
    data.append({
        "MetricName": "DailyCostUsd", "Timestamp": stamp, "Value": round(agent_total, 6),
        "Unit": "None", "Dimensions": [{"Name": "service", "Value": SERVICE}],
    })
    # Everything else in the account, kept as its own series rather than
    # dropped, so the dashboard can show the agent as a share of the bill.
    data.append({
        "MetricName": "DailyCostUsd", "Timestamp": stamp,
        "Value": round(cats.get("__other__", 0.0), 6), "Unit": "None",
        "Dimensions": [{"Name": "service", "Value": "other-projects"}],
    })
    return data


def put(cw, data: list[dict]) -> None:
    # PutMetricData takes at most 1000 datapoints, and far fewer bytes in
    # practice; 20 is comfortably inside every limit and keeps a partial
    # failure small.
    for i in range(0, len(data), 20):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 20])


def run(backfill_days: int = 1, dry_run: bool = False) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date()
    end = today - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=backfill_days - 1)

    if backfill_days > 1:
        print("[cost_publisher] NOTE: every day in a multi-day run lands on the same "
              "timestamp, so only the last survives. Backfill is not possible — see the "
              "module docstring. Use cost_report.py for history.")

    per_day = daily_costs(start, end)
    cw = boto3.client("cloudwatch", region_name=os.getenv("AWS_REGION", "us-west-2"))

    # One publish instant for the whole run, so every category in a run
    # shares an x position and a stacked chart adds up per point.
    stamp = dt.datetime.now(dt.timezone.utc)
    published = 0
    for day in sorted(per_day):
        data = build_metrics(day, per_day[day], stamp)
        total = sum(d["Value"] for d in data
                    if d["Dimensions"][-1].get("Value") == SERVICE and len(d["Dimensions"]) == 1)
        print(f"[cost_publisher] cost_date={day} published_at={stamp:%Y-%m-%d %H:%M}Z "
              f"({len(data)} datapoints, agent total ${total:.4f})")
        if not dry_run:
            put(cw, data)
        published += len(data)

    images, gb = ecr_footprint()
    ecr_data = [
        {"MetricName": "EcrImages", "Timestamp": stamp, "Value": images, "Unit": "Count",
         "Dimensions": [{"Name": "service", "Value": SERVICE}]},
        {"MetricName": "EcrStorageGB", "Timestamp": stamp, "Value": round(gb, 4), "Unit": "Gigabytes",
         "Dimensions": [{"Name": "service", "Value": SERVICE}]},
    ]
    print(f"[cost_publisher] ECR: {images} images, {gb:.2f} GB")
    if not dry_run:
        put(cw, ecr_data)

    return {"days": len(per_day), "datapoints": published + len(ecr_data),
            "ecr_images": images, "ecr_gb": round(gb, 2), "dry_run": dry_run}


def lambda_handler(event, context):  # noqa: ANN001
    """EventBridge entry point. `backfill` in the event overrides the default."""
    return run(backfill_days=int((event or {}).get("backfill", 1)), dry_run=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", type=int, default=1,
                    help="how many days to publish, ending COST_LAG_DAYS back (default 1)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(run(backfill_days=args.backfill, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
