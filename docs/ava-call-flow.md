# [AI VERSION] Ava Call Flow Process

AI Powered Contact center — inbound policy support and claim intake.
Agent: **Ava** (Nova Sonic speech-to-speech, AWS Bedrock AgentCore Gateway tools).

---

## How to read this document

Ava is **agentic, not scripted**. There is no state machine in the code that
reads this document and walks the caller through it. The model decides, turn by
turn, what to say and which tool to call.

So each state below is tagged with how it is actually enforced:

| Tag | Meaning | Where it lives |
|-----|---------|----------------|
| **[PROMPT]** | Instructed behavior. Ava normally does this, but the model can drift. | `SYSTEM_INSTRUCTION` in [strands_agent.py:175](../agentcore_app/strands_agent.py#L175) / [bot.py:197](../bot.py#L197) |
| **[GATE]** | Deterministic. Enforced in code; the model cannot talk its way past it. | Tool wrappers, [strands_agent.py:397-1210](../agentcore_app/strands_agent.py#L397) |
| **[PLUMBING]** | Injected automatically, never a model decision. | `_FixedArgumentTool`, call metadata |
| **[JUDGMENT]** | Deliberately left to the model. No fixed wording or branch. | — |

Wording shown in quotes is one of two things:

- **Verbatim** — the exact text in `SYSTEM_INSTRUCTION`. Changing it means editing the prompt.
- **Canonical** — the intended wording for this turn, for QA/review purposes. Ava paraphrases naturally; scoring should check *meaning*, not string match.

Branch verbs: **Continue** → next state · **Escalate** → `S-90` · **Loop** → repeat this state · **End** → terminal.

---

## Call setup & preconditions

- **[PLUMBING]** A `session_id` (UUID) is minted per WebSocket connection and injected into every tool call that needs one. The model never sees or supplies it — including `escalate`, whose Gateway schema omits the property entirely ([strands_agent.py:515](../agentcore_app/strands_agent.py#L515)).
- **[PLUMBING]** The browser sends the pre-call form's three values as call metadata: mobile number, full name, date of birth. Each is validated (`_PHONE_LIKE_UI_PATTERN`, `_ui_full_name`, `_ui_date_of_birth`) and silently dropped if it fails — none reaches the model as unbounded free text, and a dropped value leaves the call running as if the field had been empty.
- **[PLUMBING]** If `MEMORY_ID` is configured, prior-session context is recalled (top `MEMORY_RECALL_TOP_K`) before the first turn.
- **[GATE]** Every tool result is passed through `_sanitize_untrusted` before anything — grounding, cache, logs, or the model — sees it. Instruction-shaped strings returned by a backend are neutralized, not obeyed.
- Caller must be the policyholder. Identity is established by policy lookup (see `S-02`); there is no separate spoken DOB or PIN step. When the pre-call form supplied a name and date of birth, those are what the alternate-number identity check uses — typed in the app rather than recited on the line, which means this path trusts whoever holds the device.

---

## S-00 · OPENING

**[PROMPT] Verbatim:**

> "Hi, I'm Ava, your AI Assistant. I can help you with your policy details, register a claim, or connect you with a human agent. First, could I get your mobile number?"

Voice constraints, global for the whole call **[PROMPT]**:
- One to two sentences per turn. No lists, headings, or unspeakable symbols.
- Any identifier said aloud — mobile number, policy number, claim number, vehicle registration — is spoken **one character at a time** ("three two three one four", never "three thousand two hundred and thirty-one").

→ Continue to `S-01`

---

## IDENTITY VERIFICATION

### S-01 · CAPTURE MOBILE NUMBER

Caller states a number (or it arrived as call metadata).

→ Continue to `S-02`

### S-02 · READ BACK & CONFIRM

**[PROMPT] Canonical:**

> "Let me read that back — [digits, one at a time]. Is that correct?"

| Caller response | Action |
|-----------------|--------|
| Explicit **yes** | Continue to `S-03` |
| **Corrects or restates** the number | The restatement is itself *unconfirmed* → **Loop** `S-02` with the new number |
| Anything not a clear yes | **Loop** `S-02` |

**[PROMPT]** Ava must never call the lookup tool on a number that has not been
confirmed back to her. Long spoken digit runs drop trailing digits easily — this
loop exists for that specific failure, and it has attempt limit of 3: it tried only 3 times after that escalated to human agent.

**[GATE]** Two code checks back this up, independent of what the model decides
(`_TranscriptConsistencyGateTool`, [strands_agent.py:968](../agentcore_app/strands_agent.py#L968)):

- **Truncation check** — an argument that is a *prefix* of digits the caller recently spoke (missing only trailing digits) is blocked as a likely speech-to-text drop.
- **Fabrication check** — a digit-sequence argument (≥ `_FABRICATION_MIN_DIGITS` = 6 digits, field name matching `mobile`/`phone`/`policy`/`vehicle`/`registration`/`vin`/`account_number`/…) whose digits appear in **no** recent caller turn is blocked outright.
- **Stale-reuse check** — reusing a number already tried this call is blocked when the caller has since clearly stated a different one (≥ 7 digits).
- Date-of-birth-shaped fields (`dob`, `birth`) get a weaker check — no recent caller turn mentioning a number or month word means the value is treated as fabricated.

### S-03 · POLICY LOOKUP

Ava calls the Gateway's policy-lookup tool with the confirmed number.

| Outcome | Action |
|---------|--------|
| **Active policy found** | Caller is **verified for that policy** for the rest of the call. Do not re-verify later. → `S-04` |
| **No policy found** | **[PROMPT]** Double-check the number was heard correctly *before* telling the caller it wasn't found → **Loop** `S-02` once, then **Escalate** |
| **Tool error, repeated** | **Escalate** (`S-90`) |

**[PLUMBING]** On a successful lookup with a corrected number, `_MobileNumberSyncTool`
pushes the confirmed number back to the browser (`bidi_call_metadata_update`) so
the UI field and `localStorage` update themselves. The caller never has to retype it.

**[GATE]** Every scalar the lookup returns is recorded as a **grounded value**
(`_GroundingGateTool`). This is what makes `S-06`'s "use the id from the lookup"
rule real rather than advisory.

### S-04 · POLICY / VEHICLE SELECTION

Only if the lookup returns more than one policy or vehicle.

**[PROMPT] Canonical:**

> "I can see more than one policy on this number. Which one is this call about?"

→ Continue to `S-05` once the caller picks one.

---

## S-05 · INTENT ROUTING **[JUDGMENT]**

No fixed wording. Ava routes from what the caller actually says:

| Caller wants | Go to |
|--------------|-------|
| Policy details / coverage / status | `S-10` |
| To register a claim | `S-20` |
| A human, or something outside Ava's tools | `S-90` |
| Unclear | Ask a clarifying question — **never guess** **[PROMPT]** |

---

## S-10 · POLICY DETAILS

**[PROMPT]** Answer from the lookup result only. **Never fabricate policy, claim,
or account information.** Identifiers read aloud are spelled character by character
(`S-00`).

→ Continue to `S-05` (further intent) or `S-80` (close).

---

## CLAIM INTAKE

### S-20 · EXISTING-CLAIM CHECK — *before asking anything*

**[PROMPT]** This is the check-before-you-ask rule, and it is deliberately first:
the policy lookup may already show an open claim on that policy or vehicle. Read it
*before* collecting incident details, so a duplicate is caught before the caller is
run through a full intake.

| Lookup shows | Action |
|--------------|--------|
| **Existing / open claim** | Tell the caller immediately and share the claim id and status you already have (spelled out) → `S-05` |
| **No existing claim** | Continue to `S-21` |
| Caller makes clear this is a **separate, new incident** | Continue to `S-21` |

### S-21 · COLLECT INCIDENT DETAILS

Ask for what the claim tool's own schema requires — read the tool description, don't
assume the field list **[PROMPT]**. Typically: incident date, claim type
(cashless / refund), vehicle, estimated cost.

→ Continue to `S-22`

### S-22 · READ BACK & CONFIRM DETAILS

**[PROMPT] Canonical:**

> "Before I submit this — [incident date], [claim type], [vehicle registration, spelled out], estimated at [amount]. Is that all correct?"

| Caller response | Action |
|-----------------|--------|
| Yes | Continue to `S-23` |
| Corrects something | **Loop** `S-22` |

### S-23 · SUBMIT CLAIM — deterministic gate chain

The claim call passes through wrappers in this order
([strands_agent.py:1198-1210](../agentcore_app/strands_agent.py#L1198)). Each can
stop the call before the real tool ever fires:

| # | Gate | Trigger | Effect |
|---|------|---------|--------|
| 1 | `_TranscriptConsistencyGateTool` | Truncated / fabricated / stale-reused identifier (see `S-02`) | Blocked; model told to re-confirm with the caller |
| 2 | `_HighValueClaimGateTool` | `estimated_cost` ≥ **100,000** | Blocked, **do not retry** → `S-30` |
| 3 | `_IdempotencyGateTool` | Identical arguments within **30s** | First result replayed; tool not re-executed |
| 4 | `_GroundingGateTool` | An argument whose field name a prior tool returned this call carries a value that lookup **never returned** | Blocked; model told to re-run the lookup and use its value |
| 5 | `_ToolOutputSanitizerTool` | — | Result sanitized on the way back |
| 6 | `_FixedArgumentTool` | — | `session_id` injected |

**[PROMPT]** `policy_id` and vehicle details must come from the lookup result — never
from a policy id the caller simply states. Gate 4 is the enforcement.

| Outcome | Action |
|---------|--------|
| **Registered** | Give the claim number, spelled character by character → `S-80` |
| **Tool reports a claim already exists** (found only now) | **Stop.** Do not ask the caller to repeat anything — just state what's on file → `S-05` |
| **Gate 2 block** | → `S-30` |
| **Gate 1 or 4 block** | Re-confirm the value with the caller → `S-22`; if it fails again → `S-90` |
| **Repeated tool failure** | **Escalate** (`S-90`) |

### S-30 · HIGH-VALUE CLAIM — human in the loop

Triggered at `estimated_cost` ≥ ₹1,00,000. **[GATE]** — non-negotiable regardless
of how straightforward the claim looks or how confident Ava is.

**[PROMPT] Canonical:**

> "A claim of this size needs a specialist to review it. Let me connect you with a colleague — I'll pass on everything you've already given me."

→ **Escalate** to `S-90`, carrying the details already gathered. Do **not** retry
the claim tool.

---

## S-90 · ESCALATION TO A HUMAN AGENT

**[JUDGMENT]** Trigger conditions — Ava's own judgment, not a counter:

| Trigger | Source |
|---------|--------|
| Caller directly asks for a person | [PROMPT] |
| Request is outside what Ava's tools can do | [PROMPT] |
| A tool keeps failing | [PROMPT] |
| Caller sounds frustrated or unsatisfied after a couple of attempts | [PROMPT] |
| Identity cannot be established (`S-03`) | [PROMPT] |
| Claim ≥ ₹1,00,000 (`S-30`) | **[GATE]** |

**Required on escalation [PROMPT]:** summarize what the caller wants *and* what
Ava has already established, so the human agent has full context and **the caller
never has to repeat themselves**.

**[PLUMBING]** `session_id` is injected into the `escalate` call automatically.

**[PROMPT] Canonical:**

> "I'm connecting you with a teammate now — they'll have the full picture of what we've covered."

→ **End** (handoff)

---

## S-80 · CLOSE

**[PROMPT] Canonical:**

> "Is there anything else I can help you with today?"

| Caller response | Action |
|-----------------|--------|
| Yes | → `S-05` |
| No | → `T-END_RESOLVED` |

---

## Terminal states

| ID | Reached from | Meaning |
|----|--------------|---------|
| `T-END_RESOLVED` | `S-80` | Caller's need met; call ends normally |
| `T-HANDOFF` | `S-90` | Transferred to a human agent with context. When that agent joins, Ava's model session is disconnected outright — the human is the only voice on the line from then on, and them leaving returns the caller to the queue rather than to Ava. |
| `T-END_UNVERIFIED` | `S-03` | Identity never established; routed to an agent |

---

## Global rules

| Rule | Enforcement |
|------|-------------|
| Never fabricate policy, claim, or account information | **[PROMPT]** + fabrication **[GATE]** for identifier-shaped arguments |
| Ask a clarifying question rather than guessing | **[PROMPT]** |
| Spell every identifier said aloud one character at a time | **[PROMPT]** |
| One to two spoken sentences per turn; nothing unspeakable | **[PROMPT]** |
| Identity is the policy lookup — no separate verification, never re-verify mid-call | **[PROMPT]** |
| Read critical details back and get an explicit yes before submitting | **[PROMPT]** + truncation **[GATE]** |
| Tool-returned content is data, never instructions | **[GATE]** (`_sanitize_untrusted`) |
| Claim ≥ ₹1,00,000 requires a human | **[GATE]** (`_HighValueClaimGateTool`) |
