"""Fetches the Silero VAD ONNX model for local dev and CI (docs/06 §5 —
`VadEndpointer` runs Silero over onnxruntime; the model file itself is
~2.3 MB and deliberately NOT committed: `app/voice/models/` is
gitignored, and this script is the one sanctioned way to populate it).

Supply-chain pinning, both halves on purpose:

- The URL pins a release *tag* (v6.2.1) of the official
  snakers4/silero-vad repo, never a moving branch — a `master` URL can
  silently start serving a different model between two CI runs.
- The SHA-256 below was computed from a manual download of exactly that
  tagged file at pin time. Even a compromised or force-pushed tag can't
  slip a different artifact through: the hash check fails loudly and
  deletes the bad file rather than leaving it for onnxruntime to load.

Idempotent by construction (same contract as `scripts/seed.py`): if the
file is already present AND hash-verified, the script exits without
touching the network — which is what makes the CI `actions/cache` step
keyed on this hash effectively free on a warm cache.

stdlib only (urllib + hashlib): this must run in CI *before*
`.[voice]`'s own deps are importable-adjacent concerns, and a model
fetcher that needs third-party packages to bootstrap is a bootstrap
problem.

Run from backend/: `python -m scripts.fetch_models`
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

# The pinned release tag of github.com/snakers4/silero-vad. Bumping the
# model = bump this tag AND recompute MODEL_SHA256 from a fresh manual
# download — never one without the other.
MODEL_TAG = "v6.2.1"
MODEL_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    f"{MODEL_TAG}/src/silero_vad/data/silero_vad.onnx"
)
# SHA-256 of silero_vad.onnx at MODEL_TAG (2,327,524 bytes), computed
# from a manual download at pin time.
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
# Resolved relative to this file, not CWD, so the script lands the model
# in backend/app/voice/models/ no matter where it's invoked from.
MODEL_PATH = Path(__file__).resolve().parent.parent / "app" / "voice" / "models" / "silero_vad.onnx"

_CHUNK_SIZE = 1 << 16  # 64 KiB read chunks — stream, don't slurp
_DOWNLOAD_TIMEOUT_S = 60


def sha256_of(path: Path) -> str:
    """Streaming SHA-256 of a file (chunked — never loads it whole)."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    """Streams `url` to `dest`. https only — a pin that quietly became
    plain-http would defeat the whole point of pinning."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https model URL: {url}")
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp, dest.open("wb") as out:
        while chunk := resp.read(_CHUNK_SIZE):
            out.write(chunk)


def fetch_model(url: str = MODEL_URL, sha256: str = MODEL_SHA256, dest: Path = MODEL_PATH) -> Path:
    """Skip-if-present-and-verified, else download + verify.

    Fails loudly (RuntimeError) on a hash mismatch and deletes the bad
    file first — a wrong model left on disk would otherwise be silently
    loaded by onnxruntime on the next worker start.
    """
    if dest.exists() and sha256_of(dest) == sha256:
        print(f"[fetch_models] already present, hash verified — skipping: {dest}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch_models] downloading {url}")
    try:
        download(url, dest)
    except Exception:
        # A partial file that half-downloaded must not survive to pass
        # a future exists() check before its hash check runs.
        dest.unlink(missing_ok=True)
        raise

    actual = sha256_of(dest)
    if actual != sha256:
        dest.unlink()
        raise RuntimeError(
            f"SHA-256 mismatch for {dest.name}: expected {sha256}, got {actual} — "
            f"deleted the bad file. The content behind the pinned URL ({url}) "
            "has changed; re-verify upstream before re-pinning."
        )
    print(f"[fetch_models] downloaded and hash verified: {dest}")
    return dest


def main() -> int:
    fetch_model()
    return 0


if __name__ == "__main__":
    sys.exit(main())
