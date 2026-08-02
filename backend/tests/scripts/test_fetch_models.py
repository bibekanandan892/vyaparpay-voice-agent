"""Unit tests for `scripts/fetch_models.py`.

NO network anywhere here: every test either drives pure logic
(`sha256_of`, the https guard) or monkeypatches `fetch_models.download`
— `fetch_model` looks the downloader up as a module global at call time,
so the patch is all it takes. The real download path was executed once
manually against the pinned URL/hash; see the task report.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts import fetch_models

GOOD_BYTES = b"pretend-onnx-model-bytes"
GOOD_SHA256 = hashlib.sha256(GOOD_BYTES).hexdigest()


def make_dest(tmp_path: Path) -> Path:
    # Mirrors the real layout one level deep so mkdir(parents=True) is
    # actually exercised, not just tolerated.
    return tmp_path / "models" / "silero_vad.onnx"


# --------------------------------------------------------------------------
# sha256_of
# --------------------------------------------------------------------------


def test_sha256_of_matches_hashlib_on_known_content(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(GOOD_BYTES)
    assert fetch_models.sha256_of(f) == GOOD_SHA256


def test_sha256_of_streams_files_larger_than_one_chunk(tmp_path: Path) -> None:
    big = b"x" * (fetch_models._CHUNK_SIZE * 2 + 17)  # spans >2 read chunks
    f = tmp_path / "big.bin"
    f.write_bytes(big)
    assert fetch_models.sha256_of(f) == hashlib.sha256(big).hexdigest()


# --------------------------------------------------------------------------
# fetch_model — skip-if-present-and-verified
# --------------------------------------------------------------------------


def test_skips_download_when_present_and_hash_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = make_dest(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(GOOD_BYTES)

    def _must_not_download(url: str, dest: Path) -> None:
        raise AssertionError("download() must not be called when the file is verified-present")

    monkeypatch.setattr(fetch_models, "download", _must_not_download)

    result = fetch_models.fetch_model(url="https://example.invalid/m.onnx",
                                      sha256=GOOD_SHA256, dest=dest)

    assert result == dest
    assert dest.read_bytes() == GOOD_BYTES  # untouched


def test_redownloads_when_present_but_hash_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = make_dest(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"corrupt-or-stale-model")

    def _fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(GOOD_BYTES)

    monkeypatch.setattr(fetch_models, "download", _fake_download)

    result = fetch_models.fetch_model(url="https://example.invalid/m.onnx",
                                      sha256=GOOD_SHA256, dest=dest)

    assert result == dest
    assert dest.read_bytes() == GOOD_BYTES  # replaced with verified content


# --------------------------------------------------------------------------
# fetch_model — download + verify path
# --------------------------------------------------------------------------


def test_downloads_and_verifies_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = make_dest(tmp_path)  # parent dir doesn't exist yet
    calls: list[str] = []

    def _fake_download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(GOOD_BYTES)

    monkeypatch.setattr(fetch_models, "download", _fake_download)

    result = fetch_models.fetch_model(url="https://example.invalid/m.onnx",
                                      sha256=GOOD_SHA256, dest=dest)

    assert calls == ["https://example.invalid/m.onnx"]
    assert result == dest
    assert dest.read_bytes() == GOOD_BYTES


def test_hash_mismatch_raises_and_deletes_the_bad_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = make_dest(tmp_path)

    def _fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"not-the-pinned-model")

    monkeypatch.setattr(fetch_models, "download", _fake_download)

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        fetch_models.fetch_model(url="https://example.invalid/m.onnx",
                                 sha256=GOOD_SHA256, dest=dest)

    assert not dest.exists()  # the bad file must not survive


def test_download_failure_removes_the_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = make_dest(tmp_path)

    def _fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"half-a-mod")  # simulate a partial write...
        raise OSError("connection reset mid-stream")

    monkeypatch.setattr(fetch_models, "download", _fake_download)

    with pytest.raises(OSError, match="connection reset"):
        fetch_models.fetch_model(url="https://example.invalid/m.onnx",
                                 sha256=GOOD_SHA256, dest=dest)

    assert not dest.exists()  # partial file must not pass a later exists() check


# --------------------------------------------------------------------------
# download — https guard (pure logic, raises before any network I/O)
# --------------------------------------------------------------------------


def test_download_refuses_non_https_urls(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-https"):
        fetch_models.download("http://example.invalid/m.onnx", tmp_path / "m.onnx")


# --------------------------------------------------------------------------
# The pinned constants themselves — shape checks so a bad edit (branch
# instead of tag, truncated hash) fails here, not in CI's fetch step.
# --------------------------------------------------------------------------


def test_pinned_url_is_https_tagged_and_hash_is_wellformed() -> None:
    assert fetch_models.MODEL_URL.startswith("https://raw.githubusercontent.com/snakers4/")
    assert f"/{fetch_models.MODEL_TAG}/" in fetch_models.MODEL_URL
    assert fetch_models.MODEL_TAG.startswith("v")  # a tag, not a branch name
    assert len(fetch_models.MODEL_SHA256) == 64
    assert set(fetch_models.MODEL_SHA256) <= set("0123456789abcdef")
    assert fetch_models.MODEL_PATH.name == "silero_vad.onnx"
    assert fetch_models.MODEL_PATH.parent.name == "models"
