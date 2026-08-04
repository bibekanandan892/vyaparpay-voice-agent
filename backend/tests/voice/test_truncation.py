"""Table tests for app/voice/truncation.py — the docs/06 §6.3 played-ms →
character-offset mapping, including the §6.6 worked-example shape: cut
after a fully-voiced word, mid-word snaps complete the word, later
sentences drop entirely. Pure functions, no worker in the loop."""

from __future__ import annotations

from app.voice.truncation import SentenceAudio, truncate_reply


def _sentence(
    text: str,
    *,
    sentence_no: int = 0,
    audio_ms: int | None = None,
    alignment: list[tuple[int, int]] | None = None,
) -> SentenceAudio:
    return SentenceAudio(
        sentence_no=sentence_no,
        text=text,
        alignment=alignment if alignment is not None else [],
        audio_ms=audio_ms if audio_ms is not None else 10 * len(text),
    )


# Word-start alignment for "Alpha beta gamma delta.": Alpha@0, beta@300,
# gamma@600, delta@900; 1200 ms of audio.
_WORDS = [(0, 0), (6, 300), (11, 600), (17, 900)]


def test_zero_played_is_empty() -> None:
    s = _sentence("Alpha beta gamma delta.", alignment=_WORDS, audio_ms=1200)
    assert truncate_reply([s], 0) == ""


def test_cut_mid_sentence_snaps_word_forward() -> None:
    # 650 ms: "gamma" (char 11 @ 600 ms) began playing -> the word is
    # completed forward (judgment call 2), "delta" (900 ms) never started.
    s = _sentence("Alpha beta gamma delta.", alignment=_WORDS, audio_ms=1200)
    assert truncate_reply([s], 650) == "Alpha beta gamma"


def test_cut_before_first_aligned_char_is_empty() -> None:
    s = _sentence("Alpha beta gamma delta.", alignment=[(0, 100)], audio_ms=1200)
    assert truncate_reply([s], 50) == ""


def test_fully_played_sentence_is_verbatim() -> None:
    s = _sentence("Alpha beta gamma delta.", alignment=_WORDS, audio_ms=1200)
    assert truncate_reply([s], 1200) == "Alpha beta gamma delta."


def test_multi_sentence_cut_lands_in_second() -> None:
    s0 = _sentence("First one.", sentence_no=0, alignment=[(0, 0), (6, 400)], audio_ms=800)
    s1 = _sentence(
        "Second reply sentence.",
        sentence_no=1,
        alignment=[(0, 0), (7, 300), (13, 600)],
        audio_ms=900,
    )
    # 800 (all of s0) + 350 into s1: "reply" (char 7 @ 300) began.
    assert truncate_reply([s0, s1], 1150) == "First one. Second reply"


def test_later_sentences_drop_entirely() -> None:
    s0 = _sentence("First one.", sentence_no=0, alignment=[(0, 0)], audio_ms=800)
    s1 = _sentence("Never spoken.", sentence_no=1, alignment=[(0, 0)], audio_ms=500)
    assert truncate_reply([s0, s1], 800) == "First one."


def test_no_alignment_falls_back_to_proportional() -> None:
    # 20 chars over 1000 ms, 500 ms played -> 10 chars -> inside "beta"
    # ("Alpha beta"[..10]) -> completed forward.
    s = _sentence("Alpha beta and more.", alignment=None, audio_ms=1000)
    assert truncate_reply([s], 500) == "Alpha beta"


def test_zero_duration_sentence_contributes_nothing() -> None:
    s0 = _sentence("Skipped.", audio_ms=0)
    s1 = _sentence("Voiced fully.", sentence_no=1, alignment=[(0, 0)], audio_ms=400)
    assert truncate_reply([s0, s1], 400) == "Voiced fully."


def test_worked_example_shape_from_docs_6_6() -> None:
    # The §6.6 canonical shape: alignment maps 1180 ms to a char offset
    # that snaps to the word boundary after "rupees," — the stored turn
    # states the limit but never the used figure.
    text = "Your daily limit is twenty-five thousand rupees, and you've already used a lot."
    # Char starts, 25 ms per char (48 chars voiced = 1200 ms > 1180).
    alignment = [(i, 25 * i) for i in range(len(text))]
    s = _sentence(text, alignment=alignment, audio_ms=25 * len(text))
    result = truncate_reply([s], 1180)
    # 1180 // 25 = 47 -> char 47 (the comma after "rupees") voiced.
    assert result == "Your daily limit is twenty-five thousand rupees,"
    assert "used" not in result
