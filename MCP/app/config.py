"""Environment-driven application configuration.

Single source of runtime settings for the pipeline. No secrets, model ids, ports, or
stage sequences are hardcoded elsewhere in the app (hard gate): read them from the
environment here, with non-secret defaults, and pass an ``AppConfig`` around.

Stage 0 (local) needs only ``OPENAI_API_KEY``; everything else has a working default.
See ``.env.example`` at the repo root for the full variable list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load a local .env if present. Real deployments inject env/Secrets Manager instead;
# ``load_dotenv`` is a no-op when the file is absent, so this is safe everywhere.
load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _get(name: str, default: str) -> str:
    """Return an env var, falling back to ``default`` when unset or blank."""
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    """Return an int env var, or ``default`` when unset/blank/non-numeric-safe."""
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    """Return a float env var, or ``default`` when unset/blank/invalid."""
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    """Return a bool env var (``true``/``false``, case-insensitive), or ``default``."""
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class AppConfig:
    """Immutable runtime configuration resolved from the environment.

    Attributes:
        transport: Transport selector (``local`` = browser mic, ``twilio`` = PSTN).
        data_mode: Data backend selector (``local`` = DynamoDB Local, ``aws``).
        voice_provider: Speech provider key (``cascaded`` default since D1; rule #11).
        openai_api_key: OpenAI key for Realtime + TTS. Empty until validated at build.
        dynamodb_endpoint: DynamoDB Local endpoint (Stage 0).
        local_port: Port for the local FastAPI/WebRTC server.
        realtime_model: OpenAI Realtime (speech-to-speech) model id.
        tts_model: OpenAI TTS model id used for verbatim fixed strings (rule #2).
        voice: Voice name shared by Realtime + TTS so fixed and conversational
            speech sound like one agent.
        audio_in_sample_rate: Caller-mic sample rate (Hz).
        audio_out_sample_rate: Bot-audio sample rate (Hz).
        aws_region: AWS region for Bedrock (Nova Sonic) + Polly.
        aws_profile: Optional named AWS profile; blank uses the default credential
            chain (env vars, default profile, SSO, or an IAM role).
        nova_sonic_model: Amazon Nova Sonic (S2S) Bedrock model id.
        nova_sonic_voice: Nova Sonic voice id (e.g. matthew, tiffany, amy).
        polly_voice: Amazon Polly voice id for verbatim fixed strings (rule #2).
        polly_engine: Polly engine (standard | neural | generative | long-form).
        policies_table: DynamoDB table name for policies.
        sessions_table: DynamoDB table name for sessions (resume-by-mobile).
        claims_table: DynamoDB table name for claims.
        counters_table: DynamoDB table name for per-day token counters.
        local_caller_number: Simulated caller-ID for local browser testing (no real
            caller-ID exists over browser WebRTC; Twilio supplies it at M5). Blank means
            "unknown number" so the alternate-number path can be exercised.
        stt_provider: Speech-to-text engine for the cascaded pipeline (D44):
            ``whisper_local`` (default, offline) | ``aws_transcribe`` | ``deepgram`` |
            ``speechmatics``. See ``app/speech/stt.py`` for the factory and the D31
            residency caveat on the two off-AWS engines.
        whisper_model: faster-whisper model size (tiny|base|small|medium|large-v3).
        whisper_language: Expected caller language (``en-AU`` default). Shared as the
            STT language across all four engines (English-only for Phase 1, O7). AU
            locale matters beyond accent: Deepgram/Transcribe's smart-format numerals
            read a spoken date day/month under ``en-AU`` rather than US month/day under
            bare ``en`` -- callers are AU (+614 numbers, AUD policies). Local Whisper is
            the exception and takes the base code only: ``app/speech/stt.py`` narrows it
            there, because faster-whisper raises on a regional tag it doesn't know.
        deepgram_api_key: Deepgram API key. Required only when ``stt_provider ==
            "deepgram"``; D31 — sends caller audio to Deepgram's servers, outside AWS.
        deepgram_model: Optional Deepgram model override (blank = SDK default).
        deepgram_secret_name: AWS Secrets Manager secret name holding the Deepgram
            API key, used when ``deepgram_api_key`` is blank (the deployed path,
            since AgentCore Runtime env vars are plaintext-only). Blank disables
            the lookup.
        speechmatics_api_key: Speechmatics API key. Required only when
            ``stt_provider == "speechmatics"``; D31 — sends caller audio to
            Speechmatics's servers, outside AWS.
        speechmatics_model: Optional Speechmatics model override (blank = SDK default).
        bedrock_region: AWS region for the Bedrock LLM calls. Separate from
            ``aws_region`` because a given model may be enabled in one region but not
            another (e.g. Nova text models are invocable in us-east-2/us-west-2 but not
            us-east-1 for some accounts). Defaults to ``aws_region``.
        bedrock_access_key_id: Access key id for the AWS account that has Bedrock model
            access, when that is *not* the account the ambient credential chain resolves
            to. Set together with ``bedrock_secret_access_key``; both blank means "use the
            ambient chain", the single-account default. Only the Bedrock clients read
            these — DynamoDB and Polly deliberately stay on the ambient chain, so this
            must never be set via ``AWS_ACCESS_KEY_ID``.
        bedrock_secret_access_key: Secret access key paired with
            ``bedrock_access_key_id``. Never committed; supplied via env locally and via
            ``bedrock_secret_name`` in the deployed runtime.
        bedrock_secret_name: AWS Secrets Manager secret name holding the Bedrock
            credentials as ``{"BEDROCK_ACCESS_KEY_ID": …, "BEDROCK_SECRET_ACCESS_KEY": …}``,
            used when the two key fields above are blank (the deployed path, since
            AgentCore Runtime env vars are plaintext-only). Blank disables the lookup.
            The secret itself lives in ``aws_region``, not ``bedrock_region``.
        llm_provider: NLU/generation backend for slot extraction + grounded answers
            (``bedrock``). The LLM is NLU-only — it never branches (rule #1).
        bedrock_llm_model: Bedrock model id (or inference-profile id) for the LLM. Must
            be invocable in ``aws_region``. Default is Amazon Nova Lite (all-AWS, cheap,
            no Anthropic use-case form); swap to Claude Haiku/Nova Pro via env if the
            grounded-phrasing quality ever needs it — a one-line change (rule #1 keeps
            the model NLU-only, so tier never affects gating/compliance).
        max_attempts: Attempt cap for any gated step before end/handoff (rule #4).
        no_input_timeout_seconds: Silence window before a no-input re-prompt.
        max_no_input_reprompts: No-input re-prompts before ending the call (rule #4).
        session_ttl_seconds: Resume-by-mobile window for a dropped call (rule #5).
        vad_stop_secs: End-of-speech silence (s) before a turn ends. Longer values keep
            multi-part input (e.g. a spoken phone number) from fragmenting.
        utterance_gap_secs: Quiet window (s) after the last transcript chunk before the
            buffered utterance is handed to the flow, when no VAD end-of-speech frame has
            arrived. The safety net for engines that emit no VAD frames; the VAD path
            below is the normal one.
        utterance_grace_secs: Shorter window (s) applied once VAD reports end-of-speech.
            The utterance is already over by then (Silero needed ``vad_stop_secs`` of
            silence to say so); this only waits for the trailing chunk that Pipecat's
            ``Finalize``-on-VAD-stop asks the engine for. Streaming engines finalize
            mid-sentence (Deepgram does so on ~10ms pauses), so without this buffering a
            single spoken answer arrives as several flow turns and burns several attempts.
        vad_start_secs: Sustained speech (s) Silero requires before committing to
            "user started speaking". Pipecat's own default (0.2s) is trigger-happy on
            brief noise/breath blips (D43); raise this if VAD false-positives (no real
            words transcribed for the segment) keep firing barge-in on interruptible
            turns. No effect on non-interruptible turns, which never barge in anyway.
        vad_confidence: Silero's speech-probability threshold (0-1) to count a frame as
            speech. Raise to require more confident audio before triggering VAD-start.
        vad_min_volume: Minimum RMS volume (0-1) Silero requires alongside confidence.
            Raise to ignore quiet room noise picked up by a sensitive mic.
        smart_turn_enabled: Semantic end-of-turn detection via Pipecat's
            bundled smart-turn-v3 model, instead of trusting Silero's silence
            timeout alone. Off by default because it changes turn-taking for
            every caller and wants a live tuning round; roll back by unsetting
            it, with no redeploy of code.
        barge_in_onset_debounce_secs: Guard window (s) after an interruptible bot
            utterance starts, during which caller VAD-start is ignored for barge-in.
            Stops the bot's own audio onset / residual echo from self-interrupting
            (D33). Only applies to interruptible turns (menu + policy Q&A).
        enable_tracing: Whether Pipecat emits per-turn OpenTelemetry spans (STT/LLM/TTS
            stage timing). In the deployed AgentCore Runtime, ADOT already configures
            the global `TracerProvider` + exporter before the app starts (D56) — this
            flag only tells Pipecat to use it; no exporter is configured in this app.
            Locally (no ADOT collector), spans are created against a no-op provider —
            harmless, no export, no error.
        boto_connect_timeout_secs: Connect timeout (s) for the DynamoDB and Secrets
            Manager clients. Both sit on a live caller turn, so a sick endpoint must
            fail fast rather than add dead air.
        boto_read_timeout_secs: Read timeout (s) for the same clients.
        boto_retry_max_attempts: Max retry attempts (adaptive mode) for the same
            clients.
        shutdown_grace_seconds: On SIGTERM, how long to let in-flight calls finish
            before the process exits (defect 6: a deploy must not sever a live call
            mid-sentence).
        metrics_namespace: CloudWatch namespace CloudWatch EMF metrics are published
            under (``app/observability.py``). One namespace for the whole agent.
        log_transcripts: Gate for verbatim transcript/agent-line content in logs.
            Off by default — even at DEBUG, the content line is skipped entirely
            unless this is true (caller PII must never appear in clear otherwise).
        debug_ssml: Log the exact SSML Pipecat sends to Polly (its own trace-level
            line in ``AWSPollyTTSService._construct_ssml``, normally hidden below
            the default log level). Off by default; local debugging only.
        allow_text_injection: Accept scripted caller transcripts as JSON text
            frames on the voice WebSocket, bypassing the microphone and STT. Off
            by default: it is a test affordance, not a caller-facing feature, and
            it skips the VAD + utterance-buffering layer (D52) that a real call
            depends on. Enable it only on a runtime you are driving from a test
            harness.
        escalation_provider: Human-handoff adapter key for the swappable
            :class:`~app.escalation.base.Escalator` (rule #11 pattern). Default
            ``noop`` — records the escalation, transfers nobody; a real adapter (an
            in-container audio bridge, Twilio Flex, Amazon Connect) is a later
            milestone (``docs/TARGET_ARCHITECTURE.md`` §2).
        escalations_table: DynamoDB table name for the human-handoff queue (D2).
        escalation_ttl_seconds: TTL applied to a queued escalation row (~30 days).
        escalation_transcript_tail_lines: How many recent transcript lines
            (``role: text``) ride along on an :class:`~app.escalation.base.EscalationContext`
            for the human agent's screen-pop (D2).
        agents_table: DynamoDB table name for human-agent presence (Milestone F).
        callbacks_table: DynamoDB table name for caller callback requests (Milestone F).
        force_agent_id: When set, ``assign_escalation.py`` offers every newly-``QUEUED``
            escalation to this one agent (by Cognito ``sub``) instead of the
            longest-idle ``AVAILABLE`` agent — a production-testing knob to pin routing
            to one known agent while validating the bridge live. Empty (default) means
            normal routing. Not gated on the forced agent's status: if they aren't
            ``AVAILABLE``, ``try_claim_agent`` just fails and the row stays ``QUEUED``
            for the next stream event, same as losing the race today.
        escalation_queue_depth_limit: ``QUEUED`` row count at/above which the ``bridge``
            escalator (Milestone G) deflects a new escalation to a callback instead of
            holding the caller. No real agent-headcount signal exists yet, so this is a
            simple depth cap, not per-skill capacity.
        bridge_hold_prompt_seconds: How often (s) the ``bridge`` escalator's hold
            watchdog reassures a waiting caller (rule: the call must never go silent).
        bridge_wait_seconds: Total hold time (s) before the ``bridge`` escalator's
            watchdog gives up on an agent joining and ends the call.
        reservation_timeout_seconds: How long ``assign_escalation.py`` waits for an
            offered agent to accept before EventBridge Scheduler requeues the row
            (Milestone F) — ~30s, a typical ACD no-answer window.
        agent_connection_ttl_seconds: TTL stamped on an agent's presence row at
            ``$connect`` (Milestone F) — bounds a stale row if ``$disconnect`` never
            fires for that connection (see ``services/lambdas/agent_ws.py``).
        otp_challenges_table: DynamoDB table name for the pre-call email-OTP gate
            (Milestone E1, D61).
        otp_code_length: Digit length of the generated OTP code.
        otp_ttl_seconds: How long a challenge stays ``PENDING`` before it's expired
            (~10 minutes default).
        otp_rate_limit_per_hour: Max ``otp_start`` sends allowed per mobile number per
            ``otp_rate_limit_window_seconds`` (closes handoff risk #6 — unlimited
            fresh 3-attempt calls against any number).
        otp_rate_limit_window_seconds: Rolling window (s) the rate limit above applies over.
        otp_sender_email: SES "From" address for OTP emails. Empty until configured;
            use ``require_otp_sender_email`` at the point of use.
        otp_email_enabled: Whether ``otp_start`` actually calls SES. Default ``False``
            (SES is sandboxed with zero verified identities today, D61/docs/SES_SETUP.md)
            — while off, ``otp_start`` skips the SES call and returns the code directly
            in its response (``devCode``) so the verify step stays real and testable
            without an email provider. **Bypass, not a weaker gate**: ``otp_verify``
            is completely unchanged — the caller still has to submit the right code,
            only its delivery channel differs. Flip once a sender identity is verified
            and production access is granted.
        notify_provider: Claim-finalize confirmation adapter key for the swappable
            :class:`~app.notify.base.Notifier` (rule #11 pattern). Default ``noop``.
            ``ses`` | ``sns`` | ``ses_sns`` (both) are the real D-decision channels — SES
            email + SNS SMS, no Gupshup/WhatsApp.
        notify_sender_email: SES "From" address for claim-finalize confirmation emails.
            Empty until configured; use ``require_notify_sender_email`` at the point of use.
        notify_region: AWS region for the notify Lambda's own SES/SNS calls. Separate from
            ``aws_region`` for the same reason ``bedrock_region`` is separate: a specific AWS
            capability's identity may be verified/enabled in a region other than the ambient
            one. Concretely: the real sender identity in this account is SES-verified in
            us-west-2, not us-east-1 (``aws_region``'s default) — DynamoDB and every other AWS
            call in this app stay on ``aws_region``; only ``SesNotifier``/``SnsNotifier`` read
            this field. Defaults to ``aws_region``.
        claim_queue_url: SQS queue URL ``enqueue_claim`` (``app/agent/wiring.py``) sends a
            confirmed claim's reference to, and ``services/lambdas/claim_finalize.py``
            consumes from (BUILD_SPEC §7). Blank means no queue is configured yet — a
            deployment in that state must not break claim registration (rule #8's async
            close-out is best-effort), so ``enqueue_claim`` degrades to a logged no-op.
        memory_id: Bedrock AgentCore Memory resource id `MemoryFactWriter` (see
            `app/data/session_memory.py`) writes per-call fact events to. Blank
            disables — a deployment without the resource configured yet must not
            affect a call (same posture as `claim_queue_url`), so writes degrade to a
            logged no-op.
        memory_region: AWS region the memory resource lives in. Defaults to
            `aws_region`; separate for the same reason `bedrock_region` is — an
            in-account service may be provisioned in a specific region.
        memory_connect_timeout_secs: Connect timeout (s) for the Memory client. Its
            own, tighter budget than the DynamoDB knobs: Memory publishes no latency
            SLO, and this write fires on the call's very last turn (`on_call_ended`).
        memory_read_timeout_secs: Read timeout (s) for the Memory client.
        session_store: Which store backs resume-by-mobile (rule #5) — `dynamo`
            (default, `app/data/sessions.SessionRepository`) or `memory`
            (`app/data/session_memory.MemorySessionStore`). `dynamo` is the rollback
            target: flipping this back is an env var, not a redeploy.
        memory_semantic_strategy_id: AgentCore Memory SEMANTIC strategy id for this
            resource (see `handoff/PROJECT_STATE.md` D85). Blank disables recall --
            `retrieve_recall_facts` (see `app/data/session_memory.py`) returns `[]`
            when both strategy ids are blank, matching `memory_id`'s off-by-default
            posture.
        memory_preference_strategy_id: AgentCore Memory USER_PREFERENCE strategy id
            for this resource. Same blank-disables posture as
            `memory_semantic_strategy_id`.
        agent_framework: Which brain drives the call after the fixed disclosure
            (rule #11 pattern) — `langgraph` (default, the deterministic
            `StateGraph`) or `strands` (one Strands Agents SDK agent that decides
            what to say and which tool to call next from its own instructions,
            rather than a graph deciding for it). `langgraph` is the rollback
            target: flipping this back is an env var, not a redeploy.
        knowledge_base_id: Bedrock Knowledge Base id the `search_knowledge_base`
            MCP tool queries (see `app/data/knowledge_base.py`). Blank raises
            `ConfigError` only when that tool is actually called (`require_
            knowledge_base_id`) — every other tool keeps working without it.
        kb_region: AWS region the knowledge base lives in. Defaults to
            `aws_region`; separate for the same reason `bedrock_region` is — a
            given resource may be provisioned in a specific region.
        kb_max_results: Default cap on passages requested from the vector search
            when the tool call doesn't specify `max_results` itself.
        kb_min_score: Minimum search score a passage must clear to be returned.
            A noise floor, *not* a relevance gate: measured against this
            knowledge base, on-topic and off-topic scores overlap so heavily
            that no threshold separates them ("is a courtesy car included"
            tops out at 0.55, while "who won the world cup in 1998" reaches
            0.74 against the same insurance document). 0.6 -- the value this
            started at -- returned nothing for genuine questions. So this only
            trims the weakest tail of a result list, and what actually stops an
            irrelevant passage being spoken as an answer is the tool's own
            declared contract: passages may not answer the question, and the
            model is told to say so rather than compose from them. Re-measure
            after any change to the knowledge base or its search mode.
    """

    transport: str
    data_mode: str
    voice_provider: str
    openai_api_key: str
    dynamodb_endpoint: str
    local_port: int
    realtime_model: str
    tts_model: str
    voice: str
    audio_in_sample_rate: int
    audio_out_sample_rate: int
    aws_region: str
    aws_profile: str
    nova_sonic_model: str
    nova_sonic_voice: str
    polly_voice: str
    polly_engine: str
    policies_table: str
    sessions_table: str
    claims_table: str
    counters_table: str
    local_caller_number: str
    stt_provider: str
    whisper_model: str
    whisper_language: str
    deepgram_api_key: str
    deepgram_model: str
    deepgram_secret_name: str
    speechmatics_api_key: str
    speechmatics_model: str
    bedrock_region: str
    bedrock_access_key_id: str
    bedrock_secret_access_key: str
    bedrock_secret_name: str
    llm_provider: str
    bedrock_llm_model: str
    max_attempts: int
    no_input_timeout_seconds: int
    max_no_input_reprompts: int
    session_ttl_seconds: int
    vad_stop_secs: float
    utterance_gap_secs: float
    utterance_grace_secs: float
    vad_start_secs: float
    vad_confidence: float
    vad_min_volume: float
    smart_turn_enabled: bool
    barge_in_onset_debounce_secs: float
    enable_tracing: bool
    boto_connect_timeout_secs: float
    boto_read_timeout_secs: float
    boto_retry_max_attempts: int
    shutdown_grace_seconds: float
    metrics_namespace: str
    log_transcripts: bool
    debug_ssml: bool
    allow_text_injection: bool
    escalation_provider: str
    escalations_table: str
    escalation_ttl_seconds: int
    escalation_transcript_tail_lines: int
    agents_table: str
    callbacks_table: str
    force_agent_id: str
    escalation_queue_depth_limit: int
    bridge_hold_prompt_seconds: int
    bridge_wait_seconds: int
    reservation_timeout_seconds: int
    agent_connection_ttl_seconds: int
    otp_challenges_table: str
    otp_code_length: int
    otp_ttl_seconds: int
    otp_rate_limit_per_hour: int
    otp_rate_limit_window_seconds: int
    otp_sender_email: str
    otp_email_enabled: bool
    notify_provider: str
    notify_sender_email: str
    notify_region: str
    claim_queue_url: str
    memory_id: str
    memory_region: str
    memory_connect_timeout_secs: float
    memory_read_timeout_secs: float
    session_store: str
    memory_semantic_strategy_id: str
    memory_preference_strategy_id: str
    agent_framework: str
    knowledge_base_id: str
    kb_region: str
    kb_max_results: int
    kb_min_score: float

    def require_openai_api_key(self) -> str:
        """Return the OpenAI key or raise if it is not configured.

        Called by components that actually open OpenAI connections, so importing the
        app and running pure-logic tests never requires a key.
        """
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set. Add it to .env (see .env.example).")
        return self.openai_api_key

    def require_otp_sender_email(self) -> str:
        """Return the SES sender address or raise if it is not configured."""
        if not self.otp_sender_email:
            raise ConfigError("OTP_SENDER_EMAIL is not set.")
        return self.otp_sender_email

    def require_notify_sender_email(self) -> str:
        """Return the SES sender address for claim confirmations, or raise if unset."""
        if not self.notify_sender_email:
            raise ConfigError("NOTIFY_SENDER_EMAIL is not set.")
        return self.notify_sender_email

    def require_knowledge_base_id(self) -> str:
        """Return the Knowledge Base id or raise if it is not configured."""
        if not self.knowledge_base_id:
            raise ConfigError("KNOWLEDGE_BASE_ID is not set.")
        return self.knowledge_base_id


def load_config() -> AppConfig:
    """Build an :class:`AppConfig` from the current environment.

    Returns:
        The resolved configuration. Does not require ``OPENAI_API_KEY`` so that
        imports/tests work without secrets; use ``require_openai_api_key`` at the
        point of use.
    """
    return AppConfig(
        transport=_get("TRANSPORT", "local"),
        data_mode=_get("DATA_MODE", "local"),
        voice_provider=_get("VOICE_PROVIDER", "cascaded"),
        openai_api_key=_get("OPENAI_API_KEY", ""),
        dynamodb_endpoint=_get("DYNAMODB_ENDPOINT", "http://localhost:8000"),
        local_port=_get_int("LOCAL_PORT", 7860),
        realtime_model=_get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
        tts_model=_get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        voice=_get("OPENAI_VOICE", "alloy"),
        audio_in_sample_rate=_get_int("AUDIO_IN_SAMPLE_RATE", 24000),
        audio_out_sample_rate=_get_int("AUDIO_OUT_SAMPLE_RATE", 24000),
        aws_region=_get("AWS_REGION", "us-east-1"),
        aws_profile=_get("AWS_PROFILE", ""),
        nova_sonic_model=_get("NOVA_SONIC_MODEL", "amazon.nova-2-sonic-v1:0"),
        nova_sonic_voice=_get("NOVA_SONIC_VOICE", "matthew"),
        # Ava's voice (docs/CONVERSATION_DESIGN.md §1): Olivia/generative, not the
        # Joanna/neural placeholder rule #11's default started as. Matches
        # scripts/deploy_aws.sh's pinned runtime env -- code default kept in sync so
        # local dev sounds the same as production without exporting anything.
        polly_voice=_get("POLLY_VOICE", "Olivia"),
        polly_engine=_get("POLLY_ENGINE", "generative"),
        policies_table=_get("POLICIES_TABLE", "policies"),
        sessions_table=_get("SESSIONS_TABLE", "sessions"),
        claims_table=_get("CLAIMS_TABLE", "claims"),
        counters_table=_get("COUNTERS_TABLE", "counters"),
        local_caller_number=_get("LOCAL_CALLER_NUMBER", ""),
        stt_provider=_get("STT_PROVIDER", "whisper_local"),
        whisper_model=_get("WHISPER_MODEL", "base"),
        whisper_language=_get("WHISPER_LANGUAGE", "en-AU"),
        deepgram_api_key=_get("DEEPGRAM_API_KEY", ""),
        deepgram_model=_get("DEEPGRAM_MODEL", ""),
        deepgram_secret_name=_get("DEEPGRAM_SECRET_NAME", ""),
        speechmatics_api_key=_get("SPEECHMATICS_API_KEY", ""),
        speechmatics_model=_get("SPEECHMATICS_MODEL", ""),
        bedrock_region=_get("BEDROCK_REGION", _get("AWS_REGION", "us-east-1")),
        # Bedrock may live in a different AWS account than the data layer (D50). Deliberately
        # NOT the standard AWS_* names: those would redirect DynamoDB and Polly too.
        bedrock_access_key_id=_get("BEDROCK_ACCESS_KEY_ID", ""),
        bedrock_secret_access_key=_get("BEDROCK_SECRET_ACCESS_KEY", ""),
        bedrock_secret_name=_get("BEDROCK_SECRET_NAME", ""),
        llm_provider=_get("LLM_PROVIDER", "bedrock"),
        bedrock_llm_model=_get("BEDROCK_LLM_MODEL", "us.amazon.nova-lite-v1:0"),
        max_attempts=_get_int("MAX_ATTEMPTS", 3),
        no_input_timeout_seconds=_get_int("NO_INPUT_TIMEOUT_SECONDS", 12),
        max_no_input_reprompts=_get_int("MAX_NO_INPUT_REPROMPTS", 3),
        session_ttl_seconds=_get_int("SESSION_TTL_SECONDS", 1800),
        # Longer end-of-speech window so multi-part input (e.g. a spoken phone number
        # with pauses between digit groups) isn't split into fragments by the VAD.
        vad_stop_secs=_get_float("VAD_STOP_SECS", 1.5),
        # Streaming STT finalizes mid-sentence, so transcript chunks are buffered into one
        # utterance before reaching the flow. The VAD-anchored grace is the normal path;
        # the longer gap only applies when no end-of-speech frame arrives.
        utterance_gap_secs=_get_float("UTTERANCE_GAP_SECS", 0.8),
        utterance_grace_secs=_get_float("UTTERANCE_GRACE_SECS", 0.35),
        # Pipecat's own default (0.2s) false-triggered on non-speech audio even with
        # headphones in (D43) — 0.4s requires a more sustained onset before Silero
        # commits to "user started speaking".
        vad_start_secs=_get_float("VAD_START_SECS", 0.4),
        vad_confidence=_get_float("VAD_CONFIDENCE", 0.7),
        vad_min_volume=_get_float("VAD_MIN_VOLUME", 0.6),
        smart_turn_enabled=_get_bool("SMART_TURN_ENABLED", False),
        # Onset debounce before barge-in arms on an interruptible turn (D33). Tuned
        # live against echo; headphones are the reliable fallback (O14).
        barge_in_onset_debounce_secs=_get_float("BARGE_IN_ONSET_DEBOUNCE_SECS", 0.35),
        enable_tracing=_get_bool("ENABLE_TRACING", True),
        # DynamoDB/Secrets Manager sit on a live caller turn: fail fast rather than
        # sit on botocore's 60s default read timeout (defect 3).
        boto_connect_timeout_secs=_get_float("BOTO_CONNECT_TIMEOUT_SECS", 1.0),
        boto_read_timeout_secs=_get_float("BOTO_READ_TIMEOUT_SECS", 2.0),
        boto_retry_max_attempts=_get_int("BOTO_RETRY_MAX_ATTEMPTS", 2),
        shutdown_grace_seconds=_get_float("SHUTDOWN_GRACE_SECONDS", 30.0),
        metrics_namespace=_get("METRICS_NAMESPACE", "InsuranceVoiceAgent"),
        log_transcripts=_get_bool("LOG_TRANSCRIPTS", False),
        debug_ssml=_get_bool("DEBUG_SSML", False),
        allow_text_injection=_get_bool("ALLOW_TEXT_INJECTION", False),
        escalation_provider=_get("ESCALATION_PROVIDER", "noop"),
        escalations_table=_get("ESCALATIONS_TABLE", "escalations"),
        # ~30 days, matching the other tables' retention (rule #10 window, not indefinite).
        escalation_ttl_seconds=_get_int("ESCALATION_TTL_SECONDS", 30 * 24 * 60 * 60),
        escalation_transcript_tail_lines=_get_int("ESCALATION_TRANSCRIPT_TAIL_LINES", 8),
        agents_table=_get("AGENTS_TABLE", "agents"),
        callbacks_table=_get("CALLBACKS_TABLE", "callbacks"),
        force_agent_id=_get("FORCE_AGENT_ID", ""),
        # No real agent-headcount signal yet (Milestone F queue is small in practice) —
        # a flat depth cap, not per-skill capacity.
        escalation_queue_depth_limit=_get_int("ESCALATION_QUEUE_DEPTH_LIMIT", 20),
        bridge_hold_prompt_seconds=_get_int("BRIDGE_HOLD_PROMPT_SECONDS", 25),
        bridge_wait_seconds=_get_int("BRIDGE_WAIT_SECONDS", 180),
        reservation_timeout_seconds=_get_int("RESERVATION_TIMEOUT_SECONDS", 30),
        agent_connection_ttl_seconds=_get_int("AGENT_CONNECTION_TTL_SECONDS", 60 * 60),
        otp_challenges_table=_get("OTP_CHALLENGES_TABLE", "otp_challenges"),
        otp_code_length=_get_int("OTP_CODE_LENGTH", 6),
        otp_ttl_seconds=_get_int("OTP_TTL_SECONDS", 10 * 60),
        otp_rate_limit_per_hour=_get_int("OTP_RATE_LIMIT_PER_HOUR", 5),
        otp_rate_limit_window_seconds=_get_int("OTP_RATE_LIMIT_WINDOW_SECONDS", 60 * 60),
        otp_sender_email=_get("OTP_SENDER_EMAIL", ""),
        # Off by default: SES is sandboxed with zero verified identities (D61,
        # docs/SES_SETUP.md). While off, otp_start skips SES and returns the code
        # directly instead — see AppConfig.otp_email_enabled's docstring.
        otp_email_enabled=_get_bool("OTP_EMAIL_ENABLED", False),
        notify_provider=_get("NOTIFY_PROVIDER", "noop"),
        notify_sender_email=_get("NOTIFY_SENDER_EMAIL", ""),
        # Mirrors bedrock_region's shape/reasoning (see the docstring): the real sender
        # identity in this account is SES-verified in us-west-2, not us-east-1/aws_region.
        notify_region=_get("NOTIFY_REGION", _get("AWS_REGION", "us-east-1")),
        claim_queue_url=_get("CLAIM_QUEUE_URL", ""),
        memory_id=_get("MEMORY_ID", ""),
        memory_region=_get("MEMORY_REGION", _get("AWS_REGION", "us-east-1")),
        # Memory publishes no latency SLO and this write fires on the call's very
        # last turn (on_call_ended) -- its own tighter budget, not DynamoDB's.
        memory_connect_timeout_secs=_get_float("MEMORY_CONNECT_TIMEOUT_SECS", 0.5),
        memory_read_timeout_secs=_get_float("MEMORY_READ_TIMEOUT_SECS", 1.0),
        session_store=_get("SESSION_STORE", "dynamo"),
        memory_semantic_strategy_id=_get("MEMORY_SEMANTIC_STRATEGY_ID", ""),
        memory_preference_strategy_id=_get("MEMORY_PREFERENCE_STRATEGY_ID", ""),
        agent_framework=_get("AGENT_FRAMEWORK", "langgraph"),
        knowledge_base_id=_get("KNOWLEDGE_BASE_ID", "GT2Q6SH5AH"),
        kb_region=_get("KB_REGION", _get("AWS_REGION", "us-west-2")),
        kb_max_results=_get_int("KB_MAX_RESULTS", 5),
        kb_min_score=_get_float("KB_MIN_SCORE", 0.4),
    )
