"""Table-driven tests for the sentence chunker (docs/06 §4.2) — pure
sync string logic, no I/O, no clock.

The table covers: simple sentences, multi-sentence single deltas,
decimals ("₹1.5 lakh" must not split — including the dot landing at a
delta boundary), the Hindi danda, abbreviations/initials, ellipsis,
token-by-token streaming, end-of-stream flush, and overflow clause
breaking.
"""

from __future__ import annotations

import pytest

from app.voice.chunker import MAX_SENTENCE_CHARS, SentenceChunker

# One overflow unit: 19 chars, clause comma at index 17 — no terminal
# punctuation anywhere, so only the overflow path can emit.
_OVERFLOW_UNIT = "paanch sau rupaye, "

CASES: list[tuple[str, list[str], list[str], list[str]]] = [
    # (case id, feed deltas, expected texts from feeds, expected texts from flush)
    (
        "simple_sentence",
        ["Namaste, main Asha bol rahi hoon."],
        ["Namaste, main Asha bol rahi hoon."],
        [],
    ),
    (
        "multi_sentence_single_delta",
        ["Kya haal hai? Sab theek. Chalo!"],
        ["Kya haal hai?", "Sab theek.", "Chalo!"],
        [],
    ),
    (
        "decimal_not_split",
        ["Aapka balance ₹1.5 lakh hai. Theek?"],
        ["Aapka balance ₹1.5 lakh hai.", "Theek?"],
        [],
    ),
    (
        "decimal_dot_at_delta_boundary_held",
        ["Limit ₹1.", "5 lakh hai."],
        ["Limit ₹1.5 lakh hai."],
        [],
    ),
    (
        "trailing_number_dot_recovered_by_flush",
        ["Total ₹500."],
        [],
        ["Total ₹500."],
    ),
    (
        "danda_terminates",
        ["आपका बैलेंस पाँच सौ रुपये है। और कुछ?"],
        ["आपका बैलेंस पाँच सौ रुपये है।", "और कुछ?"],
        [],
    ),
    (
        "abbreviation_mr_held",
        ["Mr. Kumar ka payment ho gaya."],
        ["Mr. Kumar ka payment ho gaya."],
        [],
    ),
    (
        "abbreviation_rs_held",
        ["Rs. 500 bhej diye. Aur?"],
        ["Rs. 500 bhej diye.", "Aur?"],
        [],
    ),
    (
        "single_letter_initial_held",
        ["A. Kumar aaye the."],
        ["A. Kumar aaye the."],
        [],
    ),
    (
        "numbered_reference_not_split",
        ["Shop No.5 par aayiye."],
        ["Shop No.5 par aayiye."],
        [],
    ),
    (
        "ellipsis_is_not_terminal",
        ["Hmm... theek hai."],
        ["Hmm... theek hai."],
        [],
    ),
    (
        "token_by_token_stream",
        ["Main ", "dekh ", "rahi ", "hoon", ". ", "Ek ", "minute"],
        ["Main dekh rahi hoon."],
        ["Ek minute"],
    ),
    (
        "flush_emits_unterminated_remainder",
        ["adhoora vaakya bina viram"],
        [],
        ["adhoora vaakya bina viram"],
    ),
    (
        "empty_deltas_are_noops",
        ["", "Chalo!", ""],
        ["Chalo!"],
        [],
    ),
    (
        "flush_on_fresh_chunker_is_empty",
        [],
        [],
        [],
    ),
]


@pytest.mark.parametrize(
    ("deltas", "expected_fed", "expected_flushed"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_chunker_table(
    deltas: list[str], expected_fed: list[str], expected_flushed: list[str]
) -> None:
    chunker = SentenceChunker()

    fed: list[str] = []
    for delta in deltas:
        fed.extend(s.text for s in chunker.feed(delta))
    flushed = [s.text for s in chunker.flush()]

    assert fed == expected_fed
    assert flushed == expected_flushed


def test_sentence_numbers_are_zero_based_and_sequential_across_feeds_and_flush() -> None:
    chunker = SentenceChunker()

    sentences = list(chunker.feed("Ek. Do. "))
    sentences += chunker.feed("Teen? Chaar")
    sentences += chunker.flush()

    assert [s.text for s in sentences] == ["Ek.", "Do.", "Teen?", "Chaar"]
    assert [s.sentence_no for s in sentences] == [0, 1, 2, 3]


def test_overflow_breaks_at_rightmost_clause_boundary() -> None:
    # 12 units = 228 chars, commas only — no terminal punctuation, so only
    # the overflow path can emit. Rightmost comma inside the 160-char
    # window sits at index 150 (unit 8's comma).
    text = _OVERFLOW_UNIT * 12
    chunker = SentenceChunker()

    fed = list(chunker.feed(text))
    flushed = list(chunker.flush())

    assert [s.text for s in fed] == [(_OVERFLOW_UNIT * 8).strip()]
    assert [s.text for s in flushed] == [(_OVERFLOW_UNIT * 4).strip()]
    assert all(len(s.text) <= MAX_SENTENCE_CHARS for s in fed)
    # Nothing lost, nothing duplicated: pieces reassemble the original.
    reassembled = " ".join(s.text for s in fed + flushed)
    assert reassembled == text.strip()


def test_overflow_falls_back_to_whitespace_when_no_clause_punct() -> None:
    words = ("shabd " * 40).strip()  # 239 chars, spaces only
    chunker = SentenceChunker()

    fed = list(chunker.feed(words))
    flushed = list(chunker.flush())

    assert fed, "overflow should have emitted at a whitespace break"
    assert all(len(s.text) <= MAX_SENTENCE_CHARS for s in fed)
    assert " ".join(s.text for s in fed + flushed) == words


def test_overflow_hard_cuts_pathological_unbroken_run() -> None:
    run = "क" * (MAX_SENTENCE_CHARS + 40)
    chunker = SentenceChunker()

    fed = list(chunker.feed(run))
    flushed = list(chunker.flush())

    assert [s.text for s in fed] == ["क" * MAX_SENTENCE_CHARS]
    assert [s.text for s in flushed] == ["क" * 40]


def test_complete_sentence_longer_than_overflow_window_emits_whole() -> None:
    # Overflow bounds *waiting*, not sentence length (module judgment
    # call 4): terminal punctuation already in the buffer wins.
    long_sentence = "shabd " * 30 + "antim."  # > 160 chars, terminated
    chunker = SentenceChunker()

    fed = list(chunker.feed(long_sentence))

    assert [s.text for s in fed] == [long_sentence.strip()]


def test_trailing_quote_attaches_to_its_sentence() -> None:
    chunker = SentenceChunker()

    fed = list(chunker.feed('Usne bola "haan." Phir chala gaya.'))

    assert [s.text for s in fed] == ['Usne bola "haan."', "Phir chala gaya."]


def test_stacked_terminals_attach() -> None:
    chunker = SentenceChunker()

    fed = list(chunker.feed("Sach mein?! Haan."))

    assert [s.text for s in fed] == ["Sach mein?!", "Haan."]
