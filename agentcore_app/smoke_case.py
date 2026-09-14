"""Local smoke test for the case record — no mic, no gateway, no Nova Sonic.

A full voice call exercises the case path, but it needs a browser, a
microphone, the AgentCore Gateway and Nova Sonic in another region, and it
tells you very little when it fails. This drives the same code with a
scripted call instead, so the two things that are actually new can be checked
on their own:

  1. `_CaseFactsTool` pulling the policy/claim/escalation ids out of real
     gateway tool results — through the actual wrapper, not a reimplementation.
  2. `cases.write_case` -> `cases.summarize_and_update` against the real table
     and the real model, for each way a call can end.

Run it from this directory with the venv that runs the agent:

    ./.venv/bin/python smoke_case.py              # write, verify, clean up
    ./.venv/bin/python smoke_case.py --keep       # leave the rows to inspect
    ./.venv/bin/python smoke_case.py --no-write   # ids + prompt only, no AWS writes

Rows are written with a `localtest-` caseId prefix and deleted at the end
unless --keep, so a local run never leaves anything behind that a report
would count as a real call.

This is a development tool, not part of the container — the Dockerfile copies
what it needs explicitly and does not pick this up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from typing import Any

import re
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer

import cases
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse
from strands_agent import _CaseFactsTool

PREFIX = "localtest-"

# "14/09/2026 16:51:20 AEST" — or AEDT in summer, or UTC if the image turns out
# to have no tz database (epoch_to_au says so rather than mislabelling it).
_AU_STAMP = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} (AE[SD]T|UTC)$")


# ----------------------------------------------------------- fake gateway tool


class _StubTool(AgentTool):
    """A gateway tool that returns one canned result, for driving a wrapper."""

    def __init__(self, name: str, payload: dict[str, Any], status: str = "success") -> None:
        super().__init__()
        self._name = name
        self._payload = payload
        self._status = status

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_type(self) -> str:
        return "stub"

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "stub", "inputSchema": {"json": {}}}

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        yield ToolResultEvent({
            "toolUseId": tool_use["toolUseId"],
            "status": self._status,
            "content": [{"json": self._payload}],
        })


async def _run_tool(tool: AgentTool, name: str) -> None:
    async for _ in tool.stream({"toolUseId": uuid.uuid4().hex, "name": name, "input": {}}, {}):
        pass


# Exactly the shapes MCP/mcp_tools.py returns today.
GET_POLICY = {
    "found": True, "has_active": True,
    "policies": [{
        "policy_id": "POL-001", "status": "ACTIVE", "vehicle_reg": "ABC123",
        "holder_name": "Jane Doe", "expiry_date": "2027-01-01",
        "policy_type": "Comprehensive", "currency": "AUD", "open_claims": [],
    }],
}
REGISTER_CLAIM = {"claim_ref": "CLM-20260914-3"}
ESCALATE = {"escalation_id": "esc-7f2a", "status": "queued"}


async def check_id_extraction() -> int:
    """Drive _CaseFactsTool with real tool payloads and report what it kept."""
    print("=" * 68)
    print("1. id extraction through the real _CaseFactsTool")
    print("=" * 68)
    failures = 0

    for label, calls, expected in [
        ("policy lookup only",
         [("get_policy", GET_POLICY)],
         {"policyId": "POL-001"}),
        ("lookup then claim",
         [("get_policy", GET_POLICY), ("register_claim", REGISTER_CLAIM)],
         {"policyId": "POL-001", "claimId": "CLM-20260914-3"}),
        ("lookup then escalate",
         [("get_policy", GET_POLICY), ("escalate", ESCALATE)],
         {"policyId": "POL-001", "escalationId": "esc-7f2a"}),
        ("a failed tool contributes nothing",
         [("register_claim", REGISTER_CLAIM, "error")],
         {}),
    ]:
        facts: dict[str, str] = {}
        for call in calls:
            name, payload = call[0], call[1]
            status = call[2] if len(call) > 2 else "success"
            await _run_tool(_CaseFactsTool(_StubTool(name, payload, status), facts), name)
        ok = facts == expected
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {facts}")
        if not ok:
            print(f"        expected {expected}")
    print()
    return failures


# --------------------------------------------------------------- scripted calls


class ScriptedCall:
    """The parts of handoff.LiveCall that cases.py reads."""

    def __init__(self, label: str, transcript: list[tuple[str, str]], **kw: Any) -> None:
        self.call_id = f"{PREFIX}{label}-{int(time.time())}"
        self.started_at = time.time() - 120
        self.mobile_number = kw.get("mobile_number", "+61400000001")
        self.caller_name = kw.get("caller_name", "Jane Doe")
        self.caller_dob = "1990-01-15"          # must never reach the row
        self.human_label = kw.get("human_label")
        self.escalation = kw.get("escalation")
        self.memory_actor_id = "actor-local"
        self.memory_session_id = "sess-local"
        self.transcript = [{"role": r, "text": t} for r, t in transcript]


CLAIM_CHAT = [
    ("assistant", "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent."),
    ("user", "I had a prang in the car park yesterday, I want to put a claim in."),
    ("assistant", "Got it. Can I take the registration of the vehicle?"),
    ("user", "A B C 1 2 3."),
    ("assistant", "A B C 1 2 3 — is that right?"),
    ("user", "Yep."),
    ("assistant", "Thanks, that's lodged. Your claim reference is C L M dash 2 0 2 6 0 9 1 4 dash 3."),
    ("user", "Great, that's all. Thanks."),
]
ANGRY_CHAT = [
    ("assistant", "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent."),
    ("user", "This is the third time I've called about my premium going up and nobody has called me back."),
    ("assistant", "I'm not able to sort that myself, so I'll pass this to one of my colleagues."),
    ("user", "Fine, put me through."),
]
DROPPED_CHAT = [
    ("assistant", "Hi, I'm Ava, your AI Assistant. I can help with your policy details, register a claim, or connect you with a human agent."),
    ("user", "Yeah hi, I wanted to check when my policy runs out—"),
]

# The four values here are the ledger's `outcome` dimension and the dashboard's
# by-outcome widgets, verbatim. Note the `queued` scenario: a caller who asked
# for a human nobody joined now derives `completed`, because escalation is no
# longer an outcome — `escalationId` is what marks it, and the assertion below
# is the query a contact centre would actually run to find those calls.
SCENARIOS = [
    # label, call, outcome, human_handoff, ids, error, expected derived
    ("completed", ScriptedCall("completed", CLAIM_CHAT), "completed", False,
     {"policyId": "POL-001", "claimId": "CLM-20260914-3"}, None, "completed"),
    ("hangup", ScriptedCall("hangup", DROPPED_CHAT), "caller_disconnected", False,
     {}, None, "caller_disconnected"),
    ("queued", ScriptedCall("queued", ANGRY_CHAT,
                            escalation={"escalation_id": "esc-7f2a"}),
     "completed", False, {"policyId": "POL-001", "escalationId": "esc-7f2a"}, None, "completed"),
    ("takeover", ScriptedCall("takeover", ANGRY_CHAT,
                              escalation={"escalation_id": "esc-7f2a"}, human_label="Priya"),
     "completed", True, {"policyId": "POL-001", "escalationId": "esc-7f2a"}, None, "human_handoff"),
    ("error", ScriptedCall("error", DROPPED_CHAT), "error", False,
     {}, "RuntimeError: model stream closed", "error"),
]


async def run_scenarios(write: bool) -> tuple[int, list[str]]:
    print("=" * 68)
    print(f"2. a case for each way a call ends  ({'writing to ' + cases.CASES_TABLE if write else 'DRY RUN — no AWS writes'})")
    print("=" * 68)
    failures = 0
    written: list[str] = []

    for label, call, outcome, handoff, ids, error, expect in SCENARIOS:
        derived_local = cases._derived_status(
            call_outcome=outcome, human_handoff=handoff)
        ok = derived_local == expect
        failures += not ok
        print(f"\n  [{label}] derived={derived_local} {'' if ok else f'(expected {expect}) FAIL'}")

        if not write:
            item = cases.build_case(
                call=call, call_outcome=outcome, duration_seconds=120.0,
                human_handoff=handoff,
                mobile_number=call.mobile_number, ids=ids, error=error)
            print("        row:", json.dumps(
                {k: v for k, v in item.items() if v not in ("", None)}, default=str)[:300], "…")
            continue

        derived = await cases.write_case(
            call=call, call_outcome=outcome, duration_seconds=120.0,
            human_handoff=handoff,
            mobile_number=call.mobile_number, ids=ids, error=error)
        if derived is None:
            print("        FAIL write_case returned None — check the log event above")
            failures += 1
            continue
        written.append(call.call_id)
        await cases.summarize_and_update(call, derived)

    print()
    return failures, written


def verify(case_ids: list[str]) -> int:
    print("=" * 68)
    print("3. reading the rows back")
    print("=" * 68)
    ddb = boto3.client("dynamodb", region_name=cases.CASES_REGION)
    deserialize = TypeDeserializer().deserialize
    failures = 0

    for case_id in case_ids:
        item = ddb.get_item(TableName=cases.CASES_TABLE,
                            Key={"caseId": {"S": case_id}}).get("Item")
        if not item:
            print(f"  FAIL  {case_id}: no row")
            failures += 1
            continue
        row = {k: deserialize(v) for k, v in item.items()}
        blob = json.dumps(row, default=str)
        checks = [
            ("dob absent", "1990-01-15" not in blob),
            ("intent in set", row.get("customerIntent") in cases.CUSTOMER_INTENTS),
            ("status in set", row.get("resolutionStatus") in cases.RESOLUTION_STATUSES),
            ("status matches ledger vocabulary",
             row.get("resolutionStatus") in cases.RESOLUTION_STATUSES),
            ("status is the derived one, not the model's", row.get("resolutionStatus")
             == cases._derived_status(call_outcome=row.get("callOutcome", ""),
                                      human_handoff=bool(row.get("humanHandoff")))),
            ("createdAtAu is AU-formatted", _AU_STAMP.match(str(row.get("createdAtAu", "")))),
            ("startedAtAu is AU-formatted", _AU_STAMP.match(str(row.get("startedAtAu", "")))),
            ("createdAt still numeric/sortable", isinstance(row.get("createdAt"), Decimal)),
            ("memorySessionId gone", "memorySessionId" not in row),
            ("escalated gone", "escalated" not in row),
            ("summaryModelId gone", "summaryModelId" not in row),
            ("summary settled", row.get("summaryStatus") in ("ok", "skipped", "failed")),
        ]
        bad = [name for name, passed in checks if not passed]
        failures += len(bad)
        print(f"\n  {'FAIL' if bad else 'PASS'}  {case_id}")
        print(f"        intent   : {row.get('customerIntent')} — {row.get('customerIntentDetail', '')}")
        queued_never_joined = (bool(row.get("escalationId"))
                               and row.get("resolutionStatus") != "human_handoff")
        print(f"        status   : {row.get('resolutionStatus')}"
              f"{'   <-- asked for a human, never got one' if queued_never_joined else ''}")
        print(f"        when     : {row.get('startedAtAu')} -> {row.get('createdAtAu')}")
        print(f"        summary  : {row.get('resolutionSummary', '')}")
        print(f"        ids      : policy={row.get('policyId', '-')} claim={row.get('claimId', '-')} esc={row.get('escalationId', '-')}")
        if bad:
            print(f"        failed   : {', '.join(bad)}")
    print()
    return failures


def cleanup(case_ids: list[str]) -> None:
    ddb = boto3.client("dynamodb", region_name=cases.CASES_REGION)
    for case_id in case_ids:
        try:
            ddb.delete_item(TableName=cases.CASES_TABLE, Key={"caseId": {"S": case_id}})
        except Exception as exc:
            print(f"  could not delete {case_id}: {type(exc).__name__}: {exc}")
    print(f"cleaned up {len(case_ids)} test row(s).")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the test rows on the table to inspect")
    parser.add_argument("--no-write", action="store_true",
                        help="skip every AWS write; just show ids and the rows that would be written")
    args = parser.parse_args()
    write = not args.no_write

    print(f"\ntable   : {cases.CASES_TABLE} ({cases.CASES_REGION})")
    print(f"model   : {cases.CASE_SUMMARY_MODEL_ID or '(summarization disabled)'} ({cases.CASE_SUMMARY_REGION})\n")

    failures = await check_id_extraction()
    scenario_failures, written = await run_scenarios(write)
    failures += scenario_failures
    if written:
        failures += verify(written)
        if args.keep:
            print(f"kept {len(written)} row(s): {', '.join(written)}")
        else:
            cleanup(written)

    print("\n" + "=" * 68)
    print(f"{'FAILURES: ' + str(failures) if failures else 'ALL CHECKS PASSED'}")
    print("=" * 68 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
