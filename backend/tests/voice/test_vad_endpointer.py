"""Unit tests for app.voice.vad_endpointer — the docs/06 §5 endpoint
policy, driven end to end through scripted `FakeVad` probability
sequences and scripted STT partials; no onnxruntime, no clock, no I/O.

§5.3 decision-table coverage, row by row (the doc's table order):

| Row | Sil>=250 | Complete? | Sil>=2000 | Test                                               |
|-----|----------|-----------|-----------|----------------------------------------------------|
| 1   | No       | —         | No        | test_row_1_short_silence_keeps_listening           |
| 2   | Yes      | No        | No        | test_row_2_incomplete_partial_holds_endpoint_open  |
| 3   | Yes      | Yes       | —         | test_row_3_silence_plus_complete_partial_endpoints |
| 4   | —        | —         | Yes       | test_row_4_force_endpoint_at_max_delay_...         |

Plus §5.1 (mid-utterance pause hold), §5.2 (filler non-terminality and
filler-only suppression), min-speech/endpoint/force-cap exactly-at-
threshold boundaries, endpoint_ms honesty, and full state reset between
utterances.

Timeline convention: 32 ms frames (512 samples @ 16 kHz — Silero's
native streaming hop, docs/06 §5), media clock starting at 0 unless a
test says otherwise. With the default Settings (min_speech_ms=200)
activation lands on the 7th consecutive speech frame (ends at 224 ms —
the first frame end >= 200).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.domain.voice import AudioFrame, SttPartial
from app.voice.vad_endpointer import (
    Endpoint,
    SpeechStart,
    VadEndpointer,
    VadEvent,
    is_filler_only,
    looks_complete,
)
from tests.conftest import SettingsFactory
from tests.fakes import FakeVad

FRAME_SAMPLES = 512  # Silero's native 32 ms hop @ 16 kHz (docs/06 §5)
SAMPLE_RATE_HZ = 16_000
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE_HZ  # = 32, derived not hardcoded
FRAME_PCM = bytes(FRAME_SAMPLES * 2)  # content is irrelevant — FakeVad is scripted

SPEECH = 0.9  # comfortably above the 0.5 default threshold
SILENCE = 0.1  # comfortably below

# Doc-example partials (§5.1's canonical broken/complete pair).
INCOMPLETE = "I want to"
COMPLETE = "I want to pay a vendor."


def _frame(ts_ms: int) -> AudioFrame:
    return AudioFrame(
        pcm16=FRAME_PCM, sample_rate=SAMPLE_RATE_HZ, samples=FRAME_SAMPLES, ts_ms=ts_ms
    )


@dataclass
class Driver:
    """Keeps the media clock so tests read as timelines: `frames(SPEECH, 7)`
    scripts 7 probabilities, feeds 7 consecutive 32 ms frames, and returns
    whatever events fired."""

    endpointer: VadEndpointer
    vad: FakeVad
    ts_ms: int = 0
    events: list[VadEvent] = field(default_factory=list)

    def frames(self, prob: float, n: int) -> list[VadEvent]:
        fired: list[VadEvent] = []
        for _ in range(n):
            self.vad.script(prob)
            event = self.endpointer.on_frame(_frame(self.ts_ms))
            self.ts_ms += FRAME_MS
            if event is not None:
                fired.append(event)
        self.events.extend(fired)
        return fired

    def partial(self, text: str) -> None:
        self.endpointer.on_partial(SttPartial(text=text, ts_ms=self.ts_ms))


@pytest.fixture
def make_driver(settings_factory: SettingsFactory):
    def _make(**overrides: object) -> Driver:
        vad = FakeVad()
        return Driver(VadEndpointer(vad, settings_factory(**overrides)), vad)

    return _make


def _activate(d: Driver) -> SpeechStart:
    """7 consecutive speech frames — the default-settings activation run."""
    fired = d.frames(SPEECH, 7)
    assert len(fired) == 1 and isinstance(fired[0], SpeechStart)
    return fired[0]


# --------------------------------------------------------------------------
# Activation / min-speech (§5's 200 ms gate, §6.1's cough rejection)
# --------------------------------------------------------------------------


def test_speech_start_fires_when_min_speech_sustained(make_driver) -> None:
    d = make_driver()
    assert d.frames(SPEECH, 6) == []  # 6 frames end at 192 ms < 200
    assert d.frames(SPEECH, 1) == [SpeechStart(ts_ms=224, onset_ts_ms=0)]


def test_speech_start_fires_once_per_utterance(make_driver) -> None:
    d = make_driver()
    _activate(d)
    assert d.frames(SPEECH, 20) == []  # continued speech re-fires nothing


def test_min_speech_exactly_at_threshold_fires(make_driver) -> None:
    # 6 frames span exactly 192 ms — inclusive comparison (>=, not >).
    d = make_driver(min_speech_ms=192)
    assert d.frames(SPEECH, 5) == []
    assert d.frames(SPEECH, 1) == [SpeechStart(ts_ms=192, onset_ts_ms=0)]


def test_prob_exactly_at_vad_threshold_counts_as_speech(make_driver) -> None:
    d = make_driver()  # default vad_threshold = 0.5
    fired = d.frames(0.5, 7)
    assert fired == [SpeechStart(ts_ms=224, onset_ts_ms=0)]


def test_sub_min_speech_transient_never_opens_a_turn(make_driver) -> None:
    """A 128 ms cough (§6.1) neither opens an utterance nor ever endpoints,
    and the broken candidate run does not leak into the next activation."""
    d = make_driver()
    assert d.frames(SPEECH, 4) == []  # 128 ms < 200 — no activation
    assert d.frames(SILENCE, 20) == []  # no utterance was open — no endpoint either
    # Fresh, contiguous speech activates with the NEW onset, not the cough's.
    fired = d.frames(SPEECH, 7)
    assert fired == [SpeechStart(ts_ms=992, onset_ts_ms=768)]


# --------------------------------------------------------------------------
# §5.3 decision table, row by row
# --------------------------------------------------------------------------


def test_row_1_short_silence_keeps_listening(make_driver) -> None:
    """Row 1: trailing silence below 250 ms — keep listening even though
    the partial already looks complete (the silence signal gates first)."""
    d = make_driver()
    _activate(d)
    d.partial(COMPLETE)
    assert d.frames(SILENCE, 7) == []  # 224 ms < 250


def test_row_2_incomplete_partial_holds_endpoint_open(make_driver) -> None:
    """Row 2: silence >= 250 ms but the partial ends mid-thought
    ("I want to" — dangling preposition, no terminal punctuation)."""
    d = make_driver()
    _activate(d)
    d.partial(INCOMPLETE)
    assert d.frames(SILENCE, 30) == []  # 960 ms of silence, still held


def test_row_3_silence_plus_complete_partial_endpoints(make_driver) -> None:
    """Row 3: both signals agree — endpoint on the first frame at/after
    250 ms of trailing silence, with honest endpoint_ms."""
    d = make_driver()
    _activate(d)  # speech ends at ts 224
    d.partial(COMPLETE)
    assert d.frames(SILENCE, 7) == []  # 224 ms < 250
    fired = d.frames(SILENCE, 1)  # 256 ms >= 250
    assert fired == [Endpoint(ts_ms=480, endpoint_ms=256, forced=False)]


def test_row_4_force_endpoint_at_max_delay_despite_incomplete(make_driver) -> None:
    """Row 4: the completeness check never says done, so the 2,000 ms
    backstop takes the turn anyway (forced=True)."""
    d = make_driver()
    _activate(d)
    d.partial(INCOMPLETE)
    assert d.frames(SILENCE, 62) == []  # 1,984 ms < 2,000
    fired = d.frames(SILENCE, 1)  # 2,016 ms >= 2,000
    assert fired == [Endpoint(ts_ms=2240, endpoint_ms=2016, forced=True)]


# --------------------------------------------------------------------------
# Boundary conditions (exactly-at-threshold)
# --------------------------------------------------------------------------


def test_endpoint_exactly_at_silence_threshold(make_driver) -> None:
    d = make_driver(endpoint_silence_ms=224)  # divisible by the 32 ms hop
    _activate(d)
    d.partial(COMPLETE)
    assert d.frames(SILENCE, 6) == []  # 192 ms < 224
    fired = d.frames(SILENCE, 1)  # exactly 224 — inclusive
    assert fired == [Endpoint(ts_ms=448, endpoint_ms=224, forced=False)]


def test_force_cap_exactly_at_threshold(make_driver) -> None:
    d = make_driver(max_endpoint_delay_ms=320)  # divisible by the 32 ms hop
    _activate(d)
    d.partial(INCOMPLETE)
    assert d.frames(SILENCE, 9) == []  # 288 ms: >= 250 but incomplete, < 320
    fired = d.frames(SILENCE, 1)  # exactly 320 — inclusive
    assert fired == [Endpoint(ts_ms=544, endpoint_ms=320, forced=True)]


# --------------------------------------------------------------------------
# §5.1 — mid-utterance pause
# --------------------------------------------------------------------------


def test_mid_utterance_pause_holds_then_endpoints_when_thought_completes(make_driver) -> None:
    """The doc's canonical failure: 'I want to… [pause 400 ms] …pay a
    vendor.' The hold keeps the turn open through the pause; resumed
    speech continues the SAME utterance (no second SpeechStart); the
    endpoint fires on the next agreed silence."""
    d = make_driver()
    _activate(d)
    d.partial(INCOMPLETE)
    assert d.frames(SILENCE, 13) == []  # 416 ms pause — held by row 2
    assert d.frames(SPEECH, 5) == []  # "…pay a vendor" resumes — no new SpeechStart
    d.partial(COMPLETE)
    fired = d.frames(SILENCE, 8)  # 256 ms >= 250, now complete
    assert fired == [Endpoint(ts_ms=1056, endpoint_ms=256, forced=False)]
    assert [type(e) for e in d.events] == [SpeechStart, Endpoint]


# --------------------------------------------------------------------------
# §5.2 — filler handling
# --------------------------------------------------------------------------


def test_filler_partial_is_non_terminal_and_holds(make_driver) -> None:
    """'Hmm.' has terminal punctuation but ends on a filler — the
    completeness check treats it as unfinished, so no row-3 endpoint."""
    d = make_driver()
    _activate(d)
    d.partial("Hmm.")
    assert d.frames(SILENCE, 20) == []  # 640 ms, still held


def test_filler_only_utterance_suppressed_at_force_cap(make_driver) -> None:
    """A filler-only utterance never becomes a turn: the force cap fires
    but the endpoint is suppressed (no event), and the machine resets
    cleanly for the next real utterance."""
    d = make_driver(max_endpoint_delay_ms=300)
    _activate(d)
    d.partial("hmm, uh…")
    assert d.frames(SILENCE, 10) == []  # cap crossed at 320 ms — suppressed, no event
    # Machine is back in listening: a real utterance still works end to end.
    assert len(d.frames(SPEECH, 7)) == 1  # fresh SpeechStart
    d.partial("Send five hundred rupees.")
    fired = d.frames(SILENCE, 9)
    assert len(fired) == 1
    assert isinstance(fired[0], Endpoint)
    assert fired[0].forced is False


def test_no_partial_at_force_cap_is_suppressed(make_driver) -> None:
    """Activation with no STT partial ever arriving (sustained non-speech
    noise fooled the VAD) reaches the cap and is dropped — an empty
    transcript must not open a turn."""
    d = make_driver(max_endpoint_delay_ms=300)
    _activate(d)
    assert d.frames(SILENCE, 10) == []


# --------------------------------------------------------------------------
# endpoint_ms honesty (§4.1) and state reset
# --------------------------------------------------------------------------


def test_endpoint_ms_reports_actual_silence_waited_for_late_partial(make_driver) -> None:
    """When the completing partial lands late (STT lag), the endpoint
    fires on the next frame and endpoint_ms reports the silence actually
    waited — not the configured 250 ms minimum."""
    d = make_driver()
    _activate(d)
    d.partial(INCOMPLETE)
    assert d.frames(SILENCE, 12) == []  # 384 ms held as incomplete
    d.partial(COMPLETE)  # the finishing partial arrives now
    fired = d.frames(SILENCE, 1)  # 416 ms of real trailing silence
    assert fired == [Endpoint(ts_ms=640, endpoint_ms=416, forced=False)]


def test_state_resets_after_endpoint_for_next_utterance(make_driver) -> None:
    """After an endpoint the machine starts over: fresh min-speech
    qualification, fresh SpeechStart, and the previous utterance's
    partial is cleared (a stale complete partial must not endpoint the
    next utterance)."""
    d = make_driver()
    _activate(d)
    d.partial("Check my balance.")
    assert len(d.frames(SILENCE, 9)) == 1  # first endpoint

    fired = d.frames(SPEECH, 7)  # second utterance requalifies min-speech
    assert len(fired) == 1
    second_start = fired[0]
    assert isinstance(second_start, SpeechStart)
    assert second_start.onset_ts_ms == 512  # the new run's onset, not the old one
    # 288 ms of silence with NO new partial: the old "Check my balance."
    # must not leak through the completeness check.
    assert d.frames(SILENCE, 9) == []
    d.partial("Pay the vendor.")
    fired = d.frames(SILENCE, 1)
    assert fired == [Endpoint(ts_ms=1056, endpoint_ms=320, forced=False)]


# --------------------------------------------------------------------------
# Completeness / filler heuristics (§5, §5.2) as pure functions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "complete"),
    [
        ("I want to pay a vendor.", True),
        ("How much did I send?", True),  # content verb — deliberately NOT dangling
        ("Theek hai, done!", True),
        ("I want to", False),  # dangling preposition, no punctuation
        ("I want to.", False),  # dangling preposition even WITH punctuation
        ("and then.", False),  # dangling conjunction despite punctuation
        ("send it to", False),  # the doc's third example
        ("pay a vendor", False),  # no terminal punctuation
        ("Hmm.", False),  # filler is non-terminal (§5.2)
        ("", False),  # no partial yet — never complete
        ("...", False),  # punctuation with no words
    ],
)
def test_looks_complete(text: str, complete: bool) -> None:
    assert looks_complete(text) is complete


@pytest.mark.parametrize(
    ("text", "filler_only"),
    [
        ("hmm", True),
        ("uh umm hmm", True),
        ("Uh, hmm…", True),  # case and punctuation don't matter
        ("", True),  # empty partial counts as nothing-worth-a-turn
        ("uh pay the vendor", False),
        ("Check my balance.", False),
    ],
)
def test_is_filler_only(text: str, filler_only: bool) -> None:
    assert is_filler_only(text) is filler_only


# --------------------------------------------------------------------------
# Input validation at the frame boundary
# --------------------------------------------------------------------------


def test_frame_with_non_positive_samples_is_rejected(make_driver) -> None:
    d = make_driver()
    bad = AudioFrame(pcm16=b"", sample_rate=SAMPLE_RATE_HZ, samples=0, ts_ms=0)
    with pytest.raises(ValueError, match="non-positive"):
        d.endpointer.on_frame(bad)
