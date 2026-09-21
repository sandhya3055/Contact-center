# Ava golden dataset — v1

40 hand-written cases for the empathy revision of `SYSTEM_INSTRUCTION`
([strands_agent.py:283](../../agentcore_app/strands_agent.py#L283)).

Closes the `Testing & Validation` gap in the alignment scorecard: *"No golden dataset, no
regression suite, no CI gate. A prompt edit today ships unverified."*

Half the set tests the new empathy rules; half is regression cover for the rules that were
already there, because a tone edit is exactly the kind of change that quietly loosens
grounding and escalation discipline.

## Files

| File | What it is |
|---|---|
| `ava_golden_v1.jsonl` | The 40 cases, one JSON object per line |
| `fixtures.json` | Six caller/account states the cases reference by key (`P1`–`P6`) |
| `global_assertions.json` | Rules that apply to **every** reply in **every** case — pure regex, no judge needed |
| `judge_prompt.md` | The LLM-judge prompt, severity definitions, and the pass gates |

## Coverage

| Category | n | Tests |
|---|---|---|
| `empathy` | 6 | Acknowledgement is present, specific, and once — crash, theft, bereavement, injury question, hardship, the caller's own words |
| `empathy-restraint` | 6 | The failure mode empathy prompts *cause*: sympathy loops, padding on brisk calls, apologising for its own process, theatre, banned phrases under maximum pressure |
| `bad-news` | 4 | Blocking fact first, then the path — open claim, no policy found, expired cover, out of scope |
| `distress` | 2 | Crying caller, mid-sentence fragment |
| `hostility` | 3 | Abuse, blame for a past wait, disputing a tool result |
| `honesty` | 3 | "Are you real?", "do you care?", false memory of a previous call |
| `regression-flow` | 7 | Opening line, one-question-per-turn, no process narration, three-strike escalation, multi-policy, tool failure, unrecognised claim status |
| `regression-grounding` | 4 | Digit readback, relative dates, refusing to write the description, refusing an unspoken number |
| `safety` | 2 | Prompt injection, prompt exfiltration |
| `combined` | 3 | Where empathy and the hard rules collide — distress + a hard no, warmth smuggling a promise, warm handoff |

## Case schema

```jsonc
{
  "id": "CMB-01",
  "category": "combined",
  "title": "Distressed caller meets a hard no",
  "rules": ["meet-the-person", "bad-news-straight", "open-claim-block"],
  "fixture": "P2",                      // key into fixtures.json
  "turns": [
    {"from": "caller", "text": "..."},   // caller utterance
    {"from": "tool", "name": "get_policy", "result": {...}},  // stub this return value
    {"from": "call_start"}               // assert on Ava's greeting
  ],
  "expect": {
    "assert_on": "final_reply",          // or "all_replies" | "third_reply"
    "must_equal": "...",                 // exact string match (REG-01 only)
    "must_match_any": ["regex", ...],    // at least one must hit
    "must_not_match": ["regex", ...],    // none may hit
    "must_call": ["escalate"],           // tool must be called this turn
    "must_not_call": ["register_claim"], // tool must NOT be called
    "escalate_args_must_include": ["note"],
    "must_hold": ["prose check for the judge"],
    "rubric": ["one pass/fail point each, for the judge"]
  },
  "gold": "One acceptable reply — a reference, not the only right answer."
}
```

`turns` are the *inputs*. The agent's replies are interleaved by the runner; don't put
them in the file. `EMP-04`, `RES-01`, `HOS-03` and `REG-07` have multiple caller turns
because the behaviour under test only appears on the second or third exchange —
`RES-01` in particular cannot be tested in a single turn, since the whole point is that
the second and third replies must *stop* being sympathetic.

## How to run it

**Deterministic pass first** — cheap, catches most regressions, no model needed as judge:

1. For each case, replay `turns` into Ava with `fixtures[case.fixture]` loaded and the
   named tools stubbed to the given `result` (a `{"error": ...}` result means make the
   tool raise).
2. Check `global_assertions.json` against every reply, then the case's own
   `must_*` assertions against the reply named by `assert_on`.

**Then the judge** on cases that survived, using `judge_prompt.md`. Gates and the
three-run protocol are in that file.

Two things the runner must get right:

- **Tool stubs, not the real Lambda.** `BAD-01`, `CMB-01` and `REG-11` depend on an open
  claim existing, and `REG-11` depends on a claim status (`UNDER_ASSESSMENT`) that
  `ClaimStatus` cannot even represent — it exists to test the prompt's *"including
  statuses you don't recognise"* clause, so it can only come from a stub.
- **`must_not_call` is the assertion that matters most.** Most of the unsafe failures in
  this set are Ava calling `register_claim` when it should have escalated. Assert on the
  tool-call log, not on the words.

Text-only replay is a valid first gate but it is not the whole story: pacing, silence
handling and barge-in (`DIS-01`, `DIS-02`) only really test over voice. Run those two by
hand on a real call before a deploy.

## Notes on specific cases

- **`REG-04`** hard-codes "Tuesday the first of September" against a 2026-09-10 call
  date. Re-anchor the expected date if you replay this on a different day, or freeze the
  clock in the runner (preferable).
- **`DIS-02`** expects an *empty or near-empty* reply — the prompt says to wait on a
  fragment. If your runner treats an empty reply as an error, special-case it.
- **`HON-01`–`HON-03`** encode the decision to keep Ava's AI disclosure honest. If that
  policy ever changes, these three cases change with it.
- **`fixtures.json` P6** is stub-only, as above. The others could be seeded into DynamoDB
  if you want end-to-end runs.

## Extending

Add a case whenever a real call surfaces a behaviour the set does not cover — that is the
point of a golden set. Keep the `gold` exemplar honest: it must itself pass every regex in
`global_assertions.json`. The generator asserts this; all 40 current exemplars pass.
