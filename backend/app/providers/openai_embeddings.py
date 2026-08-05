"""OpenAIEmbeddings — the `EmbeddingProvider` implementation over OpenAI's
`POST /v1/embeddings` endpoint (`text-embedding-3-small`, 1536-dim, docs/09
§6.1, docs/04-backend-architecture.md §4).

Same shape as `app/providers/openrouter.py`: one vendor file behind one
Protocol, a shared/pooled `httpx.AsyncClient` injected rather than owned,
the model id in `Settings`, and the API key unwrapped from `SecretStr` only
as a short-lived local inside the request method — never cached on the
instance, never logged.

Judgment calls, recorded so review argues with the reasoning and not the
diff:

1. **Responses are re-ordered by `index`, not trusted as they arrive.**
   `EmbeddingProvider`'s contract is positional: `result[i]` is the
   embedding of `texts[i]`. OpenAI's response is a `data` array whose
   elements each carry an explicit `index`, which only exists because the
   array's own order is not the contract. Sorting by it costs nothing and
   turns "the vendor happened to return them in order" into something this
   module actually guarantees. The failure it prevents is silent and
   severe: the seed embedder and the post-call embed stage both zip the
   result back against their own source rows, so a transposition stores
   chunk A's text with chunk B's vector and every later retrieval is
   quietly wrong with no error anywhere.

2. **Count mismatch is a hard failure.** `len(result) == len(texts)` is
   the other half of the same contract. A vendor that dropped an
   over-long or empty input would shift every subsequent position by one,
   which is the transposition bug above with an off-by-one instead of a
   permutation. Checked explicitly rather than discovered downstream.

3. **The 1536-width check lives here, and it is load-bearing on the read
   path.** `EmbeddingProvider`'s docstring notes the Protocol cannot
   express the dimension and points at `MemoryChunk`'s validator and the
   `vector(1536)` column as the enforcement. Both are on the *write* path.
   `SemanticMemory.retrieve()` embeds a query and hands the vector
   straight to SQL — it never builds a `MemoryChunk` — so on the read path
   this check is the only boundary before pgvector's own operator-level
   mismatch error. It is a width check only: it cannot detect a
   *different* model of the same width (see judgment call 5).

4. **`dimensions` is deliberately NOT sent.** The API accepts it to
   shorten `text-embedding-3-*` output, and sending `dimensions: 1536`
   would pin the width against a hypothetical vendor default change. It
   was rejected because it trades a loud failure for a silent one: with
   `dimensions` set, a deployment that misconfigured
   `OPENAI_EMBEDDING_MODEL=text-embedding-3-large` would get 1536-wide
   vectors that pass every check in this file, in `MemoryChunk`, and at
   the DB column — while being geometrically incomparable with every
   vector already stored. Without it, that misconfiguration returns
   3072 floats and dies at judgment call 3's check. The realistic hazard
   is the misconfigured model, not a vendor changing a documented default.

5. **What this module does not check.** A same-width different embedding
   model (or a vendor-side model revision) produces vectors that are the
   right shape and the wrong geometry. Nothing here, in `MemoryChunk`, or
   in the `vector(1536)` column can see that; it surfaces only as
   retrieval quality quietly degrading. That is a re-embed decision
   (app/config.py's note on `openai_embedding_model`), not something this
   file enforces — stated because it would be easy to read the checks
   above as covering more than they do.

6. **`embed([])` returns `[]` without an HTTP call.** The API rejects an
   empty `input` array with a 400, and the identity `len(result) ==
   len(texts)` holds trivially. This is the one short-circuit in the
   module and it preserves the contract exactly rather than papering over
   a failure.

Failure behavior is the caller's, per `EmbeddingProvider`'s docstring and
docs/09 §11: an embedding outage skips the RAG slot entirely (drop-rung 1).
This module therefore raises — it never returns a degraded or partial
result — and `SemanticMemory` does not catch it either; the drop-rung
belongs to the context-assembly layer that owns the slot.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import orjson
from pydantic import SecretStr

from app.config import Settings
from app.domain.types import EMBEDDING_DIM, Embedding
from app.obs.logging import get_logger

log = get_logger(__name__)

# Vendor-pinned: float, not the base64 form some clients default to. Sent
# explicitly so a client-library or API default change cannot silently turn
# `embedding` into a base64 string that orjson would hand back as a str.
_ENCODING_FORMAT: Final = "float"


class OpenAIEmbeddings:
    """Implements `app.domain.interfaces.EmbeddingProvider`.

    Takes a shared/pooled `httpx.AsyncClient` (built once in the app
    lifespan, docs/04 §4) rather than owning its own client — this class
    never opens or closes a connection pool itself.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._require_api_key(settings)  # fail fast at construction, not first embed
        self._http = http
        self._url = f"{settings.openai_base_url}/embeddings"
        # The unwrapped key is deliberately NOT stored (openrouter.py's
        # SecretStr discipline): `.get_secret_value()` is called fresh
        # inside embed(), so the plaintext only ever exists as a
        # short-lived local, never as long-lived state on an instance the
        # app lifespan holds for the life of the process.
        self._settings = settings

    @staticmethod
    def _require_api_key(settings: Settings) -> SecretStr:
        key = settings.openai_api_key
        if key is None:
            raise ValueError(
                "OPENAI_API_KEY is not configured — OpenAIEmbeddings cannot start; "
                "set it in agent-api's environment (docs/04 §3)."
            )
        return key

    async def embed(self, texts: list[str]) -> list[Embedding]:
        """One HTTP call for the whole batch (`EmbeddingProvider` is
        batched by construction — the seed embedder pushes ~180 KB chunks
        through here in one pass).

        Returns vectors positionally aligned with `texts`: `result[i]` is
        the embedding of `texts[i]`, and `len(result) == len(texts)`. See
        judgment calls 1-3 in the module docstring for how that is
        guaranteed rather than assumed.
        """
        if not texts:
            return []  # judgment call 6

        body = {
            "model": self._settings.openai_embedding_model,
            "input": texts,
            "encoding_format": _ENCODING_FORMAT,
        }
        headers = {
            "Authorization": f"Bearer {self._require_api_key(self._settings).get_secret_value()}"
        }
        response = await self._http.post(self._url, headers=headers, json=body)
        response.raise_for_status()
        return _parse_embeddings(response.content, expected=len(texts))


def _parse_embeddings(payload: bytes, *, expected: int) -> list[Embedding]:
    """Decode the response body into positionally-aligned vectors.

    Split out of `embed` so the ordering/width rules are one small pure
    function that tests can drive directly, and so `embed` stays a
    readable request/response pair.
    """
    try:
        data: list[dict[str, Any]] = orjson.loads(payload)["data"]
        # Judgment call 1: place by the vendor's own `index`, never by
        # arrival order. A missing/duplicate index raises here rather than
        # producing a silently mis-zipped batch.
        by_index: dict[int, Embedding] = {
            int(item["index"]): tuple(float(value) for value in item["embedding"]) for item in data
        }
    except (orjson.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        # Fail loudly with context (openrouter.py's convention). The body
        # is vendor-generated data, not a secret — but never log `headers`,
        # which carries the bearer token, here or anywhere in this module.
        log.error(
            "openai_embeddings.malformed_response",
            raw_body=payload.decode(errors="replace")[:2000],
            error=str(exc),
        )
        raise

    # Judgment call 2. Checks the index SET, not just the count: a response
    # of the right length whose indices are {0, 0, 2} would pass a bare
    # `len(data) == expected` and then KeyError below with no explanation.
    if by_index.keys() != set(range(expected)):
        raise ValueError(
            f"OpenAI embeddings response is not positionally aligned with its input: "
            f"expected indices 0..{expected - 1}, got {sorted(by_index)}. "
            "EmbeddingProvider's contract is result[i] == embedding of texts[i]; "
            "callers zip these back against their own source rows."
        )

    vectors = [by_index[i] for i in range(expected)]
    # Judgment call 3.
    wrong = {i: len(v) for i, v in enumerate(vectors) if len(v) != EMBEDDING_DIM}
    if wrong:
        raise ValueError(
            f"OpenAI embeddings returned vectors of the wrong width: expected "
            f"{EMBEDDING_DIM} floats per text, got {wrong} (position -> width). "
            "A wrong width usually means openai_embedding_model was pointed at a "
            "different model — whose vectors are not comparable with the stored "
            "corpus at all, not merely a different size."
        )
    return vectors


__all__ = ["OpenAIEmbeddings"]
