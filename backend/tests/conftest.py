"""Root-level shared pytest fixtures (Phase-2 plan Batch 2, task 2.3).

Every later batch's test module is expected to pull from here instead of
re-inventing its own `Settings` boilerplate, respx route, or scripted LLM
double:

- `settings_factory` / `settings` — a minimal valid `Settings`
  (app/config.py fails fast on missing secrets at import time, so every
  test needs *some* concrete values for the three fields with no default)
  built from fixed test values, never a real `.env` — hermetic. Mirrors
  the `_settings(**overrides)` helper already used locally in
  tests/obs/test_logging.py; hoisted here so it isn't reinvented a third
  time.
- `openrouter_route` — a respx route pre-matched to `settings`'s
  `{openrouter_base_url}/chat/completions`, the one HTTP call
  `OpenRouterLLM.stream()` makes (docs/04 §4) and the one later
  `LLMRouter` tests will also need to stub. Built on top of respx's own
  `respx_mock` fixture (registered automatically by respx's `pytest11`
  plugin, see the installed `respx.plugin` module — this file does not
  redefine `respx_mock` itself, only adds the OpenRouter-specific route on
  top of it, since respx already fully supplies the base mock router).
- `fake_llm` — tests/fakes.py's `FakeLLM`, re-exported here so test
  modules can pull it straight from conftest instead of importing
  `tests.fakes` directly.

Docs: docs/04-backend-architecture.md §4 (provider DI / lifespan pattern),
§9.4 (respx as the provider-mocking strategy).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import respx

from app.config import Settings
from tests.fakes import FakeLLM

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def settings_factory() -> SettingsFactory:
    """Factory, not a bare instance, so a test that needs one field
    different (e.g. `openrouter_dialogue_fallbacks`, `env`) doesn't have to
    reconstruct all the required-field boilerplate — just
    `settings_factory(openrouter_dialogue_fallbacks="openai/gpt-5.1")`."""

    def _make(**overrides: object) -> Settings:
        defaults: dict[str, object] = {
            "jwt_secret": "test-jwt-secret",
            "database_url": "postgresql+asyncpg://test:test@localhost/test",
            "openrouter_api_key": "test-openrouter-key",
            "openrouter_base_url": "https://openrouter.test/api/v1",
        }
        defaults.update(overrides)
        return Settings(**defaults)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """The common case — no field overrides needed."""
    return settings_factory()


@pytest.fixture
def openrouter_route(respx_mock: respx.MockRouter, settings: Settings) -> respx.Route:
    """Pre-registered POST route for OpenRouterLLM's one HTTP call
    (`{openrouter_base_url}/chat/completions`). Nothing is stubbed by
    default — each test configures the response via
    `openrouter_route.mock(return_value=...)` or `.side_effect(...)`; an
    un-configured call still fails loudly via respx's `assert_all_mocked`
    default rather than silently reaching the real network.
    """
    return respx_mock.post(f"{settings.openrouter_base_url}/chat/completions")


@pytest.fixture
def fake_llm() -> FakeLLM:
    """A fresh, unscripted `FakeLLM` per test — see tests/fakes.py."""
    return FakeLLM()


__all__ = ["SettingsFactory"]
