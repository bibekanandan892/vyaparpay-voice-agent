"""Unit tests for app.memory.short_term.ShortTermMemory — a plain
in-process dataclass with no I/O, so these are construction/mutation
checks only (no Redis, no mocking needed)."""

from __future__ import annotations

from app.domain.types import ContextBundle, ToolInvocationStatus, ToolResult
from app.memory.short_term import ShortTermMemory


def test_constructs_empty_by_default() -> None:
    memory = ShortTermMemory()

    assert memory.context_bundle is None
    assert memory.tool_results == []
    assert memory.pending_confirm is None


def test_context_bundle_can_be_set_after_construction() -> None:
    memory = ShortTermMemory()
    bundle = ContextBundle(persona="Asha", business_rules="", user_profile="")

    memory.context_bundle = bundle

    assert memory.context_bundle is bundle


def test_record_tool_result_appends_in_order() -> None:
    memory = ShortTermMemory()
    first = ToolResult(
        tool_call_id="call_1",
        tool_name="get_wallet_balance",
        ok=True,
        data={"balance": 18450},
        status=ToolInvocationStatus.OK,
        latency_ms=8,
    )
    second = ToolResult(
        tool_call_id="call_2",
        tool_name="get_payment_status",
        ok=True,
        data={"status": "declined"},
        status=ToolInvocationStatus.OK,
        latency_ms=6,
    )

    memory.record_tool_result(first)
    memory.record_tool_result(second)

    assert memory.tool_results == [first, second]


def test_each_instance_has_its_own_tool_results_list() -> None:
    """Guards the classic mutable-default-argument bug — the dataclass's
    `field(default_factory=list)` must give each instance its own list,
    not one shared across every `ShortTermMemory()` call."""
    first = ShortTermMemory()
    second = ShortTermMemory()

    first.record_tool_result(
        ToolResult(
            tool_call_id="call_1",
            tool_name="get_wallet_balance",
            ok=True,
            data={},
            status=ToolInvocationStatus.OK,
            latency_ms=1,
        )
    )

    assert second.tool_results == []
