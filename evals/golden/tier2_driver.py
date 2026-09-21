#!/usr/bin/env python3
"""Tier 2 golden-set driver — replays cases against a running Ava over the real
`/ws` socket, exactly as the browser does.

Why this works with no change to the agent: `_normalize_caller_audio`
(strands_agent.py) passes any frame that is *not* `bidi_audio_input` straight
through to `BidiAgent.send()`, and `bidi_text_input` is the same event type the
greeting kickoff already uses. So a caller turn can be delivered as text on the
socket the page normally streams PCM over, and Ava's spoken words come back as
`bidi_transcript_stream` frames — the full production path (gates, memory,
gateway tools, Nova Sonic) with the microphone swapped for a script.

What it deliberately does NOT do is stub tools: over this socket the tools are
the real ones. The nine cases whose `turns` carry a stubbed `tool` result are
skipped with a reason — those belong in Tier 1, or need their fixture seeded
into DynamoDB first.

Usage
-----
    # 1. run Ava locally, teeing its stdout so tool calls can be recovered
    uvicorn agentcore_app.strands_agent:app --port 8080 2>&1 | tee /tmp/ava.log

    # 2. drive it
    python evals/golden/tier2_driver.py --server-log /tmp/ava.log

    python evals/golden/tier2_driver.py --only EMP-01,RES-01 --runs 3
    python evals/golden/tier2_driver.py --category empathy,empathy-restraint

Before running, unset SLACK_BOT_TOKEN / SLACK_ESCALATION_CHANNEL in the
server's environment. Twelve of these cases escalate on purpose, and each one
posts to the channel and opens a joinable portal otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

import websockets

HERE = pathlib.Path(__file__).parent
DATASET = HERE / "ava_golden_v1.jsonl"
FIXTURES = HERE / "fixtures.json"
GLOBALS_ = HERE / "global_assertions.json"
RESULTS = HERE / "results"

# How long with no new assistant transcript before a turn is considered
# finished. Ava's replies arrive as several `bidi_transcript_stream` frames and
# only the final one carries is_final, so there is no single "turn over" event
# to wait on -- this is the gap that stands in for one. Too short and a reply
# gets cut in half across two turns; too long and every case pays it 3-4 times.
DEFAULT_IDLE = 2.5

# Hard cap per case. A case that hits this is reported as a timeout rather than
# hanging the run -- usually it means Ava asked something the script has no
# answer for and both sides are waiting.
DEFAULT_CASE_TIMEOUT = 120.0


def load_cases(only: str | None, categories: str | None) -> list[dict]:
    cases = [json.loads(line)
             for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if only:
        wanted = {c.strip().upper() for c in only.split(",")}
        cases = [c for c in cases if c["id"].upper() in wanted]
    if categories:
        wanted = {c.strip() for c in categories.split(",")}
        cases = [c for c in cases if c["category"] in wanted]
    return cases


def needs_tool_stub(case: dict) -> bool:
    return any(turn["from"] == "tool" for turn in case["turns"])


class ReplyCollector:
    """One websocket connection's worth of Ava.

    Frames are drained by a dedicated task into a queue rather than being
    awaited inline. `asyncio.wait_for(ws.recv(), ...)` would cancel the pending
    recv on every idle tick, and the agent's own socket loop documents why that
    is a bad idea (a frame arriving in the same instant is lost). Queue.get is
    safe to cancel; recv is not.
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader = asyncio.create_task(self._drain())
        self.events: list[dict] = []          # every frame, for debugging
        self.interruptions: list[str] = []

    async def _drain(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    self._queue.put_nowait(json.loads(raw))
                except json.JSONDecodeError:
                    pass  # binary/keepalive frames are not our business
        except websockets.exceptions.ConnectionClosed:
            pass

    async def collect_turn(self, idle: float, deadline: float) -> str:
        """Ava's next complete utterance as text, or "" if she stays silent.

        An empty string is a legitimate result, not a failure: DIS-02 asserts
        that a mid-sentence fragment gets no reply at all.
        """
        parts: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("case deadline exceeded")
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=min(idle, remaining))
            except asyncio.TimeoutError:
                return " ".join(parts).strip()

            self.events.append(event)
            kind = event.get("type")
            if kind == "bidi_interruption":
                self.interruptions.append(event.get("reason", "?"))
            elif kind == "bidi_transcript_stream" and event.get("is_final"):
                if event.get("role") == "user":
                    continue  # our own text echoed back as the caller's turn
                text = event.get("current_transcript") or event.get("text") or ""
                if text.strip():
                    parts.append(text.strip())

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._reader
        except asyncio.CancelledError:
            pass


class ServerLog:
    """Recovers the tool calls a case made, by slicing the server's stdout.

    Nothing on the caller socket reveals a tool call -- `to_customer` is only
    used to relay a human agent's audio -- but `_LoggingTool` prints every call
    and result. Taking the file offset before and after a case gives that
    case's tool calls exactly, with no timestamp correlation needed, as long as
    cases run one at a time (they do).
    """

    CALL_RE = re.compile(r"\[tool call\] (\w+)\((.*)\)$")

    def __init__(self, path: str | None) -> None:
        self.path = pathlib.Path(path) if path else None
        self._offset = 0

    def mark(self) -> None:
        if self.path and self.path.exists():
            self._offset = self.path.stat().st_size

    def calls_since_mark(self) -> list[dict] | None:
        """Tool calls since mark(), or None if no log was supplied."""
        if not self.path or not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        calls = []
        for line in chunk.splitlines():
            match = self.CALL_RE.search(line.strip())
            if match:
                calls.append({"name": match.group(1), "raw_args": match.group(2)})
        return calls


async def drive_case(url: str, case: dict, idle: float, case_timeout: float,
                     log: ServerLog) -> dict:
    """Replay one case. Returns the transcript and everything needed to grade it."""
    caller_turns = [t for t in case["turns"] if t["from"] == "caller"]
    deadline = time.monotonic() + case_timeout
    log.mark()

    async with websockets.connect(url, max_size=None) as ws:
        collector = ReplyCollector(ws)
        try:
            # The metadata frame with NO mobile_number, on purpose. Sending one
            # makes the agent inject its "already confirmed, use it for your
            # first lookup without reading it back" override into the kickoff
            # text, which changes the opening sequence and would invalidate
            # REG-01, REG-03 and REG-09 outright. An empty frame also skips the
            # 3s first-message timeout the server otherwise waits out.
            await ws.send(json.dumps({"type": "bidi_call_metadata"}))

            replies = [await collector.collect_turn(idle, deadline)]  # the greeting
            for turn in caller_turns:
                await ws.send(json.dumps({"type": "bidi_text_input", "text": turn["text"]}))
                replies.append(await collector.collect_turn(idle, deadline))

            outcome, error = "ok", None
        except TimeoutError as exc:
            outcome, error = "timeout", str(exc)
        except websockets.exceptions.ConnectionClosed as exc:
            # Nova Sonic dropping the session mid-case shows up here. Worth
            # distinguishing from a timeout: it usually means the run was too
            # slow and the model's own idle timer fired.
            outcome, error = "connection_closed", str(exc)
            replies = locals().get("replies", [])
        finally:
            await collector.close()

    return {
        "id": case["id"],
        "category": case["category"],
        "title": case["title"],
        "outcome": outcome,
        "error": error,
        "greeting": replies[0] if replies else "",
        # Everything after the greeting is what the case is about -- except a
        # case with no caller turns at all (REG-01), where the greeting IS the
        # reply under test.
        "replies": replies[1:] if caller_turns else replies,
        "interruptions": collector.interruptions,
        "tool_calls": log.calls_since_mark(),
    }


def target_reply(result: dict, assert_on: str) -> tuple[str, list[str]]:
    """The text a case's assertions apply to, plus every reply for the globals."""
    replies = result["replies"]
    if assert_on == "all_replies":
        return "\n".join(replies), replies
    if assert_on == "third_reply":
        return (replies[2] if len(replies) > 2 else ""), replies
    return (replies[-1] if replies else ""), replies


def grade(case: dict, result: dict, global_bans: list[dict]) -> dict:
    """Deterministic checks only. Rubric points are passed through for the judge."""
    expect = case.get("expect", {})
    target, replies = target_reply(result, expect.get("assert_on", "final_reply"))
    failures: list[dict] = []

    # Globals apply to the greeting too -- it is a real spoken utterance.
    for utterance in ([result["greeting"], *replies] if result["greeting"] else replies):
        for ban in global_bans:
            if re.search(ban["re"], utterance):
                failures.append({"check": ban["id"], "kind": "global",
                                 "why": ban["why"], "in": utterance})

    if "must_equal" in expect and target.strip() != expect["must_equal"].strip():
        failures.append({"check": "must_equal", "kind": "case",
                         "expected": expect["must_equal"], "got": target})

    for pattern in expect.get("must_not_match", []):
        if re.search(pattern, target):
            failures.append({"check": "must_not_match", "kind": "case",
                             "pattern": pattern, "in": target})

    patterns = expect.get("must_match_any", [])
    if patterns and not any(re.search(p, target) for p in patterns):
        failures.append({"check": "must_match_any", "kind": "case",
                         "patterns": patterns, "in": target})

    # One '?' per reply, the rule most likely to regress after a tone edit.
    for reply in replies:
        if reply.count("?") > 1:
            failures.append({"check": "G-one-question", "kind": "global", "in": reply})

    tool_calls = result.get("tool_calls")
    tool_verdict = "unverified — pass --server-log to check tool assertions"
    if tool_calls is not None:
        called = {call["name"] for call in tool_calls}
        tool_verdict = "checked"
        for name in expect.get("must_call", []):
            if name not in called:
                failures.append({"check": "must_call", "kind": "tool", "tool": name,
                                 "called": sorted(called)})
        for name in expect.get("must_not_call", []):
            if name in called:
                failures.append({"check": "must_not_call", "kind": "tool", "tool": name,
                                 "severity": "unsafe"})
        for arg in expect.get("escalate_args_must_include", []):
            escalations = [c for c in tool_calls if c["name"] == "escalate"]
            if escalations and not any(arg in c["raw_args"] for c in escalations):
                failures.append({"check": "escalate_args_must_include", "kind": "tool",
                                 "arg": arg})

    return {
        **result,
        "tool_check": tool_verdict,
        "deterministic": "fail" if failures or result["outcome"] != "ok" else "pass",
        "failures": failures,
        # Not machine-checkable -- handed to judge_prompt.md with the reply.
        "rubric": expect.get("rubric", []),
        "must_hold": expect.get("must_hold", []),
        "gold": case.get("gold", ""),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--server-log", default=None,
                        help="path to the server's teed stdout; without it, "
                             "must_call/must_not_call cannot be checked")
    parser.add_argument("--only", default=None, help="comma-separated case ids")
    parser.add_argument("--category", default=None, help="comma-separated categories")
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat the whole set N times (judge_prompt.md asks for 3)")
    parser.add_argument("--idle", type=float, default=DEFAULT_IDLE)
    parser.add_argument("--case-timeout", type=float, default=DEFAULT_CASE_TIMEOUT)
    args = parser.parse_args()

    cases = load_cases(args.only, args.category)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    global_bans = json.loads(GLOBALS_.read_text(encoding="utf-8"))["must_not_match"]
    log = ServerLog(args.server_log)
    RESULTS.mkdir(exist_ok=True)

    runnable = [c for c in cases if not needs_tool_stub(c)]
    skipped = [c for c in cases if needs_tool_stub(c)]

    print(f"{len(runnable)} case(s) x {args.runs} run(s) against {args.url}")
    if skipped:
        print(f"{len(skipped)} skipped (need a stubbed tool result — Tier 1): "
              + ", ".join(c["id"] for c in skipped))
    if not args.server_log:
        print("no --server-log: tool assertions will be reported as unverified")
    print()

    graded: list[dict] = []
    for run in range(1, args.runs + 1):
        for case in runnable:
            try:
                result = await drive_case(args.url, case, args.idle, args.case_timeout, log)
            except Exception as exc:  # a connect failure shouldn't end the run
                result = {"id": case["id"], "category": case["category"],
                          "title": case["title"], "outcome": "error", "error": repr(exc),
                          "greeting": "", "replies": [], "interruptions": [],
                          "tool_calls": None}
            row = grade(case, result, global_bans)
            row["run"] = run
            graded.append(row)

            mark = {"pass": "PASS", "fail": "FAIL"}[row["deterministic"]]
            # A case that never ran cleanly reports *why*, not its assertion
            # names -- every assertion fails against an empty transcript, and
            # reading "must_match_any" when the real cause was a refused
            # connection sends you looking at the prompt instead of the server.
            if row["outcome"] != "ok":
                checks = f"{row['outcome']}: {(row.get('error') or '')[:60]}"
            else:
                checks = ", ".join(sorted({f["check"] for f in row["failures"]})) or "-"
            print(f"  [{mark}] {row['id']:<7} {row['title'][:44]:<44} {checks}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / f"tier2_{stamp}.json"
    out.write_text(json.dumps(
        {"url": args.url, "runs": args.runs, "dataset": DATASET.name,
         "skipped": [c["id"] for c in skipped], "results": graded},
        indent=2, ensure_ascii=False), encoding="utf-8")

    failed = [r for r in graded if r["deterministic"] == "fail"]
    unsafe = [f for r in graded for f in r["failures"] if f.get("severity") == "unsafe"]
    print(f"\n{len(graded) - len(failed)}/{len(graded)} passed the deterministic checks")
    if unsafe:
        print(f"{len(unsafe)} UNSAFE failure(s) — a tool fired that must not have")
    print(f"written {out}")
    print("next: run the survivors through judge_prompt.md for the rubric points")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
