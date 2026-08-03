"""VadEndpointer — the endpoint-policy state machine (docs/06 §5).

Consumes 30 ms `AudioFrame` hops, asks the injected `VadModel` for a
speech probability per hop, and is fed the latest live STT partial by the
caller as it arrives. Emits exactly two event kinds:

- `SpeechStart` — activation threshold sustained for `min_speech_ms`.
  During `SPEAKING` this is the future barge-in *commit* trigger
  (docs/06 §6.1); during `LISTENING` it opens an utterance.
- `Endpoint` — the turn is over, either because trailing silence and the
  completeness check agreed (§5.3 row 3) or because the
  `max_endpoint_delay_ms` force cap fired (§5.3 row 4). Carries honest
  timing: `endpoint_ms` is the trailing silence actually waited (§4.1 —
  the future worker records it on the `turn` span).

Pure logic: no I/O, no clock — time advances only via the frames'
`ts_ms` plus their sample-derived duration, so every test scripts a
deterministic timeline. All thresholds come from `Settings`
(vad_threshold, min_speech_ms, endpoint_silence_ms,
max_endpoint_delay_ms), read once at construction — never hardcoded
(canon §5: a demo-day tune is an env edit).

Judgment calls, numbered so review argues with the reasoning:

1. Event types are frozen pydantic models defined HERE, not in
   app/domain/voice.py — they are worker-internal values (they never
   cross a wire), and the contract file is pinned; adding to it is a
   contract change this task does not own. Style mirrors `AudioFrame`.
2. `SpeechStart` fires at min-speech *confirmation* (§6.1's commit
   mark), not on the first speech frame. The §6.1 duck-on-first-frame
   signal is deliberately NOT an event here: the worker that owns
   AudioEgress can act on raw per-frame activity if it needs to; this
   machine's vocabulary is the two policy decisions only.
3. `min_speech_ms` gates utterance *onset* only. Once an utterance is
   active, any single frame at/above threshold resets the
   trailing-silence clock without re-qualifying — §5.1's resumed speech
   ("…pay a vendor") continues the same utterance seamlessly. The cost
   (a lone 30 ms blip extends the turn) is accepted at demo scale.
4. Sustained activation means a *contiguous* run of at/above-threshold
   frames: a sub-threshold frame before confirmation discards the
   candidate run entirely (§6.1's cough → resume case; §5.3 "the 200 ms
   min-speech gate keeps transients from opening turns").
5. Completeness (per §5 and the task spec): finished iff the partial
   ends with terminal punctuation AND its last word is not a
   filler/conjunction/dangling function word. No terminal punctuation
   ⇒ extend; dangling last word ⇒ extend. The dangling set is function
   words only (prepositions, articles, auxiliaries/modals,
   conjunctions) — content verbs are deliberately excluded so a
   legitimate "how much did I send?" is not held to the force cap. The
   heuristic is §5's "honest engineering at demo scale — wrong at the
   margins"; word lists are English-only per §10's demo row.
6. Filler-only suppression is applied at the force cap, inside this
   machine: a forced endpoint whose live partial contains only filler
   tokens (or nothing at all — no partial ever arrived, e.g. sustained
   non-speech noise fooled activation) emits NO event and resets to
   listening. docs/06 §5.2 additionally has ConversationManager discard
   filler-only *finals*; this is the earlier, endpointer-level line of
   defense the §5 table's "drop-if-only-filler" row names. The normal
   (row 3) path cannot produce a filler-only endpoint because fillers
   are non-terminal in the completeness check.
7. All comparisons are inclusive (>=): prob exactly at `vad_threshold`
   counts as speech; silence exactly at `endpoint_silence_ms` /
   `max_endpoint_delay_ms` and speech exactly at `min_speech_ms`
   trigger. Tests pin each boundary.
8. On any turn close — endpoint emitted or filler-suppressed — ALL
   state resets, including the stored partial: stale text from a
   finished utterance must never satisfy the next utterance's
   completeness check. The caller feeds fresh partials per utterance.

Docs: docs/06 §5-§5.3 (policy + decision table), §4.1 (endpoint_ms
honesty), §6.1 (min-speech vs barge-in). Tests:
tests/voice/test_vad_endpointer.py (every §5.3 row is a named test).
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.domain.voice import AudioFrame, SttPartial, VadModel

_FROZEN = ConfigDict(frozen=True)

# Terminal punctuation for the completeness check (§5). The danda "।" is
# included because the product's callers are Hinglish/Hindi speakers
# (docs/01) and Deepgram can emit it — harmless for pure English.
TERMINAL_PUNCTUATION: Final[frozenset[str]] = frozenset({".", "?", "!", "।"})

# Fillers (§5.2's "hmm/uh/um" family). Non-terminal in the completeness
# check AND the tokens the filler-only suppression matches.
FILLER_WORDS: Final[frozenset[str]] = frozenset(
    {"hmm", "hm", "mm", "mhm", "uh", "uhh", "um", "umm", "er", "err", "ah", "aah", "haan"}
)

# A partial *ending* on any of these is mid-thought (§5's "conjunction,
# or a dangling preposition/verb" — e.g. "I want to", "and then",
# "send it to"). Function words only — see judgment call 5.
DANGLING_WORDS: Final[frozenset[str]] = frozenset(
    {
        # conjunctions
        "and", "but", "or", "so", "because", "then", "than", "if", "that",
        "while", "although", "unless", "until",
        # prepositions
        "to", "of", "for", "with", "in", "on", "at", "by", "from", "into",
        "about", "after", "before", "over", "under",
        # articles / determiners
        "the", "a", "an", "my", "your", "this", "these", "those",
        # auxiliaries / modals
        "is", "are", "was", "were", "am", "be", "been", "being",
        "do", "does", "did", "have", "has", "had",
        "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    }
)

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[\w']+")


class SpeechStart(BaseModel):
    """Speech activation confirmed: prob >= threshold sustained for
    `min_speech_ms`. `ts_ms` is the media-clock end of the confirming
    frame; `onset_ts_ms` is the start of the contiguous speech run —
    the §6.1 commit path wants both (onset anchors "how long has the
    caller been talking over Asha")."""

    model_config = _FROZEN

    ts_ms: int
    onset_ts_ms: int


class Endpoint(BaseModel):
    """Turn over. `endpoint_ms` is the trailing silence actually waited
    (frame-end media clock minus silence onset) — honest per §4.1, so a
    completeness hold that extended the wait reports the real number,
    not the configured minimum. `forced` marks the §5.3 row-4 backstop."""

    model_config = _FROZEN

    ts_ms: int
    endpoint_ms: int
    forced: bool = False


VadEvent = SpeechStart | Endpoint


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def looks_complete(text: str) -> bool:
    """§5's terminal-shape heuristic on the live partial: ends with
    terminal punctuation AND does not end on a filler or a dangling
    function word. Empty text is never complete."""
    stripped = text.strip()
    if not stripped or stripped[-1] not in TERMINAL_PUNCTUATION:
        return False
    words = _words(stripped)
    if not words:
        return False
    last = words[-1]
    return last not in FILLER_WORDS and last not in DANGLING_WORDS


def is_filler_only(text: str) -> bool:
    """True when every token is a filler — vacuously true for empty text
    (no partial ever arrived), which the force-cap suppression treats
    the same way: nothing worth a turn (judgment call 6)."""
    return all(w in FILLER_WORDS for w in _words(text))


def _frame_end_ms(frame: AudioFrame) -> int:
    """Media-clock end of the frame. The only time source in this module
    (no wall clock — judgment call in module docstring)."""
    if frame.sample_rate <= 0 or frame.samples <= 0:
        raise ValueError(
            f"AudioFrame with non-positive sample_rate/samples "
            f"({frame.sample_rate=}, {frame.samples=}) — cannot derive a frame duration"
        )
    return frame.ts_ms + round(frame.samples * 1000 / frame.sample_rate)


class VadEndpointer:
    """The §5 endpoint policy machine. Feed every 30 ms uplink hop to
    `on_frame()` (in media-clock order) and every live STT partial to
    `on_partial()` as it arrives; consume the returned events."""

    def __init__(self, vad: VadModel, settings: Settings) -> None:
        self._vad = vad
        self._threshold = settings.vad_threshold
        self._min_speech_ms = settings.min_speech_ms
        self._endpoint_silence_ms = settings.endpoint_silence_ms
        self._max_endpoint_delay_ms = settings.max_endpoint_delay_ms
        # Mutable machine state (the one deliberate exception to the
        # immutability rule: this class IS the state machine; its
        # emitted values are frozen).
        self._speech_onset_ms: int | None = None
        self._utterance_active = False
        self._silence_onset_ms: int | None = None
        self._partial_text = ""

    def on_partial(self, partial: SttPartial) -> None:
        """Record the latest live partial — each one supersedes the
        previous wholesale (docs/06 §1 / domain SttPartial contract)."""
        self._partial_text = partial.text

    def on_frame(self, frame: AudioFrame) -> VadEvent | None:
        """Advance the machine by one hop; returns at most one event
        (activation happens on a speech frame, endpointing on a silence
        frame — the two can never coincide)."""
        end_ms = _frame_end_ms(frame)
        if self._vad.prob(frame.pcm16) >= self._threshold:
            return self._on_speech_frame(frame.ts_ms, end_ms)
        return self._on_silence_frame(frame.ts_ms, end_ms)

    def _on_speech_frame(self, ts_ms: int, end_ms: int) -> SpeechStart | None:
        self._silence_onset_ms = None  # any speech resets the trailing clock (call 3)
        if self._utterance_active:
            return None
        if self._speech_onset_ms is None:
            self._speech_onset_ms = ts_ms
        if end_ms - self._speech_onset_ms >= self._min_speech_ms:
            self._utterance_active = True
            return SpeechStart(ts_ms=end_ms, onset_ts_ms=self._speech_onset_ms)
        return None

    def _on_silence_frame(self, ts_ms: int, end_ms: int) -> Endpoint | None:
        if not self._utterance_active:
            self._speech_onset_ms = None  # candidate run broken — cough case (call 4)
            return None
        if self._silence_onset_ms is None:
            self._silence_onset_ms = ts_ms
        silence_ms = end_ms - self._silence_onset_ms
        if silence_ms >= self._max_endpoint_delay_ms:
            return self._close(end_ms, silence_ms, forced=True)  # §5.3 row 4
        if silence_ms >= self._endpoint_silence_ms and looks_complete(self._partial_text):
            return self._close(end_ms, silence_ms, forced=False)  # §5.3 row 3
        return None  # §5.3 rows 1-2 — keep listening

    def _close(self, end_ms: int, silence_ms: int, *, forced: bool) -> Endpoint | None:
        """Close the utterance: emit the endpoint, or suppress it when
        the force cap fired on a filler-only/empty partial (call 6).
        Either way the machine resets fully (call 8)."""
        suppress = forced and is_filler_only(self._partial_text)
        self._reset()
        if suppress:
            return None
        return Endpoint(ts_ms=end_ms, endpoint_ms=silence_ms, forced=forced)

    def _reset(self) -> None:
        self._speech_onset_ms = None
        self._utterance_active = False
        self._silence_onset_ms = None
        self._partial_text = ""


__all__ = [
    "DANGLING_WORDS",
    "FILLER_WORDS",
    "TERMINAL_PUNCTUATION",
    "Endpoint",
    "SpeechStart",
    "VadEndpointer",
    "VadEvent",
    "is_filler_only",
    "looks_complete",
]
