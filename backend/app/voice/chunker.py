"""Sentence chunker — turns the LLM's incremental text stream into
`Sentence` values the moment each one is complete (docs/06 §4.2), so the
first sentence hits TTS while the rest of the reply is still generating.

Pure synchronous string logic, no I/O — table-driven testable
(tests/voice/test_chunker.py). One instance serves one turn; `sentence_no`
is 0-based within it (the `Sentence` contract, app/domain/voice.py).

Judgment calls, flagged per house style:

1. **`!`, `?` and the danda `।` terminate immediately, even at the buffer
   tail.** Decimals never involve them, so there is nothing to wait for;
   dispatching the instant the terminal token lands is the whole point of
   §4.2. Accepted margin: a cluster split across deltas ("What?" + "!")
   emits early and the stray "!" opens the next sentence.
2. **`.` is decided with lookahead.** A digit or another dot after it, or
   an ellipsis tail, is non-terminal; a digit *before* it with no
   lookahead available yet ("₹1." at the buffer tail) HOLDS until the
   next delta or flush resolves the ambiguity — this is what keeps
   "₹1.5 lakh" whole. A period after an ordinary word terminates even at
   the buffer tail (accepted margin: "example." + "com" mis-splits;
   URLs are not voice-reply material).
3. **Ellipsis ("...") is non-terminal** — in speech it is a thinking
   pause, not a sentence boundary; overflow or flush recovers the text.
4. **`MAX_SENTENCE_CHARS = 160`** (~10 s of speech at conversational TTS
   pace): when the *incomplete* buffer exceeds it, break at the rightmost
   clause boundary (`,;:—–` — docs/06 §4.2's "safe clause break") inside
   the window, else the rightmost whitespace, else hard-cut. A sentence
   whose terminal punctuation is already in the buffer is emitted whole
   even when longer — overflow bounds *waiting*, not sentence length.
5. **Abbreviation handling is conservative**: a small Indian-English
   flavored set (`Rs.`, `Shri`, `e.g.`, ...) plus any single alphabetic
   letter before the dot (initials — "A. Kumar") suppress the boundary.
   Wrong at the margins by design (docs/06 §5 makes the same
   heuristic-over-model call for the completeness hold).
6. **Trailing quotes/brackets and stacked terminals attach to the
   sentence they close** ("theek?!", 'bola "haan."') rather than opening
   the next one.
"""

from __future__ import annotations

from typing import Final

from app.domain.voice import Sentence

# Judgment call 1: terminate on sight, no lookahead needed.
IMMEDIATE_TERMINALS: Final = frozenset({"!", "?", "।"})

# Judgment call 4: the overflow window and its break preferences.
MAX_SENTENCE_CHARS: Final = 160
CLAUSE_BREAK_CHARS: Final = (",", ";", ":", "—", "–")

# Judgment call 6: absorbed into the sentence when directly after its
# terminal punctuation.
TRAILING_ATTACH_CHARS: Final = frozenset({'"', "'", "”", "’", ")", "!", "?", "।", "."})

# Judgment call 5: lowercase, dot-stripped tokens that end in "." without
# ending a sentence.
ABBREVIATIONS: Final = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "shri", "smt", "col",
        "rs", "re", "no", "vs", "etc", "approx", "dept", "govt",
        "ltd", "pvt", "st", "jr", "sr", "e.g", "i.e", "a.m", "p.m",
    }
)


class SentenceChunker:
    """Feed append-only text deltas in; complete `Sentence`s come out.
    `flush()` at end-of-stream emits the remainder."""

    def __init__(self) -> None:
        self._buf = ""
        self._next_sentence_no = 0

    def feed(self, delta: str) -> tuple[Sentence, ...]:
        """Append one streamed delta and return every sentence it
        completed (possibly several — a single delta can carry a whole
        multi-sentence reply)."""
        if not delta:
            return ()
        self._buf += delta
        return self._drain()

    def flush(self) -> tuple[Sentence, ...]:
        """End-of-stream: emit any drained sentences plus the remainder
        (a reply legitimately ends without terminal punctuation)."""
        out = list(self._drain())
        remainder = self._buf.strip()
        self._buf = ""
        if remainder:
            out.append(self._make(remainder))
        return tuple(out)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _drain(self) -> tuple[Sentence, ...]:
        out: list[Sentence] = []
        while True:
            cut = self._find_terminal_cut()
            if cut is None:
                if len(self._buf) <= MAX_SENTENCE_CHARS:
                    break
                cut = self._find_overflow_cut()
            piece = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if piece:
                out.append(self._make(piece))
        return tuple(out)

    def _make(self, text: str) -> Sentence:
        sentence = Sentence(text=text, sentence_no=self._next_sentence_no)
        self._next_sentence_no += 1
        return sentence

    def _find_terminal_cut(self) -> int | None:
        """Index just past the first sentence boundary (terminal punct
        plus any attached trailing chars), or None if the buffer holds no
        complete sentence yet."""
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch in IMMEDIATE_TERMINALS:
                return self._extend_cut(i + 1)
            if ch == "." and _is_terminal_period(buf, i):
                return self._extend_cut(i + 1)
        return None

    def _extend_cut(self, cut: int) -> int:
        buf = self._buf
        while cut < len(buf) and buf[cut] in TRAILING_ATTACH_CHARS:
            cut += 1
        return cut

    def _find_overflow_cut(self) -> int:
        """The incomplete buffer overflowed `MAX_SENTENCE_CHARS`: break at
        the rightmost clause punct in the window, else the rightmost
        whitespace, else hard-cut (judgment call 4)."""
        window = self._buf[:MAX_SENTENCE_CHARS]
        clause_at = max(window.rfind(c) for c in CLAUSE_BREAK_CHARS)
        if clause_at > 0:
            return clause_at + 1
        whitespace_at = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
        if whitespace_at > 0:
            return whitespace_at
        return MAX_SENTENCE_CHARS


def _is_terminal_period(buf: str, i: int) -> bool:
    """Judgment calls 2/3/5: is the '.' at `buf[i]` a sentence boundary?"""
    nxt = buf[i + 1] if i + 1 < len(buf) else None
    prev = buf[i - 1] if i > 0 else None
    if nxt is not None and (nxt.isdigit() or nxt == "."):
        return False  # decimal ("1.5"), ref ("No.5"), or ellipsis start
    if prev == ".":
        return False  # tail dot of an ellipsis
    if nxt is None and prev is not None and prev.isdigit():
        return False  # "₹1." at the buffer tail — hold for the next delta
    token = _token_before(buf, i)
    if len(token) == 1 and token.isalpha():
        return False  # an initial — "A. Kumar"
    if token.lower() in ABBREVIATIONS:
        return False
    return True


def _token_before(buf: str, i: int) -> str:
    """The alphabetic/dotted token immediately before position `i`
    ("Rs", "e.g"), dot-stripped for the abbreviation lookup."""
    j = i
    while j > 0 and (buf[j - 1].isalpha() or buf[j - 1] == "."):
        j -= 1
    return buf[j:i].strip(".")


__all__ = [
    "ABBREVIATIONS",
    "CLAUSE_BREAK_CHARS",
    "IMMEDIATE_TERMINALS",
    "MAX_SENTENCE_CHARS",
    "SentenceChunker",
]
