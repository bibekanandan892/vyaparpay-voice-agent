"""Settings — single pydantic-settings BaseSettings, env-only, fail-fast on
missing secrets (they have no default, so Settings() raises at import time
if unset — the process dies at startup, not on first request).

Phase-2 vars (docs/04 §3) plus the Phase-3 voice set (STT/TTS providers,
signaling/TURN, VAD tuning). Every Phase-3 field is optional-with-default:
agent-api and the whole Phase-2 test suite boot with no voice env at all;
the voice worker fail-fasts on the subset it needs at its own startup.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"

    # SecretStr, not str: these render as "**********" in repr()/str()/
    # model_dump(), so an accidental `logger.debug(settings)` or an
    # exception traceback capturing a local `settings` variable can't leak
    # the JWT signing secret, DB credentials, or LLM API key. Call
    # .get_secret_value() only at the point of actual use (JWT encode/
    # decode, SQLAlchemy engine construction, the OpenRouter HTTP client).
    jwt_secret: SecretStr
    database_url: SecretStr
    # SecretStr (not str, fixed after review): today's compose Redis has no
    # auth, so nothing leaks yet — but any real deployment needs
    # redis://:password@host or rediss://user:pass@host, and this field sat
    # unprotected right next to database_url/openrouter_api_key, which
    # already get the masking documented above.
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")

    openrouter_api_key: SecretStr
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_dialogue_model: str = "anthropic/claude-sonnet-5"
    openrouter_utility_model: str = "anthropic/claude-haiku-4-5"
    # comma-separated fallback slugs, e.g. "openai/gpt-5.1,google/gemini-3-pro"
    openrouter_dialogue_fallbacks: str = ""

    # LLM pricing, USD per MILLION tokens — config, never constants in
    # CostTracker logic (canon §5; added by task 4.3). Defaults are the
    # standard list prices docs/05 §3.4's table headlines (Sonnet 5 $3/M
    # in, $15/M out; Haiku 4.5 $1/M in, $5/M out), NOT the temporary intro
    # discount ($2/$10 through 2026-08-31): a deployment on the discount
    # overrides via env rather than a hardcoded default silently going
    # stale on expiry. Cached-input reads are priced at 0.1x the base
    # input rate — Anthropic's published prompt-caching cache-read
    # multiplier (assumption documented per task 4.3; docs/05 §3.4's
    # table gives no cached price of its own).
    llm_dialogue_input_usd_per_mtok: Decimal = Decimal("3.00")
    llm_dialogue_cached_input_usd_per_mtok: Decimal = Decimal("0.30")
    llm_dialogue_output_usd_per_mtok: Decimal = Decimal("15.00")
    llm_utility_input_usd_per_mtok: Decimal = Decimal("1.00")
    llm_utility_cached_input_usd_per_mtok: Decimal = Decimal("0.10")
    llm_utility_output_usd_per_mtok: Decimal = Decimal("5.00")

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "agent-api"

    session_ttl_seconds: int = 86400
    rate_limit_sessions_per_min: int = 5
    call_cost_cap_usd: Decimal = Decimal("1.00")

    # --- Phase-3 voice (docs/06) — every field below is optional-with-
    # default so agent-api boots with no voice env at all; the voice
    # worker fail-fasts on the ones it needs at its own startup. The API
    # keys and TURN secret are SecretStr for the same masking reason
    # documented above jwt_secret.

    # STT — Deepgram streaming (docs/04 §3, docs/06 §1).
    deepgram_api_key: SecretStr | None = None
    deepgram_model: str = "nova-3"
    deepgram_url: str = "wss://api.deepgram.com/v1/listen"

    # TTS — ElevenLabs Flash (docs/04 §3, docs/06 §1); fallback voice id
    # is used when the primary voice errors, empty = no fallback.
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str = ""
    elevenlabs_fallback_voice_id: str = ""
    elevenlabs_url: str = "https://api.elevenlabs.io"
    # Streaming synthesis model (docs/06 §1/§4.1: Flash v2.5, the lowest-
    # latency tier). A model id is config, never a constant (canon §5) —
    # same rule as openrouter_dialogue_model above. Added by T4.1.
    elevenlabs_model_id: str = "eleven_flash_v2_5"

    # WebRTC / signaling (docs/06 §2): agent-api mints short-lived
    # signaling tokens and HMAC TURN credentials (docs/04 §3);
    # session_grace_s is the reconnect window after transport loss
    # (docs/06 §8).
    turn_secret: SecretStr | None = None
    coturn_host: str = ""
    signaling_public_url: str = ""
    signaling_token_ttl_s: int = 300
    turn_credential_ttl_s: int = 600
    session_grace_s: int = 30
    # Where the voice-worker's /v1/signal WebSocket listens (app/voice/run.py).
    # 0.0.0.0 because a real phone must reach it (docker-compose.yml publishes
    # 8080:8080 — keep the port in lockstep with that mapping).
    signaling_bind_host: str = "0.0.0.0"
    signaling_bind_port: int = 8080

    # VAD / endpointing (docs/06 §5-§6) — config, never constants in
    # VadEndpointer/barge-in logic (canon §5), so a demo-day tune is an
    # env edit, not a code change.
    vad_threshold: float = 0.5
    min_speech_ms: int = 200
    endpoint_silence_ms: int = 250
    max_endpoint_delay_ms: int = 2000
    barge_in_debounce_ms: int = 100

    @property
    def dialogue_models(self) -> list[str]:
        """The `models: [...]` fallback array for OpenRouterLLM.stream() —
        primary dialogue model first, then configured fallbacks."""
        fallbacks = [m.strip() for m in self.openrouter_dialogue_fallbacks.split(",") if m.strip()]
        return [self.openrouter_dialogue_model, *fallbacks]


@lru_cache
def get_settings() -> Settings:
    """Process-singleton settings, built once (docs/04 §4's lifespan calls
    this once and stores the result on app.state)."""
    # call-arg ignored: pydantic-settings populates the three
    # required-no-default fields (jwt_secret, database_url,
    # openrouter_api_key) from the environment at runtime — the
    # fail-fast-on-missing behavior this module's docstring promises —
    # which mypy's constructor signature can't see. The standard
    # pydantic-settings pattern; anything else missing still raises
    # ValidationError at startup exactly as before.
    return Settings()  # type: ignore[call-arg]
