"""Settings — single pydantic-settings BaseSettings, env-only, fail-fast on
missing secrets (they have no default, so Settings() raises at import time
if unset — the process dies at startup, not on first request).

Phase-2 env subset only (docs/04 §3) — signaling/TURN/coturn vars are
Phase-3-only and deliberately absent here.
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
    redis_url: str = "redis://redis:6379/0"

    openrouter_api_key: SecretStr
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_dialogue_model: str = "anthropic/claude-sonnet-5"
    openrouter_utility_model: str = "anthropic/claude-haiku-4-5"
    # comma-separated fallback slugs, e.g. "openai/gpt-5.1,google/gemini-3-pro"
    openrouter_dialogue_fallbacks: str = ""

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "agent-api"

    session_ttl_seconds: int = 86400
    rate_limit_sessions_per_min: int = 5
    call_cost_cap_usd: Decimal = Decimal("1.00")

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
    return Settings()
