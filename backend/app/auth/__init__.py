"""Credential minting for the voice call path (docs/13-api-contracts.md
§6.2 signaling token, §7 TURN REST credentials).

Deliberately separate from `app/api/` and `app/agent/`: both modules here
mint or verify *bearer* material, and both are consumed by two different
processes — agent-api mints (`app/api/routes/sessions.py`), the
voice-worker verifies (`SignalingServer`, a later task). Keeping them in
one leaf package with no FastAPI/aiortc imports is what lets the worker
depend on them without dragging the REST stack in (docs/13 §5's
"one image, two entrypoints" seam).

Nothing in this package ever logs a token, a credential, or `TURN_SECRET`.
"""

from __future__ import annotations

__all__: list[str] = []
