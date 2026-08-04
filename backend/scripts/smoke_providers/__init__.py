"""Live-provider smoke harness — the operator-run answer to H1 ("no live
API keys exist in the build environment, so every provider wire assumption
is pinned only against fake servers").

`tests/providers/test_deepgram.py` and `test_elevenlabs.py` mark every
vendor-pinned behavior `PINNED(vendor)`; this package is their live
counterpart. It talks to the REAL Deepgram and ElevenLabs endpoints, and
prints a PASS/FAIL/UNKNOWN line per assumption, each cross-referenced to
the judgment-call number in the provider module it validates.

**It costs real money and it must never run in CI.** It lives in
`scripts/`, not `tests/`; nothing here is named `test_*`; importing any
module in this package performs no I/O, reads no env, and opens no socket
— all work is behind `main()`. `.github/workflows/ci.yml` runs
`pytest tests …` (and `pyproject.toml` pins `testpaths = ["tests"]`), so
there is no collection path that reaches this code.

Run from `backend/` (needs the `[voice]` extra for `websockets`/`numpy`):

    python -m scripts.smoke_providers                    # both legs, synthetic audio
    python -m scripts.smoke_providers --wav sample.wav   # both legs, real speech
    python -m scripts.smoke_providers --elevenlabs-only  # just the TTS leg

Judgment calls made here, recorded so review argues with the reasoning and
not the diff:

1. **Two probes per provider: a RAW socket probe and a real-provider
   run.** They answer different questions and neither subsumes the other.
   The raw probe sees the vendor cadence the provider's mapping was built
   ON (Deepgram's `is_final`/`speech_final` flags, ElevenLabs'
   un-rebased `charStartTimesMs`) — information the provider deliberately
   consumes and discards. The provider run sees whether our mapping
   survives that cadence (one `SttFinal` per utterance, a sane segment
   join, a TTFB, an alignment ceiling that stays quiet). Reporting only
   one of the two would leave the interesting half of H1 unanswered.
2. **The probes reach for the providers' own private URL builders and
   frame constants** (`DeepgramStt._listen_url`,
   `ElevenLabsTts._stream_input_url`, `elevenlabs._MSG_BOS`/`_MSG_EOS`).
   Re-spelling those here would create a second, drifting copy of the
   wire — and a harness that validates a wire production does not send is
   worse than no harness. Private access is the point, not an oversight.
3. **Synthetic audio validates the wire, not the words.** `--synthetic`
   generates a speech-like `/u a u a/` phrase (technique lifted from
   `tests/voice/test_silero.py::_synthetic_voiced_phrase`, reimplemented
   in `audio.py` — importing from `tests/` is not allowed); Silero reads
   it as speech, but it is not language and Deepgram will not transcribe
   words out of it. Every transcript-dependent check therefore reports
   UNKNOWN in synthetic mode and never PASS. Pass `--wav` with real
   speech to turn those into real verdicts.
4. **Audio is paced in real time** (20 ms frames, one wall-clock 20 ms
   apart). Blasting a 5 s clip in one burst would hand Deepgram's
   endpointer the whole utterance at once and produce a cadence no live
   call ever sees — which is exactly the cadence judgment call 1 needs
   measured honestly.
5. **UNKNOWN is a first-class verdict and never fails the run.** Only
   FAIL sets exit 1 (2 is reserved for bad invocation / missing keys).
   "The live stream did not exercise this" and "the pinned assumption is
   wrong" are different findings and an operator must be able to tell
   them apart at a glance.
6. **Settings placeholder substitution.** `Settings` has three required
   fields unrelated to voice (`jwt_secret`, `database_url`,
   `openrouter_api_key`); demanding a Postgres URL to smoke-test a TTS
   socket is absurd, so `cli.py` substitutes obviously-inert placeholders
   for exactly those three when absent — loudly, on stdout — and lets any
   OTHER validation error fail. Nothing here reads those fields.
7. **API keys are never printed, logged, or put in a URL.** Keys are
   unwrapped from `SecretStr` only into a handshake header, as a
   short-lived local, the same discipline `app/providers/*.py` follows.
   The URLs this harness prints carry query params only — Deepgram's key
   rides `Authorization`, ElevenLabs' rides `xi-api-key`.
"""

from __future__ import annotations

from scripts.smoke_providers.cli import main

__all__ = ["main"]
