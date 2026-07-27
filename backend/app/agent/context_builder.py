"""`ContextBuilder` — assembles the per-turn `ContextBundle`
(docs/05-agent-architecture.md §3.3; slot semantics owned by
docs/11-prompt-engineering.md §1, assembly by docs/08-context-and-events.md §5).

Phase-2 scope (plan decision #1): only persona, business_rules,
user_profile, conversation, and current_utterance are populated. Slots
4–7 (screen_context / recent_actions / memory_summary / knowledge) stay
at their `""` defaults on the frozen `ContextBundle` and are still
rendered as empty tag pairs by `PromptBuilder` — Phase 4/5 only start
*populating* them, never restructuring the template.

Judgment calls made in this module, flagged per house style:

1. **Prompt files are read once at construction, not per call.** The
   prefix cache keys on byte-identical slot-1/2 bytes across every call
   (docs/11 §1.1); loading once guarantees that stability for the
   process lifetime and keeps file I/O off the 15/40 ms `context.build`
   hot path (docs/08 §5). A missing/unreadable prompt file therefore
   fails *construction* — loudly, at startup — because docs/05 §3.3's
   "degrade context, never the turn" policy is about per-turn data
   sources (DB, Redis); a missing deploy artifact is a broken deploy,
   not a degradable turn.
2. **The user_profile line carries no personal name.** docs/11 §4's
   example opens with "Rajesh Kumar." but `merchants` (docs/12 §3.1,
   app/models/orm.py) has no personal-name column — only business_name,
   city, account_type, preferred_language, merchant_since, kyc_status.
   The rendered line therefore starts at the "Business:" segment and
   otherwise follows the doc's format exactly ("Merchant since <year>"
   uses `merchant_since.year`). The personal name presumably arrives
   with the Phase-5 `user_profiles` table (docs/12's ER diagram);
   nothing in Phase 2 can supply it honestly.
3. **A missing merchant row (or a DB failure) degrades the slot, not
   the turn** (docs/05 §3.3). Both produce the same deterministic
   `_PROFILE_UNAVAILABLE` line, which stays byte-stable within the call
   (preserving slot 3's within-a-call cache role, docs/11 §1.1) and
   steers the model toward tools instead of invented facts (docs/10 §1
   invariant 1). The failure is logged, never swallowed silently.
4. **A Redis/deserialization failure degrades the window to empty**
   for the same reason: the never-drop set is system, business rules,
   current utterance, pending-confirm (docs/08 §5.2) — the conversation
   window is explicitly shrinkable, so an empty window is a degraded
   but correct turn, while an exception here would kill it.

The merchant row is re-read each turn, yet slot 3 must be byte-stable
within a call (docs/11 §1.1): that holds because merchants are not
self-service-editable (MerchantRepo's own docstring) — the row cannot
change mid-call in Phase 2.
"""

from __future__ import annotations

from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data.repositories.merchant_repo import MerchantRepo
from app.domain.types import ContextBundle, Message, Session
from app.memory.session_memory import SessionMemory
from app.models import Merchant
from app.obs.logging import get_logger

log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Deterministic fallback for judgment call #3 — phrased to keep the model
# inside docs/10 §1 invariant 1 (account facts come from tools, never memory).
_PROFILE_UNAVAILABLE = (
    "Profile unavailable for this caller. Do not state or guess any account "
    "details; anything account-specific must come from a tool call."
)


def _load_prompt(filename: str) -> str:
    """Read one prompts/*.md file. Explicit UTF-8: the files carry ₹ and
    en-dashes, and Windows' locale default (cp1252) would corrupt them —
    which, per docs/11 §1.1, would also silently change the cached-prefix
    bytes between differently-configured hosts."""
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def _format_profile(merchant: Merchant) -> str:
    """docs/11 §4's compact one-line format, minus the personal name the
    schema cannot supply (judgment call #2). NO balances or statuses ever
    — docs/11 §1: those are tool-only."""
    return (
        f"Business: {merchant.business_name}, {merchant.city}. "
        f"Merchant since {merchant.merchant_since.year}. "
        f"Account type: {merchant.account_type}. "
        f"Preferred language: {merchant.preferred_language}."
    )


class ContextBuilder:
    """Implements `ContextBuilderProto` (app/domain/interfaces.py).

    Dependencies are injected: the app-lifespan `async_sessionmaker`
    (docs/04 §4 — this module never opens its own engine) for the
    per-turn MerchantRepo read, and the shared `SessionMemory` for the
    transcript window.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        session_memory: SessionMemory,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._memory = session_memory
        # Judgment call #1: loaded once, byte-stable for the process lifetime.
        self._persona = _load_prompt("persona.md")
        self._business_rules = _load_prompt("business_rules.md")

    async def build(self, session: Session, *, current_utterance: str) -> ContextBundle:
        user_profile = await self._build_user_profile(session.user_id)
        conversation = await self._get_window(session.session_id)
        return ContextBundle(
            persona=self._persona,
            business_rules=self._business_rules,
            user_profile=user_profile,
            # slots 4-7 stay at their "" defaults (Phase 4/5 scope)
            conversation=tuple(conversation),
            current_utterance=current_utterance,
        )

    async def _build_user_profile(self, merchant_id: str) -> str:
        try:
            async with self._sessionmaker() as db:
                merchant = await MerchantRepo(db).get(merchant_id)
        except SQLAlchemyError:
            # Judgment call #3: degrade the slot, never the turn (docs/05 §3.3).
            log.warning("context.user_profile.db_error", merchant_id=merchant_id, exc_info=True)
            return _PROFILE_UNAVAILABLE
        if merchant is None:
            log.warning("context.user_profile.merchant_missing", merchant_id=merchant_id)
            return _PROFILE_UNAVAILABLE
        return _format_profile(merchant)

    async def _get_window(self, session_id: str) -> list[Message]:
        try:
            return await self._memory.get_window(session_id)
        except (RedisError, ValueError):
            # Judgment call #4. ValueError covers both orjson decode errors
            # (RedisClient.get_transcript_window) and pydantic's
            # ValidationError (Message.model_validate) — both subclass it.
            log.warning("context.conversation.window_error", session_id=session_id, exc_info=True)
            return []


__all__ = ["ContextBuilder"]
