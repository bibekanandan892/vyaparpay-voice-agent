"""Operator entry point: argument parsing, key gating, usage banners, and
orchestration of the two legs. See the package docstring for the harness-
wide judgment calls this file implements (6: Settings placeholder
substitution; 5: UNKNOWN never fails the run; 7: keys never printed).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from app.config import Settings, get_settings
from scripts.smoke_providers import audio, deepgram_leg, elevenlabs_leg
from scripts.smoke_providers.report import Check, exit_code, render_summary

# Judgment call 6: inert placeholders for the three Settings fields this
# harness never touches (no engine, no Redis, no LLM call happens here).
# Real values, if the operator already has a backend/.env, win — these
# only fill gaps so Settings() doesn't fail-fast on unrelated fields.
_PLACEHOLDER_ENV: Final = {
    "JWT_SECRET": "smoke-harness-unused-placeholder",
    "DATABASE_URL": "postgresql+asyncpg://smoke:smoke@localhost/smoke_harness_unused",
    "OPENROUTER_API_KEY": "sk-or-smoke-harness-unused-placeholder",
}

# Just over Deepgram's ~10 s NET-0001 idle-close timeout (deepgram.py JC 5).
_DEFAULT_IDLE_S: Final = 11.0

_BANNER: Final = (
    "=" * 78
    + "\n LIVE PROVIDER SMOKE — talks to the REAL Deepgram/ElevenLabs APIs.\n"
    " This incurs real vendor usage charges (small, but not zero).\n" + "=" * 78
)


def _ensure_placeholder_env() -> None:
    """Fill in the three Settings fields this harness never uses, loudly,
    only when actually absent — never overwrites a real operator value."""
    for name, placeholder in _PLACEHOLDER_ENV.items():
        if not os.environ.get(name):
            os.environ[name] = placeholder
            print(f"[smoke_providers] {name} not set — using an inert placeholder (unused here)")


def _load_settings() -> Settings:
    _ensure_placeholder_env()
    return get_settings()


def _blank(secret: object) -> bool:
    """True for both "the env var is absent" (None) and "the env var is
    present but empty" (SecretStr("")) — `.env.example` ships every voice
    key as a bare `KEY=` line, and pydantic-settings parses that as an
    empty SecretStr, not None, so an `is None` check alone misses the
    single most likely operator mistake: copying .env.example and
    forgetting to fill the key in."""
    return secret is None or (isinstance(secret, SecretStr) and not secret.get_secret_value())


def _missing_env_vars(
    settings: Settings, *, want_deepgram: bool, want_elevenlabs: bool
) -> list[str]:
    """Judgment call: gate on Settings' parsed view, not raw os.environ —
    a value supplied via backend/.env should count as configured too."""
    missing = []
    if want_deepgram and _blank(settings.deepgram_api_key):
        missing.append("DEEPGRAM_API_KEY")
    if want_elevenlabs:
        if _blank(settings.elevenlabs_api_key):
            missing.append("ELEVENLABS_API_KEY")
        if not settings.elevenlabs_voice_id:
            missing.append("ELEVENLABS_VOICE_ID")
    return missing


def _prepare_deepgram_audio(args: argparse.Namespace) -> tuple[bytes, int, bool]:
    """Returns (pcm, sample_rate, is_real_speech). `--synthetic` forces the
    generated phrase even when `--wav` is also given (harness JC 3: only
    real speech turns the transcript-dependent checks into real verdicts)."""
    if args.wav is not None and not args.synthetic:
        pcm, rate = audio.load_wav(args.wav)
        return pcm, rate, True
    return audio.synthetic_voiced_phrase(), audio.SAMPLE_RATE_HZ, False


def _print_usage_banner(
    *, run_deepgram: bool, dg_pcm: bytes, dg_rate: int, idle_s: float | None,
    run_elevenlabs: bool, sentence: str,
) -> None:
    """"Say the estimated usage up front" — durations/character counts,
    deliberately not a dollar figure (vendor pricing moves; a stale
    hardcoded rate would mislead more than it helps)."""
    print(_BANNER)
    if run_deepgram:
        secs = audio.duration_s(dg_pcm, dg_rate)
        idle_note = f", plus one ~{idle_s:.0f}s idle KeepAlive window" if idle_s else ""
        print(
            f" Deepgram : ~{secs:.1f}s of audio streamed twice (raw probe + provider "
            f"probe){idle_note}. Check current Deepgram pricing for an exact figure."
        )
    if run_elevenlabs:
        print(
            f" ElevenLabs: one {len(sentence)}-character sentence synthesized twice "
            "(raw probe + provider probe). Check current ElevenLabs pricing for an "
            "exact figure."
        )
    print("=" * 78)


async def _run_checks(settings: Settings, args: argparse.Namespace) -> list[Check]:
    run_deepgram = not args.elevenlabs_only
    run_elevenlabs = not args.deepgram_only
    dg_pcm, dg_rate, real_speech = (b"", audio.SAMPLE_RATE_HZ, False)
    if run_deepgram:
        dg_pcm, dg_rate, real_speech = _prepare_deepgram_audio(args)
    idle_s = None if args.skip_keepalive else args.idle_s
    _print_usage_banner(
        run_deepgram=run_deepgram,
        dg_pcm=dg_pcm,
        dg_rate=dg_rate,
        idle_s=idle_s,
        run_elevenlabs=run_elevenlabs,
        sentence=args.sentence,
    )

    checks: list[Check] = []
    if run_deepgram:
        checks.extend(
            await deepgram_leg.run(
                settings, pcm=dg_pcm, sample_rate=dg_rate, real_speech=real_speech, idle_s=idle_s
            )
        )
    if run_elevenlabs:
        checks.extend(await elevenlabs_leg.run(settings, text=args.sentence))
    return checks


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke_providers",
        description="Live-provider smoke harness for DeepgramStt/ElevenLabsTts (H1) — "
        "validates PINNED(vendor) wire assumptions against the REAL APIs. Costs real "
        "money; never run this in CI.",
    )
    legs = parser.add_mutually_exclusive_group()
    legs.add_argument("--deepgram-only", action="store_true", help="run only the Deepgram STT leg")
    legs.add_argument(
        "--elevenlabs-only", action="store_true", help="run only the ElevenLabs TTS leg"
    )
    parser.add_argument(
        "--wav", type=Path, default=None,
        help="mono 16-bit PCM WAV of real speech for the Deepgram leg (default: --synthetic; "
        "transcript-dependent checks report UNKNOWN without real speech)",
    )
    parser.add_argument(
        "--synthetic", action="store_true",
        help="force synthetic speech-like audio even if --wav is given",
    )
    parser.add_argument(
        "--sentence", default=elevenlabs_leg.DEFAULT_SENTENCE,
        help="sentence to synthesize for the ElevenLabs leg",
    )
    parser.add_argument(
        "--idle-s", type=float, default=_DEFAULT_IDLE_S,
        help=f"idle-window duration for the Deepgram KeepAlive check "
        f"(default: {_DEFAULT_IDLE_S:.0f}s, just over the vendor's ~10s NET-0001 timeout)",
    )
    parser.add_argument(
        "--skip-keepalive", action="store_true",
        help="skip the idle-window KeepAlive check (faster, less audio streamed)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = _load_settings()
    run_deepgram = not args.elevenlabs_only
    run_elevenlabs = not args.deepgram_only

    missing = _missing_env_vars(
        settings, want_deepgram=run_deepgram, want_elevenlabs=run_elevenlabs
    )
    if missing:
        print(
            f"[smoke_providers] missing required env var(s): {', '.join(missing)} — "
            "set them (see backend/.env.example) and re-run.",
            file=sys.stderr,
        )
        return 2

    try:
        checks = asyncio.run(_run_checks(settings, args))
    except (OSError, ValueError) as exc:
        # A bad --wav path/format, or a provider construction error that
        # somehow slipped past the env-var gate above — an invocation
        # problem, not a vendor finding.
        print(f"[smoke_providers] {exc}", file=sys.stderr)
        return 2

    print(render_summary(checks))
    return exit_code(checks)


__all__ = ["main"]
