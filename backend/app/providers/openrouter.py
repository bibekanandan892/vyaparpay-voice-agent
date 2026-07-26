"""OpenRouterLLM — the `LLMProvider` implementation over OpenRouter's
OpenAI-compatible `/chat/completions` streaming endpoint.

Docs: docs/04-backend-architecture.md §4 (the DI/lifespan pattern and this
exact class sketch — reproduced faithfully below, only adapted to pull the
API key out of the frozen `Settings.openrouter_api_key: SecretStr` via
`.get_secret_value()` instead of interpolating it directly, since §4's
prose sketch predates the SecretStr hardening in app/config.py), docs/05
§3.4/§3.8 (why the `models` fallback array and the `usage` frame exist —
LLMRouter and CostTracker are the consumers, not this module).

Scope note (interface pin, docs/05 §3.4 / app/domain/interfaces.py's
`LLMRouterProto` docstring): this class does **not** reassemble streamed
`tool_calls` fragments into whole `ToolCall`s. Real OpenAI-compatible
streaming responses split a single tool call's `function.arguments` across
multiple delta chunks, keyed by `tool_calls[i].index` — stitching those
fragments back into one JSON-parseable string is explicitly `LLMRouter`'s
job (a later batch, task 4.3), per both the plan ("reassembly of streamed
tool_call fragments" is called out as LLMRouter's responsibility) and the
frozen `TokenEvent`/`ToolCall` docstrings in app/domain/types.py. This
module's contract ends at faithfully forwarding each chunk's raw
`delta` dict inside a `TokenEvent` — reassembly logic does not belong
here and none is guessed at.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson

from app.config import Settings
from app.domain.types import LLMEvent, TokenEvent, UsageEvent
from app.obs.logging import get_logger

log = get_logger(__name__)


class OpenRouterLLM:
    """Implements `app.domain.interfaces.LLMProvider`.

    Takes a shared/pooled `httpx.AsyncClient` (built once in the app
    lifespan, docs/04 §4) rather than owning its own client — this class
    never opens or closes a connection pool itself.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._url = f"{settings.openrouter_base_url}/chat/completions"
        # The unwrapped key is deliberately NOT stored here (an earlier
        # version cached it in a `self._headers` dict, contradicting this
        # exact comment — fixed after review). `.get_secret_value()` is
        # called fresh inside stream(), each request, so the plaintext
        # value only ever exists as a short-lived local — never as
        # long-lived state on an instance the app lifespan holds for the
        # life of the process (app/config.py's SecretStr rule).
        self._settings = settings

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """docs/04 §4. `messages` arrives already wire-shaped
        (`Message.to_wire()` applied by the caller — see `LLMProvider`'s
        docstring in app/domain/interfaces.py); this method does no
        Message-domain-type handling of its own.

        `models` is the fallback array (primary dialogue/utility model
        first, then configured fallbacks) — a primary-model 5xx re-routes
        at the OpenRouter edge inside this single HTTP call, never visible
        to this method as a retry.
        """
        body: dict[str, Any] = {
            "models": models,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "usage": {"include": True},  # gateway streams a final usage frame
        }
        headers = {
            "Authorization": f"Bearer {self._settings.openrouter_api_key.get_secret_value()}"
        }
        async with self._http.stream(
            "POST", self._url, headers=headers, json=body
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                raw = line[6:]
                try:
                    chunk = orjson.loads(raw)
                    if chunk.get("usage"):
                        event: LLMEvent = UsageEvent(model=chunk["model"], usage=chunk["usage"])
                    else:
                        event = TokenEvent(delta=chunk["choices"][0]["delta"])
                except (orjson.JSONDecodeError, KeyError, IndexError) as exc:
                    # Fail loudly (project convention: never silently
                    # swallow errors) but with the raw line attached —
                    # without this, the original crash gave no way to
                    # tell which upstream frame caused it. The raw SSE
                    # line is upstream-generated data, not a secret, so
                    # it's safe to log; never log `headers` (the bearer
                    # token) here or anywhere else in this method.
                    log.error(
                        "openrouter.stream.malformed_chunk",
                        raw_line=raw,
                        error=str(exc),
                    )
                    raise
                yield event


__all__ = ["OpenRouterLLM"]
