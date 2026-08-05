"""Unit tests for app.memory.session_memory.SessionMemory.

`RedisClient` is mocked (`AsyncMock(spec=RedisClient)`) per the Batch 3.4
task brief — Docker/Redis is not available in this environment.
`RedisClient` itself is already exercised against a hand-rolled in-memory
fake in tests/data/test_redis_client.py; this file only has to prove
`SessionMemory`'s business logic (window cap, domain round-trips, cost
accumulation, digest formatting) on top of that already-tested boundary.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.data.redis_client import RedisClient
from app.domain.types import (
    Message,
    PendingConfirm,
    Role,
    RollingSummary,
    ToolInvocationStatus,
    ToolResult,
)
from app.memory.session_memory import _MAX_DIGEST_CHARS, _MAX_TRANSCRIPT_TURNS, SessionMemory


@pytest.fixture
def redis_client() -> AsyncMock:
    return AsyncMock(spec=RedisClient)


@pytest.fixture
def memory(redis_client: AsyncMock) -> SessionMemory:
    return SessionMemory(redis_client)


# --------------------------------------------------------------------------
# transcript window
# --------------------------------------------------------------------------


async def test_get_window_is_empty_when_no_transcript(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_transcript_window.return_value = []

    assert await memory.get_window("sess_1") == []


async def test_get_window_deserializes_raw_dicts_into_messages(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_transcript_window.return_value = [
        Message(role=Role.USER, content="My payment got declined").model_dump(mode="json"),
        Message(role=Role.ASSISTANT, content="Let me check that.").model_dump(mode="json"),
    ]

    window = await memory.get_window("sess_1")

    assert window == [
        Message(role=Role.USER, content="My payment got declined"),
        Message(role=Role.ASSISTANT, content="Let me check that."),
    ]


async def test_append_message_writes_back_through_get_modify_set(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_transcript_window.return_value = []

    await memory.append_message("sess_1", Message(role=Role.USER, content="hi"))

    redis_client.set_transcript_window.assert_awaited_once()
    session_id, turns = redis_client.set_transcript_window.await_args.args
    assert session_id == "sess_1"
    assert turns == [Message(role=Role.USER, content="hi").model_dump(mode="json")]


async def test_append_message_caps_the_window_at_8_turns(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    existing = [
        Message(role=Role.USER, content=f"turn {i}").model_dump(mode="json")
        for i in range(_MAX_TRANSCRIPT_TURNS)
    ]
    redis_client.get_transcript_window.return_value = existing

    await memory.append_message("sess_1", Message(role=Role.USER, content="turn 8"))

    _, turns = redis_client.set_transcript_window.await_args.args
    assert len(turns) == _MAX_TRANSCRIPT_TURNS


async def test_append_message_eviction_is_drop_oldest_fifo(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    """When the 9th entry is appended, the oldest (turn 0) is evicted —
    docs/09 §3's transcript is the recent-context window, not an archive;
    older turns live only in the rolling summary (Phase 5, out of scope
    here)."""
    existing = [
        Message(role=Role.USER, content=f"turn {i}").model_dump(mode="json")
        for i in range(_MAX_TRANSCRIPT_TURNS)
    ]
    redis_client.get_transcript_window.return_value = existing

    await memory.append_message("sess_1", Message(role=Role.USER, content="turn 8"))

    _, turns = redis_client.set_transcript_window.await_args.args
    contents = [t["content"] for t in turns]
    assert contents == [f"turn {i}" for i in range(1, _MAX_TRANSCRIPT_TURNS)] + ["turn 8"]


async def test_append_message_does_not_trim_under_the_cap(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_transcript_window.return_value = [
        Message(role=Role.USER, content="turn 0").model_dump(mode="json")
    ]

    await memory.append_message("sess_1", Message(role=Role.ASSISTANT, content="turn 1"))

    _, turns = redis_client.set_transcript_window.await_args.args
    assert [t["content"] for t in turns] == ["turn 0", "turn 1"]


async def test_append_message_fills_exactly_to_the_cap_without_eviction(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    """The other side of the off-by-one from the eviction tests above:
    7 existing + 1 new = exactly 8 (the cap) must keep all 8 entries,
    none dropped — eviction only starts on the 9th."""
    existing = [
        Message(role=Role.USER, content=f"turn {i}").model_dump(mode="json")
        for i in range(_MAX_TRANSCRIPT_TURNS - 1)
    ]
    redis_client.get_transcript_window.return_value = existing

    await memory.append_message("sess_1", Message(role=Role.USER, content="turn 7"))

    _, turns = redis_client.set_transcript_window.await_args.args
    assert len(turns) == _MAX_TRANSCRIPT_TURNS
    assert [t["content"] for t in turns] == [f"turn {i}" for i in range(_MAX_TRANSCRIPT_TURNS)]


# --------------------------------------------------------------------------
# pending_confirm — thin pass-through (see module docstring: supersession
# policy is deliberately NOT implemented here)
# --------------------------------------------------------------------------


async def test_get_pending_confirm_passes_through(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    pending = PendingConfirm(
        tool="request_limit_increase",
        args={"requested_limit": 50000},
        proposed_turn=5,
        invocation_id="inv_1",
    )
    redis_client.get_pending_confirm.return_value = pending

    result = await memory.get_pending_confirm("sess_1")

    redis_client.get_pending_confirm.assert_awaited_once_with("sess_1")
    assert result == pending


async def test_get_pending_confirm_passes_through_none(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_pending_confirm.return_value = None

    assert await memory.get_pending_confirm("sess_1") is None


async def test_set_pending_confirm_passes_through(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    pending = PendingConfirm(
        tool="request_limit_increase", args={}, proposed_turn=5, invocation_id="inv_1"
    )

    await memory.set_pending_confirm("sess_1", pending)

    redis_client.set_pending_confirm.assert_awaited_once_with("sess_1", pending)


async def test_set_pending_confirm_none_passes_through_to_clear(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    await memory.set_pending_confirm("sess_1", None)

    redis_client.set_pending_confirm.assert_awaited_once_with("sess_1", None)


# --------------------------------------------------------------------------
# running cost accumulation
# --------------------------------------------------------------------------


async def test_get_cost_passes_through(memory: SessionMemory, redis_client: AsyncMock) -> None:
    redis_client.get_running_cost.return_value = Decimal("0.1420")

    assert await memory.get_cost("sess_1") == Decimal("0.1420")


async def test_add_cost_accumulates_onto_the_existing_total(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    redis_client.get_running_cost.return_value = Decimal("0.10")

    new_total = await memory.add_cost("sess_1", Decimal("0.05"))

    assert new_total == Decimal("0.15")
    redis_client.set_running_cost.assert_awaited_once_with("sess_1", Decimal("0.15"))


async def test_add_cost_from_zero(memory: SessionMemory, redis_client: AsyncMock) -> None:
    redis_client.get_running_cost.return_value = Decimal("0")

    new_total = await memory.add_cost("sess_1", Decimal("0.0023"))

    assert new_total == Decimal("0.0023")


async def test_add_cost_is_read_modify_write_not_a_bare_set(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    """A regression a naive `set_running_cost(session_id, delta)`
    implementation would pass if the starting balance happened to be
    zero — assert the read actually feeds the write."""
    redis_client.get_running_cost.return_value = Decimal("1.00")

    await memory.add_cost("sess_1", Decimal("0.30"))

    redis_client.get_running_cost.assert_awaited_once_with("sess_1")
    redis_client.set_running_cost.assert_awaited_once_with("sess_1", Decimal("1.30"))


# --------------------------------------------------------------------------
# tool-result digest formatting (judgment call — module docstring #1:
# formatting only, no persistence path exists in RedisClient yet)
# --------------------------------------------------------------------------


def test_format_tool_digest_carries_tool_name_and_idempotency_key() -> None:
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="get_wallet_balance",
        ok=True,
        data={"balance": 18450, "currency": "INR"},
        status=ToolInvocationStatus.OK,
        latency_ms=8,
        idempotency_key="sess_1:get_wallet_balance:3",
    )

    digest = SessionMemory.format_tool_digest(result)

    assert digest["tool"] == "get_wallet_balance"
    assert digest["idem"] == "sess_1:get_wallet_balance:3"
    assert "18450" in digest["digest"]
    assert isinstance(digest["ts"], int)


def test_format_tool_digest_has_no_idem_when_result_has_none() -> None:
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="get_wallet_balance",
        ok=True,
        data={"balance": 18450},
        status=ToolInvocationStatus.OK,
        latency_ms=8,
    )

    digest = SessionMemory.format_tool_digest(result)

    assert digest["idem"] is None


def test_format_tool_digest_falls_back_to_error_payload_on_failure() -> None:
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="request_limit_increase",
        ok=False,
        error={"type": "business", "code": "LIMIT_REQUEST_ALREADY_PENDING"},
        status=ToolInvocationStatus.ERROR,
        latency_ms=5,
    )

    digest = SessionMemory.format_tool_digest(result)

    assert "LIMIT_REQUEST_ALREADY_PENDING" in digest["digest"]


def test_format_tool_digest_falls_back_to_gate_payload_when_pending() -> None:
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="request_limit_increase",
        ok=False,
        gate={"status": "pending_confirm", "action": {"tool": "request_limit_increase"}},
        status=ToolInvocationStatus.PENDING_CONFIRM,
        latency_ms=4,
    )

    digest = SessionMemory.format_tool_digest(result)

    assert "pending_confirm" in digest["digest"]


def test_format_tool_digest_truncates_long_payloads() -> None:
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="get_transactions",
        ok=True,
        data={"items": [{"id": f"txn_{i}", "amount": i} for i in range(100)]},
        status=ToolInvocationStatus.OK,
        latency_ms=12,
    )

    digest = SessionMemory.format_tool_digest(result)

    assert len(digest["digest"]) <= _MAX_DIGEST_CHARS + len("…(truncated)")
    assert digest["digest"].endswith("…(truncated)")


# --------------------------------------------------------------------------
# rolling summary — thin pass-through (docs/09 §3, §4). The fold ALGORITHM
# lives in app/memory/summarizer.py; this layer adds no business shape, so
# these tests assert exactly that and no more.
# --------------------------------------------------------------------------


async def test_get_summary_passes_through(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    summary = RollingSummary(text="covers turns 1-3", thru_turn=3)
    redis_client.get_summary.return_value = summary

    result = await memory.get_summary("sess_1")

    redis_client.get_summary.assert_awaited_once_with("sess_1")
    assert result == summary


async def test_get_summary_passes_through_none(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    """docs/09 §4.1 rule 1's state: no fold has landed, so the window
    still holds every turn verbatim."""
    redis_client.get_summary.return_value = None

    assert await memory.get_summary("sess_1") is None


async def test_set_summary_passes_through(
    memory: SessionMemory, redis_client: AsyncMock
) -> None:
    summary = RollingSummary(text="covers turns 1-9", thru_turn=9)

    await memory.set_summary("sess_1", summary)

    redis_client.set_summary.assert_awaited_once_with("sess_1", summary)
