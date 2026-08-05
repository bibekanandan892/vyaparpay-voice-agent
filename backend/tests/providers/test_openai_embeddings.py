"""Wire-level tests for `OpenAIEmbeddings.embed()` — respx stubs the
`/embeddings` HTTP call, no real network and no API key involved
(docs/04-backend-architecture.md §4, §9.4). Same strategy as
`test_openrouter.py`, for the same reason: there is no OpenAI key on the
development machine, so the class has to be fully exercisable against a
stub or it is untested until deploy.

The load-bearing tests here are the three that guard
`EmbeddingProvider`'s positional-alignment contract (`result[i]` is the
embedding of `texts[i]`, `len(result) == len(texts)`). Its failure mode is
silent: the seed embedder and the post-call embed stage both zip the result
back against their own source rows, so a transposition stores one chunk's
text with another's vector and every later retrieval is quietly wrong with
no error anywhere.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson
import pytest
import respx

from app.config import Settings
from app.domain.types import EMBEDDING_DIM
from app.providers.openai_embeddings import OpenAIEmbeddings
from tests.conftest import SettingsFactory


def _vector(fill: float, width: int = EMBEDDING_DIM) -> list[float]:
    return [fill] * width


def _response(*items: tuple[int, list[float]]) -> dict[str, Any]:
    """An `/embeddings` response body from (index, vector) pairs, in the
    order given — so a test can hand back a deliberately scrambled `data`
    array and still describe which vector belongs to which input."""
    return {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector} for index, vector in items
        ],
        "usage": {"prompt_tokens": 8, "total_tokens": 8},
    }


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client


# --------------------------------------------------------------------------
# Construction — fail fast on a missing key
# --------------------------------------------------------------------------


def test_constructor_raises_when_no_api_key_is_configured(
    settings_factory: SettingsFactory, http_client: httpx.AsyncClient
) -> None:
    """`openai_api_key` defaults to None (agent-api and the whole standard
    suite boot with no OpenAI env), so the guard is at construction — the
    same shape as `DeepgramStt._require_api_key`. Failing at the app
    lifespan beats failing on the first post-call embed, hours later."""
    settings = settings_factory(openai_api_key=None)

    with pytest.raises(ValueError, match="OPENAI_API_KEY is not configured"):
        OpenAIEmbeddings(http_client, settings)


# --------------------------------------------------------------------------
# The request this class sends
# --------------------------------------------------------------------------


async def test_embed_sends_one_batched_request_with_the_configured_model(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """`EmbeddingProvider` is batched by construction: N texts must be ONE
    HTTP call, not N. The seed embedder pushes ~180 KB chunks through this
    in a single pass."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200, json=_response((0, _vector(0.1)), (1, _vector(0.2)), (2, _vector(0.3)))
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)
    await embeddings.embed(["limits reset at midnight", "settlement T+1", "refund window"])

    assert openai_embeddings_route.call_count == 1
    request = openai_embeddings_route.calls.last.request
    assert request.url == f"{settings.openai_base_url}/embeddings"
    assert request.headers["Authorization"] == "Bearer test-openai-key"

    body = orjson.loads(request.content)
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == ["limits reset at midnight", "settlement T+1", "refund window"]
    assert body["encoding_format"] == "float"


async def test_embed_does_not_send_a_dimensions_parameter(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Judgment call 4 (module docstring), pinned because it looks like an
    omission. Sending `dimensions: 1536` would make a misconfigured
    `openai_embedding_model=text-embedding-3-large` return 1536-wide
    vectors that pass every width check in the system while being
    geometrically incomparable with the stored corpus. Omitting it makes
    that misconfiguration return 3072 floats and die loudly."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(200, json=_response((0, _vector(0.1))))
    )

    embeddings = OpenAIEmbeddings(http_client, settings)
    await embeddings.embed(["one"])

    assert "dimensions" not in orjson.loads(openai_embeddings_route.calls.last.request.content)


async def test_embed_uses_the_configured_model_id_not_a_hardcoded_one(
    respx_mock: respx.MockRouter,
    settings_factory: SettingsFactory,
    http_client: httpx.AsyncClient,
) -> None:
    """A model id is config, never a constant (canon §5) — the field's
    default is the frozen `EMBEDDING_MODEL`, but the provider must read the
    field, not the constant."""
    settings = settings_factory(openai_embedding_model="text-embedding-3-large")
    route = respx_mock.post(f"{settings.openai_base_url}/embeddings").mock(
        return_value=httpx.Response(200, json=_response((0, _vector(0.1, width=3072))))
    )

    embeddings = OpenAIEmbeddings(http_client, settings)
    with pytest.raises(ValueError, match="wrong width"):
        await embeddings.embed(["one"])

    assert orjson.loads(route.calls.last.request.content)["model"] == "text-embedding-3-large"


async def test_embed_never_logs_the_api_key(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The error path logs the raw response body for diagnosis; it must
    never log `headers`. Exercised on the failing path specifically,
    because that is the only path in this module that logs at all."""
    openai_embeddings_route.mock(return_value=httpx.Response(200, content=b"not json"))

    embeddings = OpenAIEmbeddings(http_client, settings)
    with pytest.raises(orjson.JSONDecodeError):
        await embeddings.embed(["one"])

    captured = capsys.readouterr()
    assert "test-openai-key" not in captured.out + captured.err
    assert "openai_embeddings.malformed_response" in captured.out


# --------------------------------------------------------------------------
# Positional alignment — the contract whose failure is silent
# --------------------------------------------------------------------------


async def test_embed_returns_vectors_positionally_aligned_with_texts(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200, json=_response((0, _vector(0.1)), (1, _vector(0.2)), (2, _vector(0.3)))
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)
    result = await embeddings.embed(["a", "b", "c"])

    assert len(result) == 3
    assert [vector[0] for vector in result] == [0.1, 0.2, 0.3]
    # Tuples, not lists — `Embedding` is `tuple[float, ...]`, the same
    # frozen-value discipline the rest of app/domain/types.py applies.
    assert all(isinstance(vector, tuple) for vector in result)


async def test_embed_reorders_a_scrambled_response_by_its_index_field(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """**The transposition guard.** OpenAI's `data` array carries an
    explicit `index` per element, which only exists because array order is
    not the contract. A provider that trusted arrival order would zip
    chunk A's text to chunk B's vector, and nothing downstream — not
    `MemoryChunk`, not the `vector(1536)` column, not retrieval — would
    report an error. Every stored vector would just be attached to the
    wrong text.
    """
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200,
            # Deliberately out of order: 2, 0, 1.
            json=_response((2, _vector(0.3)), (0, _vector(0.1)), (1, _vector(0.2))),
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)
    result = await embeddings.embed(["a", "b", "c"])

    assert [vector[0] for vector in result] == [0.1, 0.2, 0.3]


async def test_embed_raises_when_the_response_drops_an_input(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """A vendor that silently skipped an over-long or empty input shifts
    every later position by one — the transposition bug with an off-by-one
    instead of a permutation."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(200, json=_response((0, _vector(0.1)), (1, _vector(0.2))))
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(ValueError, match="not positionally aligned"):
        await embeddings.embed(["a", "b", "c"])


async def test_embed_raises_on_duplicate_indices(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Two elements claiming index 0 means one input's vector was never
    returned, whichever way the duplicate is resolved."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200, json=_response((0, _vector(0.1)), (0, _vector(0.9)), (2, _vector(0.3)))
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(ValueError, match="not positionally aligned"):
        await embeddings.embed(["a", "b", "c"])


async def test_embed_raises_when_an_index_is_outside_the_expected_range(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """Indices `{0, 1, 3}` for three inputs: three distinct elements, the
    right *count*, and position 2 missing. This is the case that makes the
    check a set comparison rather than `len(data) == len(texts)` — a count
    check passes here and then `KeyError: 2` surfaces with no explanation
    of what the vendor actually did.
    """
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200, json=_response((0, _vector(0.1)), (1, _vector(0.2)), (3, _vector(0.3)))
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(ValueError, match="not positionally aligned"):
        await embeddings.embed(["a", "b", "c"])


async def test_embed_returns_empty_without_an_http_call_for_an_empty_batch(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """`len(result) == len(texts)` holds trivially, and the API rejects an
    empty `input` array with a 400 — so the short-circuit preserves the
    contract rather than papering over a failure."""
    embeddings = OpenAIEmbeddings(http_client, settings)

    assert await embeddings.embed([]) == []
    assert not openai_embeddings_route.called


# --------------------------------------------------------------------------
# Width — the only dimension check on the RETRIEVAL path
# --------------------------------------------------------------------------


async def test_embed_raises_on_a_wrong_width_vector(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """`EmbeddingProvider`'s docstring points at `MemoryChunk`'s validator
    and the `vector(1536)` column as the width enforcement — both on the
    WRITE path. `SemanticMemory.retrieve()` embeds a query and hands the
    vector straight to SQL without ever building a `MemoryChunk`, so on the
    read path this check is the only boundary before pgvector's own
    operator-level mismatch error.
    """
    openai_embeddings_route.mock(
        return_value=httpx.Response(200, json=_response((0, _vector(0.1, width=3072))))
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(ValueError, match="wrong width"):
        await embeddings.embed(["one"])


async def test_embed_reports_which_positions_had_the_wrong_width(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """One bad vector in a 180-chunk seed batch has to be findable."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(
            200,
            json=_response((0, _vector(0.1)), (1, _vector(0.2, width=8)), (2, _vector(0.3))),
        )
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(ValueError, match=r"\{1: 8\}"):
        await embeddings.embed(["a", "b", "c"])


# --------------------------------------------------------------------------
# Failure propagates — no degraded or partial result
# --------------------------------------------------------------------------


async def test_embed_raises_on_non_2xx_response(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """docs/09 §11 makes an embedding outage drop-rung 1 (skip the RAG slot
    entirely) — a decision for the context-assembly layer that owns the
    slot, not something this provider may pre-empt by returning `[]`."""
    openai_embeddings_route.mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(httpx.HTTPStatusError):
        await embeddings.embed(["one"])


async def test_embed_raises_with_context_on_a_malformed_body(
    openai_embeddings_route: respx.Route,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    openai_embeddings_route.mock(return_value=httpx.Response(200, json={"object": "list"}))

    embeddings = OpenAIEmbeddings(http_client, settings)

    with pytest.raises(KeyError):
        await embeddings.embed(["one"])
