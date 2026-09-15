"""The case record: what a finished call leaves behind for the business to act on.

A ledger line (observability.emit_call_ledger) answers "what did this call cost
and how did it end". A case answers the other question — "what did the caller
want, and did they get it" — in fields a contact centre can sort, assign and
report on rather than by reading a transcript.

Written at teardown, from the one `finally` in strands_agent.serve that every
ending goes through: the caller closing the tab, Ava calling `end_call`, a human
agent taking the call over, or the connection failing. There is no separate
"agent disconnected" path to hook — they all land there.

The write is deliberately in two halves, in this order:

1. `write_case` — every fact the machinery already knows (who called, which
   policy and claim came out of a completed lookup, how the call ended). One
   PutItem, tens of milliseconds, no model involved. This runs immediately
   after the ledger line for exactly the reason the ledger runs first: the
   container can be killed during teardown (see `_bounded_teardown`), and a
   case that records only the hard facts is worth far more than no case.

2. `summarize_and_update` — a Bedrock Converse call over the transcript that
   fills in `customerIntent`, `resolutionSummary` and `resolutionStatus`, then
   UpdateItems them onto the row from step 1. This runs last in teardown,
   after the socket and the model session are closed, because it is the only
   part that takes seconds and nobody is waiting on it — the caller has
   already gone.

Neither half ever raises: a case is a record *about* a call, and it must not
become the reason a call's teardown fails. Both report what went wrong as one
structured event and return.

The split runs along what each half can actually know. The model is asked only
what the words show — what the caller wanted, what was done, and whether they
left with anything outstanding. It is never asked to name a `resolutionStatus`,
because three of those four are facts about the connection. It does decide one
thing: a caller who gets their answer and hangs up satisfied closes the socket
identically to one who gives up, so `callerNeedMet` turns `caller_disconnected`
into `completed`. That is the only transition, and it only ever goes up.

`resolutionStatus` uses the same four values, spelled the same way, as the call
ledger's `outcome` dimension and the dashboard's two by-outcome widgets. The
vocabulary is shared; the per-call answer can differ, because the ledger asks
how the connection ended and this asks how the caller's need ended. `callOutcome`
keeps the raw connection fact on the row either way.

Timestamps are Sydney-local strings, matching the rest of this project —
`startedAt` and `createdAt` both read as `14/09/2026 17:27:56 AEST`.
`createdAtEpoch` carries the same instant as a raw epoch and is the range key on
both GSIs, because an index has to sort and an AU string neither sorts
chronologically nor runs monotonically across the AEDT->AEST change.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import portal_context
from observability import event

if TYPE_CHECKING:  # imported for typing only — handoff doesn't import this module
    from handoff import LiveCall

# The table this writes. Created out of band like every other table here
# (policies, claims, escalations) — see DEPLOY.md for the create-table call and
# the IAM statement the agent's role needs.
CASES_TABLE = os.getenv("CASES_TABLE", "cases")
CASES_REGION = os.getenv("CASES_REGION") or os.getenv("AWS_REGION", "us-west-2")

# The model that reads the transcript. Any Bedrock model id the Converse API
# accepts works — Converse normalizes the request shape, so swapping Nova Lite
# for Claude on Bedrock is an env var and no code change. Set it empty to turn
# summarization off entirely and keep only the deterministic row.
#
# The default is an inference-profile id (`us.` prefix), not the bare model id:
# Nova is only invocable through a cross-region profile in most regions, and
# the bare id fails there with a ValidationException that reads like the model
# doesn't exist.
CASE_SUMMARY_MODEL_ID = os.getenv("CASE_SUMMARY_MODEL_ID", "us.amazon.nova-lite-v1:0")

# Deliberately *not* BEDROCK_REGION — that one is Nova Sonic's region, which is
# a different service in a different region (us-east-1 here) and has nothing to
# do with where a text model is invocable. Same trap documented on
# strands_agent._prompt_region.
CASE_SUMMARY_REGION = os.getenv("CASE_SUMMARY_REGION") or os.getenv("AWS_REGION", "us-west-2")

CASE_WRITE_TIMEOUT = float(os.getenv("CASE_WRITE_TIMEOUT_SECONDS", "5"))
CASE_SUMMARY_TIMEOUT = float(os.getenv("CASE_SUMMARY_TIMEOUT_SECONDS", "20"))

# How long a case row lives, in days. 0 (the default) means no expiry — a case
# is a business record, unlike the escalations queue row it outlives, so it
# only gets a TTL if someone deliberately asks for one.
CASE_TTL_DAYS = int(os.getenv("CASE_TTL_DAYS", "0"))

# How much of the conversation the summarizer is shown, newest turns kept. A
# long call is mostly readbacks and confirmations; the end of it is where the
# outcome is. Also what bounds the cost of this call.
CASE_TRANSCRIPT_TURNS = int(os.getenv("CASE_TRANSCRIPT_TURNS", "60"))
_MAX_LINE_CHARS = 400

# ------------------------------------------------------------- AU datetimes
#
# Sydney-local display strings, the same shape every other record in this
# project stores (`epoch_to_au` in MCP/app/data/models.py). Reimplemented here
# rather than imported: the container ships five Python files (see the
# Dockerfile's COPY line) and MCP/ is not among them — agentcore_app and MCP/
# are separate deployables that share no code.

_AU_DATETIME_FMT = "%d/%m/%Y %H:%M:%S %Z"
_AU_ZONE = "Australia/Sydney"
_tz_cache: list[Any] = []


def _au_timezone():
    """The Sydney zone, resolved once, or None if this image has no tz database.

    Deliberately lazy and allowed to fail. The base image is `python:3.13-slim`,
    and the zoneinfo database is one of the things a Debian slim image may not
    carry; `tzdata` is not in `requirements.lock.txt`. A module-level
    `ZoneInfo("Australia/Sydney")` would then raise `ZoneInfoNotFoundError` at
    *import* — killing the container before /ping ever answers, which is exactly
    the failure mode the Dockerfile's COPY comment warns about, and here it
    would be for the sake of a display string.

    If `case.timezone_unavailable` ever appears in the log, the fix is to add
    `tzdata` to requirements.txt and regenerate the lock (instructions in
    requirements.lock.txt's header).
    """
    if not _tz_cache:
        try:
            from zoneinfo import ZoneInfo

            _tz_cache.append(ZoneInfo(_AU_ZONE))
        except Exception as exc:
            event("case.timezone_unavailable", zone=_AU_ZONE,
                  error=f"{type(exc).__name__}: {exc}",
                  impact="case timestamps fall back to UTC; add tzdata to "
                         "requirements.txt and regenerate requirements.lock.txt")
            _tz_cache.append(None)
    return _tz_cache[0]


def epoch_to_au(epoch_seconds: float) -> str:
    """An epoch as a Sydney-local string, e.g. ``"14/09/2026 16:51:20 AEST"``.

    Falls back to UTC — *labelled* UTC, never silently mislabelled as AEST —
    when the image has no tz database. A timestamp in the wrong zone that claims
    to be Sydney is worse than one that says which zone it is really in, and the
    `%Z` suffix is what keeps that honest.

    Never sort on this. ``DD/MM/YYYY`` is not lexicographically chronological,
    and Sydney's clock falls back an hour at the AEDT->AEST transition, so it is
    not monotonic either. That is why ``createdAt`` stays a raw epoch Number:
    it is the range key on both of this table's GSIs.
    """
    zone = _au_timezone()
    return datetime.fromtimestamp(epoch_seconds, tz=zone or timezone.utc).strftime(
        _AU_DATETIME_FMT)


# ---------------------------------------------------------------- vocabulary
#
# Closed sets, because these two fields exist to be counted. A free-text intent
# gives you a thousand one-row groups and no report; the model's own words are
# kept alongside in `customerIntentDetail`, which is where the nuance goes.

# The *same four values*, spelled the same way, as the ledger's `outcome`
# dimension and the dashboard's two by-outcome widgets — one vocabulary for
# "how did this call end" across all three.
#
# The values match; the per-call answer can differ, and that difference is the
# point. The ledger asks how the *connection* ended, which it settles before
# any model runs. The case asks how the *caller's need* ended, which sometimes
# only the transcript knows: someone who got their answer and hung up satisfied
# leaves a closed socket that looks identical to an abandoned call.
#
# So exactly one transition is delegated to the model, and only ever upward:
# `caller_disconnected` -> `completed`, when the transcript shows the question
# was answered with nothing outstanding (see _RESOLVED_UPGRADE_FROM). `error`
# and `human_handoff` are never model-writable — whether a process raised or a
# human pressed Join is not visible in anything that was said. `callOutcome`
# keeps the raw connection fact either way, so an upgraded row still shows
# `callOutcome: caller_disconnected` next to `resolutionStatus: completed` and
# a reader can see both.
RESOLUTION_STATUSES = (
    "completed",             # the caller got what they came for
    "caller_disconnected",   # the caller hung up with something still open
    "human_handoff",         # a human agent took the call over
    "error",                 # the call itself broke
)

# The only status the transcript is allowed to change, and the only one it can
# be changed to. Anything else stays exactly as the machinery derived it.
_RESOLVED_UPGRADE_FROM = "caller_disconnected"
_RESOLVED_UPGRADE_TO = "completed"

CUSTOMER_INTENTS = (
    "policy_enquiry",
    "claim_registration",
    "claim_status",
    "renewal",
    "complaint",
    "human_agent_request",
    "unknown",
)


def _derived_status(*, call_outcome: str, human_handoff: bool) -> str:
    """How this call ended, in the ledger's own words.

    Intentionally the same two lines strands_agent's teardown runs to pick the
    ledger's `outcome` dimension, so the two can never drift: a human taking
    the call over wins, because that is what actually happened to the caller,
    and otherwise `call_outcome` already is the answer ("completed",
    "caller_disconnected" or "error").

    Escalation is not an outcome here. Whether a human was ever *queued for* is
    a fact about the queue rather than about how the connection ended, and it
    is still on the row as `escalationId` — present exactly when the call
    escalated. `escalationId` set with a status other than `human_handoff` is
    the query for "the caller asked for a person and never got one".
    """
    if human_handoff:
        return "human_handoff"
    return call_outcome if call_outcome in RESOLUTION_STATUSES else "completed"


# ------------------------------------------------------------- DynamoDB glue


def _attr(value: Any) -> dict[str, Any] | None:
    """One Python value as a DynamoDB AttributeValue, or None to leave it off.

    Hand-rolled rather than `boto3.dynamodb.types.TypeSerializer` because that
    one raises on a plain float (it insists on Decimal), and every number here
    — a duration, a timestamp — is naturally a float. The low-level client is
    what handoff._write_portal_row already uses, so the shape matches.

    Empty and None are omitted rather than written: an absent attribute reads
    as "this call had no policy", where an empty string reads as a policy id
    that is the empty string, and only one of those is true.

    `bool` is checked before `int` on purpose — in Python `bool` *is* an `int`,
    so the obvious ordering silently writes `true` as the number 1.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": f"{value}"}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (list, tuple)):
        members = [m for m in (_attr(v) for v in value) if m is not None]
        return {"L": members} if members else None
    if isinstance(value, dict):
        members = {k: m for k, m in ((k, _attr(v)) for k, v in value.items()) if m is not None}
        return {"M": members} if members else None
    return {"S": str(value)}


def _put_case(item: dict[str, Any]) -> None:
    """Write the case row (blocking; call via asyncio.to_thread).

    PutItem rather than UpdateItem: this is the first write for this caseId and
    it owns every field it sets. It is also an idempotent upsert — a redelivery
    or a retry rewrites the same row rather than creating a second case for one
    call, which is what keying on the call id buys.
    """
    attributes = {key: _attr(value) for key, value in item.items()}
    portal_context.client("dynamodb", CASES_REGION).put_item(
        TableName=CASES_TABLE,
        Item={key: value for key, value in attributes.items() if value is not None},
    )


def _update_case(case_id: str, fields: dict[str, Any]) -> None:
    """Merge the summarized fields onto an existing case (blocking).

    Conditional on the row existing, for the same reason
    handoff._write_portal_row is: UpdateItem creates the item when it's
    missing, and a row holding a summary with no customer, no policy and no
    outcome is a phantom case that every report would count. If the
    deterministic write didn't land, this must fail rather than invent one.
    """
    attributes = {key: _attr(value) for key, value in fields.items()}
    attributes = {key: value for key, value in attributes.items() if value is not None}
    if not attributes:
        return
    expression = ", ".join(f"#{key} = :{key}" for key in attributes)
    portal_context.client("dynamodb", CASES_REGION).update_item(
        TableName=CASES_TABLE,
        Key={"caseId": {"S": case_id}},
        UpdateExpression=f"SET {expression}",
        ExpressionAttributeNames={f"#{key}": key for key in attributes},
        ExpressionAttributeValues={f":{key}": value for key, value in attributes.items()},
        ConditionExpression="attribute_exists(caseId)",
    )


# ------------------------------------------------------------ the case itself


def build_case(
    *,
    call: "LiveCall",
    call_outcome: str,
    duration_seconds: float,
    human_handoff: bool,
    mobile_number: str | None,
    ids: dict[str, str],
    error: str | None = None,
) -> dict[str, Any]:
    """Everything about this call that is already settled when the line drops.

    Args:
        call: The finished call. Read for the transcript, the caller's typed
            details, and the memory keys a reader needs to pull the full
            conversation back later.
        call_outcome: strands_agent's own outcome for the connection.
        duration_seconds: Wall-clock length of the call.
        human_handoff: Whether a human agent actually took the call over.
            Whether a human was merely *queued* for takes no argument: it is
            `escalationId` being set, which `ids`/`call.escalation` already
            supply.
        mobile_number: The caller's number as finally confirmed — the tool-
            confirmed one where a lookup happened, otherwise whatever the
            pre-call form supplied.
        ids: Identifiers a completed tool call actually returned this call
            (see strands_agent._CaseFactsTool). Never what the caller merely
            said, and never what the model passed in.
        error: The exception that ended the call, when one did.

    Returns:
        The row as plain Python. `_attr` drops whatever is absent.
    """
    now = time.time()
    derived = _derived_status(call_outcome=call_outcome, human_handoff=human_handoff)
    item: dict[str, Any] = {
        # The call id, not a fresh uuid: one call is one case, the portal link
        # and every log line for this call already carry it, and reusing it is
        # what makes a retry of this write an upsert instead of a duplicate.
        "caseId": call.call_id,
        # No customer entity exists upstream — policies carry a mobile number
        # and a holder name, and nothing joins a person across them. The
        # confirmed number is the one identifier that is stable between calls
        # and still present when no policy was ever found.
        "customerId": mobile_number or call.mobile_number or "",
        "policyId": ids.get("policyId", ""),
        "claimId": ids.get("claimId", ""),
        "escalationId": ids.get("escalationId") or (call.escalation or {}).get("escalation_id") or "",

        # Filled in by summarize_and_update. Seeded here so a case that is
        # never summarized still has the fields, rather than a reader having
        # to treat "absent" and "not worked out yet" as the same thing.
        # Derived, and never overwritten by the summarizer — see the note on
        # RESOLUTION_STATUSES. Only the two free-text fields below are the
        # model's, and they start empty until it has read the transcript.
        "resolutionStatus": derived,
        "customerIntent": "unknown",
        "resolutionSummary": "",
        "summaryStatus": "pending",

        # What resolutionStatus was derived from. Kept because the outcome
        # collapses them: `completed` alone doesn't say whether the caller had
        # asked for a human (that's `escalationId`), and `human_handoff` alone
        # doesn't say what the connection did afterwards.
        "callOutcome": call_outcome,
        "humanHandoff": human_handoff,
        "handledBy": call.human_label or "",
        "callError": error or "",

        "callerName": call.caller_name or "",
        "channel": "voice",
        "durationSeconds": round(duration_seconds, 3),
        "transcriptTurns": len(call.transcript),

        # Sydney-local, like every other timestamp this project stores.
        "startedAt": epoch_to_au(call.started_at),
        "createdAt": epoch_to_au(now),
        # The sortable twin of createdAt, and the reason both exist: an index
        # has to order chronologically as stored, and the AU string does not
        # (see epoch_to_au — DD/MM/YYYY isn't lexicographic, and Sydney's clock
        # repeats an hour at the AEDT->AEST change). So this carries the raw
        # epoch and is the range key on both GSIs, leaving `createdAt` free to
        # be the one a human reads. Exactly the split the escalations table
        # makes between its `created_at` and `queueKey`.
        "createdAtEpoch": round(now, 3),

        # How to read the conversation itself back out of AgentCore Memory.
        # The case holds a summary, not the transcript — this is the pointer
        # to the real thing.
        "memoryActorId": call.memory_actor_id or "",
    }
    # The caller's DOB is deliberately absent. It's an identity secret used to
    # authenticate them, and a case is read by far more people than an
    # escalation queue row is.
    if CASE_TTL_DAYS > 0:
        # DynamoDB TTL only accepts a raw epoch int — not a float, and not a
        # formatted date. Same constraint as the escalations/claims rows.
        item["ttl"] = int(now + CASE_TTL_DAYS * 86400)
    return item


async def write_case(**kwargs: Any) -> str | None:
    """Write the deterministic half of the case. Never raises.

    Returns:
        The status this call derived from the machinery's own record, or None
        if no row reached the table. Both halves of that matter to the caller:
        None means `summarize_and_update` has nothing to update, and the status
        is what the summarizer falls back to when the model can't supply one —
        handing it back here saves recomputing it from the same four values.
    """
    try:
        item = build_case(**kwargs)
        # Recomputed rather than read back off the row: the derived status is
        # no longer stored (it is reconstructible from callOutcome,
        # humanHandoff and escalationId), but the summarizer still needs it as
        # the value it falls back to when the model can't supply a valid one.
        derived = _derived_status(
            call_outcome=kwargs["call_outcome"],
            human_handoff=kwargs["human_handoff"],
        )
    except Exception as exc:  # a malformed case must not break a teardown
        event("case.build_failed", error=f"{type(exc).__name__}: {exc}")
        return None

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_put_case, item), timeout=CASE_WRITE_TIMEOUT)
    except TimeoutError:
        event("case.write_failed.timeout", case_id=item["caseId"],
              timeout_seconds=CASE_WRITE_TIMEOUT)
        return None
    except Exception as exc:
        event("case.write_failed", case_id=item["caseId"],
              error=f"{type(exc).__name__}: {exc}")
        return None

    event("case.written", case_id=item["caseId"],
          policy_id=item["policyId"] or None, claim_id=item["claimId"] or None,
          resolution_status=item["resolutionStatus"])
    return derived


# ------------------------------------------------------------ summarization

_SYSTEM = (
    "You read a finished contact-centre call and record what it was about and "
    "how it ended. You are writing an internal case record, not talking to the "
    "customer. Report only what the transcript shows: never guess an outcome "
    "the conversation doesn't support, and never carry over a detail that "
    "isn't in it. Reply with a single JSON object and nothing else."
)

_INTENT_LIST = ", ".join(CUSTOMER_INTENTS)


def _render_transcript(transcript: list[dict[str, Any]]) -> str:
    """The last CASE_TRANSCRIPT_TURNS turns as plain speaker-labelled lines."""
    lines = []
    for turn in transcript[-CASE_TRANSCRIPT_TURNS:]:
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        speaker = "Caller" if turn.get("role") == "user" else "Ava"
        lines.append(f"{speaker}: {text[:_MAX_LINE_CHARS]}")
    return "\n".join(lines)


def _parse_summary(text: str) -> dict[str, Any] | None:
    """The model's JSON object, however it chose to wrap it.

    Tolerant of a ```json fence and of leading prose, because a cheap model
    asked for "JSON and nothing else" will occasionally supply a sentence
    first, and losing a whole case to that would be silly.
    """
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        braced = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not braced:
            return None
        try:
            parsed = json.loads(braced.group(0))
        except (json.JSONDecodeError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _summarize(transcript_text: str, derived_status: str) -> dict[str, Any]:
    """Ask Bedrock what this call was about (blocking; use a thread).

    Returns only what a transcript can actually answer: what the caller wanted,
    what was done about it, and whether they left with anything outstanding.

    That last one is `callerNeedMet`, and it is a *question*, not a status —
    the model is never asked to name a `resolutionStatus`, because three of the
    four are facts about the connection it cannot see. The caller's own words
    are the one thing it can judge, and the caller hanging up the moment their
    question is answered is indistinguishable from abandonment at the socket.
    :func:`summarize_and_update` is what turns a True here into the single
    permitted upgrade.

    `customerIntent` is validated against its closed set; anything the model
    invents becomes "unknown" rather than landing an unreportable value.
    """
    client = portal_context.client("bedrock-runtime", CASE_SUMMARY_REGION)
    prompt = (
        "Here is the transcript of a finished call.\n\n"
        f"<transcript>\n{transcript_text}\n</transcript>\n\n"
        f"The connection ended as: {derived_status}. That is already recorded "
        "and is not yours to decide.\n\n"
        "Reply with exactly this JSON object:\n"
        "{\n"
        f'  "customerIntent": one of [{_INTENT_LIST}],\n'
        '  "customerIntentDetail": one short sentence saying what they '
        "actually wanted, in your own words,\n"
        '  "resolutionSummary": two or three sentences for a colleague picking '
        "this up cold — what the caller wanted, what was done, and what (if "
        "anything) is still outstanding,\n"
        '  "callerNeedMet": true or false\n'
        "}\n\n"
        "callerNeedMet is true only if the caller's question was actually "
        "answered and nothing was left open — including when they then hung up "
        "without saying goodbye, which is a normal way to end a call someone is "
        "satisfied with. Signs of true: they said that's all, they don't need "
        "anything else, thanks, or the answer they asked for was given and they "
        "raised nothing further. Signs of false: they were still waiting on "
        "something, a question went unanswered, they were promised a person, "
        "they were cut off mid-answer, or they sounded unhappy. If the "
        "transcript doesn't settle it, answer false.\n\n"
        "Do not include the caller's date of birth, mobile number, address or "
        "any other identifier in the summary — the case record already holds "
        "the ones that matter."
    )
    response = client.converse(
        modelId=CASE_SUMMARY_MODEL_ID,
        system=[{"text": _SYSTEM}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # Zero temperature because two runs over the same call should produce
        # the same case; 600 tokens is comfortably above three sentences and
        # is what stops a runaway model from being expensive.
        inferenceConfig={"maxTokens": 600, "temperature": 0.0},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    parsed = _parse_summary(text)
    if parsed is None:
        raise ValueError(f"model returned no JSON object: {text[:200]!r}")

    intent = str(parsed.get("customerIntent") or "").strip()
    # Only a real boolean true counts. A model that omits the field, or answers
    # with prose, leaves the derived status alone — the upgrade has to be
    # asserted, never inferred from something ambiguous.
    need_met = parsed.get("callerNeedMet")
    return {
        "customerIntent": intent if intent in CUSTOMER_INTENTS else "unknown",
        "customerIntentDetail": str(parsed.get("customerIntentDetail") or "").strip()[:500],
        "resolutionSummary": str(parsed.get("resolutionSummary") or "").strip()[:2000],
        "callerNeedMet": need_met is True or str(need_met).strip().lower() == "true",
        "summaryStatus": "ok",
    }


async def summarize_and_update(call: "LiveCall", derived_status: str) -> None:
    """Fill in the three judged fields on an already-written case. Never raises.

    Runs last in teardown: it is the only part of the case that takes seconds,
    and by the time it runs the caller has hung up, the socket is closed and
    the model session is stopped, so the seconds cost nobody anything.

    It is also where the one permitted status upgrade is applied:
    `caller_disconnected` becomes `completed` when the model reports the
    caller's need was met — someone who gets their answer and hangs up
    satisfied closes the socket exactly like someone who gives up, and only the
    transcript tells them apart. Nothing else about the status is negotiable.

    Every failure leaves the deterministic row exactly as it was, with
    `summaryStatus` saying which failure it was — "skipped" (nothing to read,
    or summarization turned off) or "failed" (the model call didn't work).
    That distinction is the difference between a quiet call and a broken
    pipeline, and it is not inferable from an empty summary alone.
    """
    if not CASE_SUMMARY_MODEL_ID:
        await _mark_summary(call.call_id, "skipped", "summarization disabled")
        return

    transcript_text = _render_transcript(call.transcript)
    if not transcript_text:
        # A call that dropped before anyone said anything. There is genuinely
        # nothing to summarize, and asking a model to try would invent one.
        await _mark_summary(call.call_id, "skipped", "no transcript")
        return

    try:
        fields = await asyncio.wait_for(
            asyncio.to_thread(_summarize, transcript_text, derived_status),
            timeout=CASE_SUMMARY_TIMEOUT,
        )
    except TimeoutError:
        event("case.summary_failed.timeout", case_id=call.call_id,
              timeout_seconds=CASE_SUMMARY_TIMEOUT)
        await _mark_summary(call.call_id, "failed", "timeout")
        return
    except Exception as exc:
        event("case.summary_failed", case_id=call.call_id,
              model_id=CASE_SUMMARY_MODEL_ID, error=f"{type(exc).__name__}: {exc}")
        await _mark_summary(call.call_id, "failed", f"{type(exc).__name__}: {exc}")
        return

    # The one transition the transcript is allowed to make. Applied here rather
    # than inside _summarize so the rule lives next to the status it governs,
    # and so the model's answer stays a plain question-and-answer that can be
    # logged and argued with.
    need_met = fields.pop("callerNeedMet", False)
    upgraded = need_met and derived_status == _RESOLVED_UPGRADE_FROM
    if upgraded:
        fields["resolutionStatus"] = _RESOLVED_UPGRADE_TO

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_update_case, call.call_id, fields),
            timeout=CASE_WRITE_TIMEOUT)
    except Exception as exc:
        # The summary existed and the row didn't take it. Logged with the
        # summary attached, so the one that cost money isn't simply lost.
        event("case.summary_update_failed", case_id=call.call_id,
              error=f"{type(exc).__name__}: {exc}",
              customer_intent=fields.get("customerIntent"))
        return

    event("case.summarized", case_id=call.call_id,
          customer_intent=fields["customerIntent"],
          resolution_status=fields.get("resolutionStatus", derived_status),
          # Both reported, so an upgrade is auditable after the fact: the row
          # says `completed` and this line says what it was derived as and that
          # the transcript is why it changed.
          derived_status=derived_status,
          caller_need_met=need_met,
          upgraded=upgraded)


async def _mark_summary(case_id: str, status: str, note: str) -> None:
    """Record why a case carries no summary, without ever raising."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _update_case, case_id, {"summaryStatus": status, "summaryNote": note[:500]}),
            timeout=CASE_WRITE_TIMEOUT,
        )
    except Exception as exc:
        event("case.summary_status_failed", case_id=case_id,
              error=f"{type(exc).__name__}: {exc}")
