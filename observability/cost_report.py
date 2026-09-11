#!/usr/bin/env python
"""What this agent actually costs — all of it, not just the model.

    ./agentcore_app/.venv/bin/python observability/cost_report.py
    ./agentcore_app/.venv/bin/python observability/cost_report.py --days 30
    ./agentcore_app/.venv/bin/python observability/cost_report.py --sessions

The per-call ledger prices Nova Sonic tokens, which is around 7% of the bill.
The CloudWatch dashboard adds Runtime compute, which is most of the rest. This
is the whole thing in one table, because the interesting facts live in the
lines neither of those shows — container image storage costing more than a
third of the model spend, eval traffic billed in a different region to
production, and 77% of everything going to memory held by idle sessions.

Cost Explorer is the source, not CloudWatch: there is no metric for most of
this, and CE is the only place that knows what was actually charged.

Three traps this handles, each of which silently produces a wrong number if
you query CE by hand:

1. **Credits.** This account's spend is fully offset, so net UnblendedCost
   grouped by service reads -0.00 everywhere. Everything here filters to
   RECORD_TYPE = Usage for gross spend.
2. **Other projects share the account.** ELB, RDS, WAF and friends are
   nothing to do with this agent. They are separated out rather than dropped,
   so the account total still reconciles and nobody mistakes one for the
   other.
3. **CE lags ~2 days.** The window ends two days back by default; a fresh day
   reads as a collapse in spend.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))

REGION = "us-west-2"
USAGE_LOG_GROUP = "/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS/ava_voice_agent-TVSWmk7feq"
ECR_REPOS = ["ava-voice-agent", "insurance-voice-agent"]

# The classifier and category list live in cost_categories so this report and
# the daily cost_publisher Lambda can never disagree about what a line is.
from cost_categories import CATEGORIES, GB_RATE, VCPU_RATE, classify  # noqa: E402


def fetch(days: int, end: dt.date):
    ce = boto3.client("ce", region_name="us-east-1")
    start = end - dt.timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": (end + dt.timedelta(days=1)).isoformat()},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"},
                 {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    agent = collections.defaultdict(float)
    lines = collections.defaultdict(float)
    other = collections.defaultdict(float)
    for period in resp["ResultsByTime"]:
        for g in period["Groups"]:
            service, usage_type = g["Keys"]
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost <= 0:
                continue
            cat = classify(service, usage_type)
            if cat is None:
                other[service] += cost
            else:
                agent[cat] += cost
                lines[(cat, usage_type)] += cost
    return agent, lines, other, start


def ecr_footprint():
    ecr = boto3.client("ecr", region_name=REGION)
    out = []
    for repo in ECR_REPOS:
        try:
            paginator = ecr.get_paginator("describe_images")
            n = size = 0
            for page in paginator.paginate(repositoryName=repo):
                for img in page["imageDetails"]:
                    n += 1
                    size += img.get("imageSizeInBytes", 0)
            out.append((repo, n, size / 1e9))
        except Exception as exc:
            out.append((repo, -1, 0.0))
    return out


def session_idle(hours: int):
    """Seconds each session stayed billable, vs how long its call lasted.

    The gap between the two is the single biggest cost lever in this system,
    and it exists in no metric — only in AgentCore's own per-second usage log.
    """
    logs = boto3.client("logs", region_name=REGION)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    q = logs.start_query(
        logGroupNames=[USAGE_LOG_GROUP], startTime=now - hours * 3600, endTime=now,
        # The whole dotted path must be one backticked token: Logs Insights
        # flattens nested JSON with dots and these keys contain dots too, so
        # metrics.`agent...` resolves to no field at all — silently.
        queryString=(
            "fields `attributes.session.id` as session_id, "
            "`metrics.agent.runtime.vcpu.hours.used` as v, "
            "`metrics.agent.runtime.memory.gb_hours.used` as g "
            "| stats count(*) as seconds_alive, sum(v) as vcpu_h, sum(g) as gb_h "
            "by session_id | sort seconds_alive desc | limit 20"),
    )["queryId"]
    import time as _t
    for _ in range(30):
        _t.sleep(2)
        res = logs.get_query_results(queryId=q)
        if res["status"] == "Complete":
            break
    else:
        return []
    rows = []
    for row in res["results"]:
        d = {f["field"]: f["value"] for f in row}
        if "session_id" not in d:
            continue
        secs = int(float(d["seconds_alive"]))
        cost = float(d["vcpu_h"]) * VCPU_RATE + float(d["gb_h"]) * GB_RATE
        rows.append((d["session_id"], secs, float(d["gb_h"]), cost))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--through", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--detail", action="store_true", help="show every usage type")
    ap.add_argument("--sessions", action="store_true",
                    help="also query per-session idle time (slow: scans the usage log)")
    ap.add_argument("--session-hours", type=int, default=6)
    args = ap.parse_args()

    end = (dt.date.fromisoformat(args.through) if args.through
           else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2))

    agent, lines, other, start = fetch(args.days, end)
    total = sum(agent.values())

    print(f"\nAva voice agent — gross cost by category, {start} to {end}")
    print("(credits excluded: this account's spend is fully offset, so net cost reads zero)\n")
    print(f"{'category':46} {'USD':>9} {'%':>6}   covered by")
    print("-" * 104)
    for cat, covered in CATEGORIES:
        cost = agent.get(cat, 0.0)
        if cost <= 0 and cat not in agent:
            continue
        pct = cost / total * 100 if total else 0
        print(f"{cat:46} {cost:9.4f} {pct:5.1f}%   {covered}")
        if args.detail:
            for (c, ut), v in sorted(lines.items(), key=lambda kv: -kv[1]):
                if c == cat:
                    print(f"{'':6}{ut:56} {v:9.4f}")
    print("-" * 104)
    print(f"{'TOTAL — this agent':46} {total:9.4f} 100.0%")

    if other:
        other_total = sum(other.values())
        print(f"\n{'Not this agent (other projects in the account)':46} {other_total:9.4f}")
        for svc, cost in sorted(other.items(), key=lambda kv: -kv[1])[:6]:
            if cost > 0.01:
                print(f"{'':6}{svc:56} {cost:9.4f}")
        print(f"{'':6}{'account total':56} {total + other_total:9.4f}")

    # --- what the ledger claims to cover -----------------------------------
    prod = agent.get("Nova Sonic — production (us-west-2)", 0.0)
    print(f"\nCoverage: the per-call ledger prices {prod:.4f} of {total:.4f} "
          f"({prod / total * 100 if total else 0:.1f}%).")
    compute = agent.get("AgentCore Runtime (compute)", 0.0)
    print(f"          the dashboard's compute widget adds {compute:.4f} "
          f"({compute / total * 100 if total else 0:.1f}%).")
    rest = total - prod - compute
    print(f"          {rest:.4f} ({rest / total * 100 if total else 0:.1f}%) is in neither.")

    # --- container images --------------------------------------------------
    ecr_cost = agent.get("Container images (ECR)", 0.0)
    if ecr_cost > 0:
        print(f"\nContainer images — {ecr_cost:.4f} over this window, "
              f"{ecr_cost / prod * 100 if prod else 0:.0f}% of production model spend:")
        for repo, n, gb in ecr_footprint():
            if n < 0:
                print(f"  {repo:28} (could not read — ecr:DescribeImages denied)")
            else:
                print(f"  {repo:28} {n:3} images   {gb:5.2f} GB")
        print("  Every build is kept forever unless a lifecycle policy says otherwise.\n"
              "  Keeping the last 5 would reclaim most of this:\n"
              "    aws ecr put-lifecycle-policy --region us-west-2 --repository-name ava-voice-agent \\\n"
              "      --lifecycle-policy-text '{\"rules\":[{\"rulePriority\":1,\"description\":\"keep last 5\",\n"
              "        \"selection\":{\"tagStatus\":\"any\",\"countType\":\"imageCountMoreThan\",\"countNumber\":5},\n"
              "        \"action\":{\"type\":\"expire\"}}]}'\n"
              "  Check what it would delete first with ecr start-lifecycle-policy-preview.")

    # --- where the compute actually goes -----------------------------------
    if args.sessions:
        rows = session_idle(args.session_hours)
        if rows:
            print(f"\nRuntime sessions in the last {args.session_hours}h — "
                  f"billed for as long as they stay alive, not for call length:")
            print(f"  {'session':38} {'alive':>8} {'GB-hours':>9} {'USD':>8}")
            for sid, secs, gb, cost in rows[:10]:
                print(f"  {sid[:36]:38} {secs // 60:6}m  {gb:9.4f} {cost:8.4f}")
            print("  A one-minute call holding a session for an hour pays for the hour.\n"
                  "  This is the largest cost lever in the system — see the runtime's\n"
                  "  session idle timeout before optimising anything about the model.")
        else:
            print(f"\nNo session usage records in the last {args.session_hours}h.")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
