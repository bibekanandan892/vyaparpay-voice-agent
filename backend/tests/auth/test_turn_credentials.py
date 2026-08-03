"""Unit tests for app.auth.turn_credentials — coturn `use-auth-secret`
REST credentials (docs/13-api-contracts.md §7).

The load-bearing test here is `test_credential_matches_a_known_hmac_sha1_
vector`: coturn recomputes this value independently with the shared
secret, so a self-consistent implementation ("whatever the code produces")
would pass every other assertion in this file and still fail every real
TURN allocation. The vector below pins the algorithm itself — digest
(SHA-1, not SHA-256), key/message order, and base64 (not hex) output.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.auth.turn_credentials import (
    build_turn_username,
    compute_turn_credential,
    mint_ice_servers,
)
from app.config import Settings
from tests.conftest import SettingsFactory

_SESSION_ID = "a1f3c9"
_COTURN_HOST = "turn.vyapar.local"

# --- the pinned vector ----------------------------------------------------
# base64(HMAC-SHA1(b"vyapar-turn-test-secret", b"1784537062:a1f3c9")).
# Independently reproducible:
#   python -c "import base64,hashlib,hmac; print(base64.b64encode(
#     hmac.new(b'vyapar-turn-test-secret', b'1784537062:a1f3c9',
#              hashlib.sha1).digest()).decode())"
_VECTOR_SECRET = "vyapar-turn-test-secret"
_VECTOR_USERNAME = "1784537062:a1f3c9"
_VECTOR_CREDENTIAL = "Jr23cAAm/9dZc2UxipX+A2ltpvg="

_NOW = datetime(2026, 7, 24, 8, 44, 22, tzinfo=UTC)
_TURN_TTL_S = 600


@pytest.fixture
def voice_settings(settings_factory: SettingsFactory) -> Settings:
    return settings_factory(
        turn_secret=SecretStr(_VECTOR_SECRET),
        coturn_host=_COTURN_HOST,
        turn_credential_ttl_s=_TURN_TTL_S,
    )


# --------------------------------------------------------------------------
# The algorithm itself (docs/13 §7)
# --------------------------------------------------------------------------


def test_credential_matches_a_known_hmac_sha1_vector() -> None:
    assert compute_turn_credential(_VECTOR_SECRET, _VECTOR_USERNAME) == _VECTOR_CREDENTIAL


def test_credential_changes_with_the_secret() -> None:
    """Rotating `TURN_SECRET` invalidates every outstanding credential
    (docs/13 §7's rotation row)."""
    assert compute_turn_credential("other-secret", _VECTOR_USERNAME) != _VECTOR_CREDENTIAL


def test_username_is_expiry_then_session_id() -> None:
    assert build_turn_username(_SESSION_ID, 1_784_537_062) == _VECTOR_USERNAME


def test_username_rejects_a_session_id_containing_the_separator() -> None:
    """A ':' in the id would forge a different (expiry, session) split on
    coturn's side."""
    with pytest.raises(ValueError, match="must not contain"):
        build_turn_username("a1f3:c9", 1_784_537_062)


# --------------------------------------------------------------------------
# ice_servers shape (docs/13 §2.1's worked example)
# --------------------------------------------------------------------------


def test_ice_servers_are_stun_first_then_turn(voice_settings: Settings) -> None:
    stun, turn = mint_ice_servers(_SESSION_ID, voice_settings, now=_NOW)

    assert stun.urls == (f"stun:{_COTURN_HOST}:3478",)
    assert stun.username is None
    assert stun.credential is None
    assert turn.urls == (
        f"turn:{_COTURN_HOST}:3478?transport=udp",
        f"turns:{_COTURN_HOST}:5349",
    )


def test_turn_entry_carries_the_expiring_hmac_pair(voice_settings: Settings) -> None:
    """The expiry inside the username is `now + turn_credential_ttl_s`
    (docs/13 §7's 10 minutes), and the credential signs that exact
    username."""
    _, turn = mint_ice_servers(_SESSION_ID, voice_settings, now=_NOW)

    expected_username = f"{int(_NOW.timestamp()) + _TURN_TTL_S}:{_SESSION_ID}"
    assert turn.username == expected_username
    assert turn.credential == compute_turn_credential(_VECTOR_SECRET, expected_username)


def test_stun_entry_omits_credential_keys_on_the_wire(voice_settings: Settings) -> None:
    """`to_wire()`'s exclude_none, not a nulled field — the STUN entry in
    docs/13 §2.1 has no `username`/`credential` keys at all."""
    stun, turn = mint_ice_servers(_SESSION_ID, voice_settings, now=_NOW)

    assert stun.to_wire() == {"urls": [f"stun:{_COTURN_HOST}:3478"]}
    assert set(turn.to_wire()) == {"urls", "username", "credential"}


def test_credential_is_excluded_from_repr(voice_settings: Settings) -> None:
    _, turn = mint_ice_servers(_SESSION_ID, voice_settings, now=_NOW)

    assert turn.credential is not None
    assert turn.credential not in repr(turn)


# --------------------------------------------------------------------------
# Misconfiguration (note 4) — loud, and never echoing the secret
# --------------------------------------------------------------------------


def test_missing_turn_secret_raises(settings_factory: SettingsFactory) -> None:
    settings = settings_factory(coturn_host=_COTURN_HOST)

    with pytest.raises(RuntimeError, match="TURN_SECRET is not configured"):
        mint_ice_servers(_SESSION_ID, settings, now=_NOW)


def test_missing_coturn_host_raises(settings_factory: SettingsFactory) -> None:
    settings = settings_factory(turn_secret=SecretStr(_VECTOR_SECRET))

    with pytest.raises(RuntimeError, match="COTURN_HOST is not configured"):
        mint_ice_servers(_SESSION_ID, settings, now=_NOW)


def test_misconfiguration_message_never_contains_the_secret(
    settings_factory: SettingsFactory,
) -> None:
    settings = settings_factory(turn_secret=SecretStr(_VECTOR_SECRET))

    with pytest.raises(RuntimeError) as exc_info:
        mint_ice_servers(_SESSION_ID, settings, now=_NOW)

    assert _VECTOR_SECRET not in str(exc_info.value)
