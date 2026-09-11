#!/usr/bin/env python
"""Point the AgentCore Runtime at a new image, without losing its settings.

    ./.venv/bin/python deploy.py --tag v18
    ./.venv/bin/python deploy.py --tag v18 --set NOVA_SONIC_PRICES_JSON='{...}'
    ./.venv/bin/python deploy.py --tag v18 --unset OTEL_EXPORTER_OTLP_ENDPOINT
    ./.venv/bin/python deploy.py --tag v18 --dry-run

Why this exists rather than a documented `aws` one-liner:

1. **`update-agent-runtime` replaces, it does not merge.** Every environment
   variable, the role, the network mode and the protocol have to be passed
   back on every update, and anything omitted is silently deleted. Omitting
   `SLACK_BOT_TOKEN` doesn't fail the deploy — escalations just stop reaching
   Slack, days later, for no visible reason. This reads the live config and
   changes only what you name.
2. **The AWS CLI can't do it.** `bedrock-agentcore-control` doesn't exist in
   aws-cli 2.25.8, which is what's installed here. The venv's boto3 has it.
3. **Runtimes pin by digest, not tag.** Pushing over a tag deploys nothing;
   this resolves the tag to the digest it currently points at and pins that,
   so a rollback is a digest you can still name.

Nothing is written until you've seen the diff and confirmed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import boto3

REGION = "us-west-2"
RUNTIME_ID = "ava_voice_agent-TVSWmk7feq"
REPO = "ava-voice-agent"
# Values worth not printing in full to a shared terminal or a CI log.
SECRET_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD")


def redact(key: str, value: str) -> str:
    if any(hint in key.upper() for hint in SECRET_HINTS):
        return f"{value[:6]}…({len(value)} chars)"
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True, help="ECR image tag to deploy, e.g. v18")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="add or overwrite one environment variable (repeatable)")
    ap.add_argument("--unset", action="append", default=[], metavar="KEY",
                    help="delete one environment variable (repeatable)")
    ap.add_argument("--runtime-id", default=RUNTIME_ID)
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the diff and exit without changing anything")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    ecr = boto3.client("ecr", region_name=args.region)
    control = boto3.client("bedrock-agentcore-control", region_name=args.region)

    # --- resolve the tag to the digest it points at right now ---------------
    images = ecr.describe_images(repositoryName=REPO,
                                 imageIds=[{"imageTag": args.tag}])["imageDetails"]
    digest = images[0]["imageDigest"]
    media = images[0].get("imageManifestMediaType", "")
    if "list" in media or "index" in media:
        # buildx with attestations on pushes an image *index*, whose digest is
        # not the arm64 manifest's. AgentCore wants the platform manifest.
        print(f"ERROR: {args.tag} is an image index ({media}).\n"
              f"       Rebuild with --provenance=false.", file=sys.stderr)
        return 1

    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    container_uri = f"{account}.dkr.ecr.{args.region}.amazonaws.com/{REPO}@{digest}"

    # --- read the live config; this is the part that must not be guessed ----
    live = control.get_agent_runtime(agentRuntimeId=args.runtime_id)
    env = dict(live.get("environmentVariables") or {})
    current_uri = live["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]

    if current_uri == container_uri and not args.set and not args.unset:
        print(f"Runtime is already on {digest[:19]} and no env changes requested — nothing to do.")
        return 0

    changes: list[str] = []
    if current_uri != container_uri:
        changes.append(f"  image   {current_uri.split('@')[-1][:26]}\n"
                       f"       -> {digest[:26]}  ({args.tag})")
    for pair in args.set:
        if "=" not in pair:
            print(f"ERROR: --set expects KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 1
        key, value = pair.split("=", 1)
        before = env.get(key)
        env[key] = value
        verb = "set" if before is None else "change"
        changes.append(f"  {verb:7} {key} = {redact(key, value)}"
                       + (f"   (was {redact(key, before)})" if before is not None else ""))
    for key in args.unset:
        if key in env:
            changes.append(f"  delete  {key}   (was {redact(key, env[key])})")
            del env[key]
        else:
            print(f"  note: {key} is not set on the runtime; nothing to unset")

    print(f"\nRuntime {args.runtime_id} (version {live.get('agentRuntimeVersion')}, "
          f"status {live.get('status')})")
    print("\nChanges:")
    print("\n".join(changes) or "  (none)")
    print(f"\nCarried over unchanged: {len(env) - len(args.set)} environment variables, "
          f"role, network ({live['networkConfiguration']['networkMode']}), "
          f"protocol ({live['protocolConfiguration']['serverProtocol']}).")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not args.yes:
        if input("\nApply? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    control.update_agent_runtime(
        agentRuntimeId=args.runtime_id,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": container_uri}},
        roleArn=live["roleArn"],
        networkConfiguration=live["networkConfiguration"],
        protocolConfiguration=live["protocolConfiguration"],
        environmentVariables=env,
    )

    print("\nWaiting for READY", end="", flush=True)
    for _ in range(60):
        time.sleep(5)
        status = control.get_agent_runtime(agentRuntimeId=args.runtime_id).get("status")
        print(".", end="", flush=True)
        if status == "READY":
            print(f"\nREADY — now on {digest[:26]} ({args.tag})")
            print("\nConfirm the container came up:")
            print(f"  aws logs tail /aws/bedrock-agentcore/runtimes/{args.runtime_id}-DEFAULT \\\n"
                  f"    --region {args.region} --since 5m --format short")
            return 0
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING"):
            print(f"\nFAILED: status={status}")
            return 1
    print("\nTimed out waiting for READY; check the console.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
