"""Barge-in transcript truncation — the docs/06 §6.3 played-ms →
character-offset mapping, as pure functions so the math is table-testable
with no worker, egress, or provider in the loop.

`VoiceAgentWorker` accumulates one `SentenceAudio` record per sentence it
dispatches to TTS (text, character-timing alignment, and how much genuine
audio the sentence contributed to the egress queue). On a committed
barge-in it asks `truncate_reply()` what was actually voiced before the
flush, using `AudioEgress`'s `played_ms` counter delta for the turn.

Judgment calls, flagged per house style:

1. **Milliseconds live in one domain: audio duration.** `TtsChunk.pcm` is
   24 kHz mono s16 (48 bytes/ms — the contract in app/domain/voice.py),
   and `AudioEgress.played_ms` counts genuine audio at the wire rate.
   Resampling preserves duration, so the two clocks agree to within the
   sub-millisecond rounding audio_egress.py already documents; this
   module treats them as one clock and never converts.
2. **A char whose alignment start-ms has been reached counts as voiced,
   and the word containing it is completed forward.** ElevenLabs
   alignment gives each char's *start* time; a char that began playing
   was (at least partly) heard, and docs/06 §6.6's worked example snaps
   *forward* past "rupees," rather than dropping the half-heard word.
   Completing the word (up to the next whitespace, trailing punctuation
   attached) is that same snap: never mid-word, biased toward what the
   caller actually heard.
3. **A sentence with no alignment degrades to proportional estimation.**
   The provider contract allows pure-audio chunks with no alignment
   frame (`TtsChunk.alignment` empty — e.g. a fallback-voice retry path
   or a fake in tests); mapping played-ms proportionally over the
   sentence's characters is honest-at-demo-scale degradation, and it is
   logged at the call site, never silent. A zero-duration sentence (no
   audio ever enqueued) contributes nothing.
4. **Fully played sentences are included verbatim, in order**, joined
   with single spaces — the same joining the worker uses for the wire
   captions, so the truncated `transcript.final` is a strict prefix of
   what the full caption would have been (modulo the partial sentence's
   cut).

Docs: docs/06 §6.3 (mechanism), §6.6 (worked example — reproduced in
tests/voice/test_truncation.py). Tests: tests/voice/test_truncation.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

# Judgment call 1: 24 kHz mono s16 == 48 bytes per millisecond
# (app/domain/voice.py's TtsChunk contract; docs/06 §3.2).
TTS_PCM_BYTES_PER_MS: Final = 48


@dataclass
class SentenceAudio:
    """Per-sentence working record the worker builds WHILE synthesizing —
    mutable internal state (the same deliberate exception audio_egress.py's
    `_Segment` documents; everything this module *returns* is a plain str).

    `alignment` is sentence-relative `(char_offset, start_ms)` pairs as
    the provider rebases them (elevenlabs.py judgment call 4);
    `audio_ms` is the genuine audio duration this sentence enqueued."""

    sentence_no: int
    text: str
    alignment: list[tuple[int, int]] = field(default_factory=list)
    audio_ms: int = 0


def truncate_reply(sentences: Sequence[SentenceAudio], played_ms: int) -> str:
    """The §6.3 mapping: given the per-sentence audio records for one turn
    and how many milliseconds of the turn's audio actually played before
    the barge-in flush, return the text that was actually voiced."""
    if played_ms <= 0:
        return ""
    voiced: list[str] = []
    elapsed = 0
    for sentence in sentences:
        if sentence.audio_ms <= 0:
            continue  # judgment call 3: nothing enqueued, nothing voiced
        if played_ms >= elapsed + sentence.audio_ms:
            voiced.append(sentence.text)
            elapsed += sentence.audio_ms
            continue
        partial = _truncate_sentence(sentence, played_ms - elapsed)
        if partial:
            voiced.append(partial)
        break
    return " ".join(voiced)


def _truncate_sentence(sentence: SentenceAudio, in_sentence_ms: int) -> str:
    """Map a mid-sentence play position to the voiced prefix of that
    sentence's text (alignment when present, proportional otherwise)."""
    if in_sentence_ms <= 0:
        return ""
    if sentence.alignment:
        char_offset = _alignment_offset(sentence.alignment, in_sentence_ms)
    else:
        # Judgment call 3: proportional fallback, floor-rounded so an
        # instant of audio never claims a character that hadn't started.
        char_offset = len(sentence.text) * in_sentence_ms // sentence.audio_ms
    if char_offset <= 0:
        return ""
    return _complete_word(sentence.text, char_offset)


def _alignment_offset(alignment: Sequence[tuple[int, int]], in_sentence_ms: int) -> int:
    """Chars voiced = 1 + the largest char_offset whose start-ms has been
    reached (judgment call 2); 0 when playback stopped before the first
    aligned char began."""
    voiced = 0
    for char_offset, start_ms in alignment:
        if start_ms <= in_sentence_ms:
            voiced = max(voiced, char_offset + 1)
    return voiced


def _complete_word(text: str, char_count: int) -> str:
    """Snap the raw char count forward to the end of the word it lands in
    (judgment call 2), then strip stray whitespace."""
    if char_count >= len(text):
        return text
    end = char_count
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[:end].rstrip()


__all__ = ["TTS_PCM_BYTES_PER_MS", "SentenceAudio", "truncate_reply"]
