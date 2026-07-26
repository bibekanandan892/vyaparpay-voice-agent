"""Provider implementations — the outbound HTTP boundary to third-party
services (OpenRouter today; Deepgram/ElevenLabs/OpenAI-embeddings join in
later phases, docs/04-backend-architecture.md §4). Downstream code depends
on the `Protocol` in `app.domain.interfaces`, never on a concrete class
directly — the app lifespan (app/main.py, a later batch) is the one place
that imports from here to build the process-singleton and hand it out
protocol-typed.
"""

from __future__ import annotations

from app.providers.openrouter import OpenRouterLLM

__all__ = ["OpenRouterLLM"]
