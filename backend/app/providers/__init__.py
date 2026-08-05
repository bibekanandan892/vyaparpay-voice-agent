"""Provider implementations — the outbound HTTP/WS boundary to third-party
services (OpenRouter, Deepgram, ElevenLabs, OpenAI embeddings —
docs/04-backend-architecture.md §4). Downstream code depends
on the `Protocol` in `app.domain.interfaces` / `app.domain.voice`, never on
a concrete class directly — the app lifespan (app/main.py, a later batch)
is the one place that imports from here to build the process-singleton and
hand it out protocol-typed.

The voice providers (`app.providers.deepgram.DeepgramStt`,
`app.providers.elevenlabs.ElevenLabsTts`) are deliberately NOT re-exported
here: they import `websockets` from the [voice] extra, and re-exporting
them would make this package's import — which agent-api's Phase-2 path
performs — require the voice extra everywhere (pyproject.toml: "agent-api
and the test suite never import these"). The voice worker imports those
modules directly.
"""

from __future__ import annotations

from app.providers.openai_embeddings import OpenAIEmbeddings
from app.providers.openrouter import OpenRouterLLM

__all__ = ["OpenAIEmbeddings", "OpenRouterLLM"]
