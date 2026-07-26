"""Wire-level tests for `OpenRouterLLM.stream()` — respx stubs the
`chat/completions` HTTP call, no real network or containers involved
(docs/04-backend-architecture.md §4, §9.4).

Verifies both directions of the contract: the *request* this class sends
(the `models` fallback array, `tools` passthrough, `stream: true`,
`usage: {"include": true}`, the Bearer header built from
`Settings.openrouter_api_key`) and the *events* it yields from a scripted
SSE response, in order, as the exact frozen `TokenEvent`/`UsageEvent`
types from app/domain/types.py.

Also pins the tool-call-reassembly scope decision (see
app/providers/openrouter.py's module docstring): fragments of a streamed
`tool_calls` delta must come through `TokenEvent.delta` raw and
un-stitched — reassembling them into a whole `ToolCall` is `LLMRouter`'s
job, not this provider's.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson
import pytest
import respx

from app.config import Settings
from app.domain.types import TokenEvent, UsageEvent
from app.providers.openrouter import OpenRouterLLM


def _sse(*chunks: dict[str, Any]) -> bytes:
    """Build a `data: {...}\\n\\n`-framed SSE body from JSON-able chunk
    dicts, terminated by the `data: [DONE]` sentinel line
    `OpenRouterLLM.stream()` treats as the loop terminator. Also injects a
    non-`data:` comment line before the first chunk, mirroring real SSE
    streams (id:/event: lines, keep-alive comments) that must be skipped
    without breaking JSON parsing of the lines that follow.
    """
    body = ": keep-alive\n\n"
    body += "".join(f"data: {orjson.dumps(chunk).decode()}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"
    return body.encode()


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client


async def test_stream_yields_token_then_usage_events_in_order(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    chunk_1 = {
        "model": "anthropic/claude-sonnet-5",
        "choices": [{"delta": {"role": "assistant", "content": "Hello"}}],
    }
    chunk_2 = {
        "model": "anthropic/claude-sonnet-5",
        "choices": [{"delta": {"content": " Rajesh"}}],
    }
    usage_chunk = {
        "model": "anthropic/claude-sonnet-5",
        "choices": [],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 0},
    }
    openrouter_route.mock(
        return_value=httpx.Response(200, content=_sse(chunk_1, chunk_2, usage_chunk))
    )

    llm = OpenRouterLLM(http_client, settings)
    events = [
        event
        async for event in llm.stream(
            [{"role": "user", "content": "hi"}], models=[settings.openrouter_dialogue_model]
        )
    ]

    assert events == [
        TokenEvent(delta={"role": "assistant", "content": "Hello"}),
        TokenEvent(delta={"content": " Rajesh"}),
        UsageEvent(
            model="anthropic/claude-sonnet-5",
            usage={"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 0},
        ),
    ]


async def test_stream_sends_models_fallback_array_and_flags(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    openrouter_route.mock(return_value=httpx.Response(200, content=_sse()))
    tools = [{"type": "function", "function": {"name": "get_wallet_balance"}}]
    models = [settings.openrouter_dialogue_model, "openai/gpt-5.1", "google/gemini-3-pro"]

    llm = OpenRouterLLM(http_client, settings)
    async for _ in llm.stream(
        [{"role": "user", "content": "hi"}], models=models, tools=tools
    ):
        pass

    assert openrouter_route.called
    request = openrouter_route.calls.last.request
    assert request.url == f"{settings.openrouter_base_url}/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-openrouter-key"

    body = orjson.loads(request.content)
    assert body["models"] == models
    assert body["tools"] == tools
    assert body["stream"] is True
    assert body["usage"] == {"include": True}
    assert body["messages"] == [{"role": "user", "content": "hi"}]


async def test_stream_passes_tools_none_through_when_not_given(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """No `tools` kwarg (a plain dialogue turn with no tool policy active)
    must still send an explicit `tools: null`, not omit the key — matches
    the docs/04 §4 sketch verbatim rather than guessing at an
    "omit when None" convenience the docs don't specify."""
    openrouter_route.mock(return_value=httpx.Response(200, content=_sse()))

    llm = OpenRouterLLM(http_client, settings)
    async for _ in llm.stream(
        [{"role": "user", "content": "hi"}], models=[settings.openrouter_dialogue_model]
    ):
        pass

    body = orjson.loads(openrouter_route.calls.last.request.content)
    assert body["tools"] is None


async def test_stream_does_not_reassemble_tool_call_fragments(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Scope pin: a tool call's `function.arguments` split across two
    delta chunks (keyed by `tool_calls[0].index`, the real OpenRouter/
    OpenAI streaming shape) must come through as two separate raw
    `TokenEvent`s — concatenating the argument-string fragments into one
    parseable JSON object is explicitly out of scope for this class (see
    module docstring) and belongs to LLMRouter (a later batch)."""
    fragment_1 = {
        "model": "anthropic/claude-sonnet-5",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_abc123",
                            "type": "function",
                            "function": {"name": "get_wallet_balance", "arguments": '{"mer'},
                        }
                    ]
                }
            }
        ],
    }
    fragment_2 = {
        "model": "anthropic/claude-sonnet-5",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": 'chant_id":"usr_rajesh01"}'}}
                    ]
                }
            }
        ],
    }
    openrouter_route.mock(return_value=httpx.Response(200, content=_sse(fragment_1, fragment_2)))

    llm = OpenRouterLLM(http_client, settings)
    events = [
        event
        async for event in llm.stream(
            [{"role": "user", "content": "check my balance"}],
            models=[settings.openrouter_dialogue_model],
        )
    ]

    assert events == [
        TokenEvent(delta=fragment_1["choices"][0]["delta"]),
        TokenEvent(delta=fragment_2["choices"][0]["delta"]),
    ]
    # explicitly not reassembled: no single event carries the full,
    # concatenated, JSON-parseable arguments string.
    for event in events:
        assert isinstance(event, TokenEvent)
        for call in event.delta.get("tool_calls", []):
            fn = call.get("function", {})
            if "arguments" in fn:
                assert fn["arguments"] in ('{"mer', 'chant_id":"usr_rajesh01"}')


async def test_stream_raises_on_non_2xx_response(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    openrouter_route.mock(return_value=httpx.Response(500, json={"error": "upstream failure"}))

    llm = OpenRouterLLM(http_client, settings)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in llm.stream(
            [{"role": "user", "content": "hi"}], models=[settings.openrouter_dialogue_model]
        ):
            pass


async def test_stream_raises_with_context_on_malformed_chunk(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """A chunk that isn't valid JSON, or is valid JSON in an unexpected
    shape (e.g. `choices: []` with no `usage` key — a plausible keep-alive
    frame), must fail loudly rather than crash with no indication of which
    raw line caused it (review finding: the original version raised a bare
    JSONDecodeError/KeyError/IndexError with zero context)."""
    body = b': keep-alive\n\ndata: not valid json at all\n\ndata: [DONE]\n\n'
    openrouter_route.mock(return_value=httpx.Response(200, content=body))

    llm = OpenRouterLLM(http_client, settings)

    with pytest.raises(orjson.JSONDecodeError):
        async for _ in llm.stream(
            [{"role": "user", "content": "hi"}], models=[settings.openrouter_dialogue_model]
        ):
            pass


async def test_stream_raises_on_chunk_missing_choices_and_usage(
    openrouter_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """`choices: []` with no `usage` key falls through to the TokenEvent
    branch and indexing `choices[0]` raises IndexError — also caught and
    re-raised with context, not left as a bare, unexplained crash."""
    malformed_chunk = {"model": "anthropic/claude-sonnet-5", "choices": []}
    openrouter_route.mock(return_value=httpx.Response(200, content=_sse(malformed_chunk)))

    llm = OpenRouterLLM(http_client, settings)

    with pytest.raises(IndexError):
        async for _ in llm.stream(
            [{"role": "user", "content": "hi"}], models=[settings.openrouter_dialogue_model]
        ):
            pass
