package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [ScreenContextPublisher] against a [FakeContextChannel] capturing raw
 * bytes — snapshot-on-new-screen, delta-on-same-screen-changed, the one
 * shared monotonic `seq`, `base_seq` correctness, immediate (non-conflated)
 * event publishing, and capture exclusion holding at this layer. The highest
 * -value test decodes a produced frame's raw bytes back into JSON and
 * compares it, field-for-field, against `protocol/fixtures/context/
 * ctx_snapshot_payment_decline.json`'s `payload` — copied verbatim, matching
 * `SemanticSnapshotBuilderTest`'s own established convention (Android tests
 * have no wired-up path to read `protocol/` fixtures directly).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ScreenContextPublisherTest {

    private class FakeContextChannel : ContextChannel {
        val sent = mutableListOf<ByteArray>()
        override suspend fun send(bytes: ByteArray) {
            sent += bytes
        }
    }

    private fun decode(bytes: ByteArray): JsonObject =
        Json.parseToJsonElement(String(bytes, Charsets.UTF_8)).jsonObject

    /** docs/07 §3's canonical `PaymentScreen` decline IR — the same fixture `SemanticSnapshotBuilderTest` targets. */
    private fun paymentDeclineIr(
        dialogVisible: Boolean = true,
        lastActionTarget: String = "Pay Now",
        lastActionTs: Long = 1_784_536_440_000L,
    ): ScreenContextIr = ScreenContextIr(
        screen = "PaymentScreen",
        flow = "vendor_payment",
        components = listOf(
            ScreenComponent.AmountField(label = "Amount", value = "₹245"),
            ScreenComponent.Recipient(label = "To", value = "Amazon Business"),
            ScreenComponent.PrimaryCta(label = "Pay Now", enabled = true),
            ScreenComponent.Dialog(label = "Daily Limit Exceeded", visible = dialogVisible),
            ScreenComponent.Snackbar(label = "Payment Failed", visible = true),
        ),
        lastAction = LastAction(type = "tap", target = lastActionTarget, ts = lastActionTs),
        lastApi = LastApi(method = "POST", path = "/payments", status = 402, errorCode = "DAILY_LIMIT_EXCEEDED"),
        dirtyFields = emptyList(),
        loading = false,
    )

    private fun publisherUnderTest(
        state: MutableStateFlow<AppContextState>,
        events: MutableSharedFlow<AppEvent>,
        clock: () -> Long = { FIXED_TS },
    ): ScreenContextPublisher = ScreenContextPublisher(state, events, clock)

    // ------------------------------------------------------------------
    // Snapshot vs delta
    // ------------------------------------------------------------------

    @Test
    fun `a new screen publishes a full ctx snapshot`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()

        assertEquals(1, channel.sent.size)
        val envelope = decode(channel.sent.single())
        assertEquals(1L, envelope["v"]!!.jsonPrimitive.long)
        assertEquals("ctx.snapshot", envelope["type"]!!.jsonPrimitive.content)
        assertEquals(1L, envelope["seq"]!!.jsonPrimitive.long)
        assertEquals(FIXED_TS, envelope["ts"]!!.jsonPrimitive.long)
        val payload = envelope["payload"]!!.jsonObject
        assertEquals("PaymentScreen", payload["screen"]!!.jsonPrimitive.content)
    }

    @Test
    fun `a full snapshot's payload matches the protocol fixture's screen_context shape field-for-field`() = runTest {
        // protocol/fixtures/context/ctx_snapshot_payment_decline.json's
        // `payload`, copied verbatim.
        val expectedPayload = Json.parseToJsonElement(
            """
            {
              "v": "screen_context/v1",
              "screen": "PaymentScreen",
              "flow": "vendor_payment",
              "components": [
                {"role": "amount_field", "label": "Amount", "value": "₹245"},
                {"role": "recipient", "label": "To", "value": "Amazon Business"},
                {"role": "primary_cta", "label": "Pay Now", "enabled": true},
                {"role": "dialog", "label": "Daily Limit Exceeded", "visible": true},
                {"role": "snackbar", "label": "Payment Failed", "visible": true}
              ],
              "last_action": {"type": "tap", "target": "Pay Now", "ts": 1784536440000},
              "last_api": {"method": "POST", "path": "/payments", "status": 402,
                           "error_code": "DAILY_LIMIT_EXCEEDED"},
              "dirty_fields": [],
              "loading": false
            }
            """.trimIndent(),
        ).jsonObject

        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()

        val actualPayload = decode(channel.sent.single())["payload"]!!.jsonObject
        // JsonObject equality is Map equality -- field-for-field, order-independent
        // (SemanticSnapshotBuilderTest's own established comparison discipline).
        assertEquals(expectedPayload, actualPayload)
    }

    @Test
    fun `a changed component on the same screen publishes a delta whose base_seq points at the snapshot`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()
        val snapshotSeq = decode(channel.sent.single())["seq"]!!.jsonPrimitive.long

        state.value = AppContextState(
            route = "PaymentScreen",
            flow = "vendor_payment",
            screen = paymentDeclineIr(dialogVisible = false, lastActionTarget = "Dismiss", lastActionTs = 1_784_536_447_102L),
        )
        runCurrent()

        assertEquals(2, channel.sent.size)
        val deltaEnvelope = decode(channel.sent[1])
        assertEquals("ctx.delta", deltaEnvelope["type"]!!.jsonPrimitive.content)
        val payload = deltaEnvelope["payload"]!!.jsonObject
        assertEquals(snapshotSeq, payload["base_seq"]!!.jsonPrimitive.long)

        val changed = payload["changed"]!!.jsonArray
        assertEquals(1, changed.size)
        val dialogChange = changed.single().jsonObject
        assertEquals("dialog", dialogChange["role"]!!.jsonPrimitive.content)
        assertEquals(false, dialogChange["visible"]!!.jsonPrimitive.boolean)
        assertTrue(payload["removed"]!!.jsonArray.isEmpty())

        val lastAction = payload["last_action"]!!.jsonObject
        assertEquals("Dismiss", lastAction["target"]!!.jsonPrimitive.content)
    }

    @Test
    fun `an unchanged screen publishes nothing`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        val ir = paymentDeclineIr()
        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = ir)
        runCurrent()
        assertEquals(1, channel.sent.size)

        // Re-emitting the SAME (structurally equal) IR — no change at all.
        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = ir.copy())
        runCurrent()

        assertEquals(1, channel.sent.size)
    }

    // ------------------------------------------------------------------
    // One shared monotonic seq
    // ------------------------------------------------------------------

    @Test
    fun `one shared monotonic seq spans snapshot, delta, and event -- base_seq never points at an event`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        // seq 1: snapshot
        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()

        // seq 2: event -- shares the counter, does not become a base_seq anchor
        events.tryEmit(AppEvent.Tap(name = "Dismiss", ts = 1L, screen = "PaymentScreen"))
        runCurrent()

        // seq 3: delta
        state.value = AppContextState(
            route = "PaymentScreen",
            flow = "vendor_payment",
            screen = paymentDeclineIr(dialogVisible = false, lastActionTarget = "Dismiss", lastActionTs = 2L),
        )
        runCurrent()

        assertEquals(3, channel.sent.size)
        val decoded = channel.sent.map(::decode)
        assertEquals(listOf(1L, 2L, 3L), decoded.map { it["seq"]!!.jsonPrimitive.long })
        assertEquals(listOf("ctx.snapshot", "ctx.event", "ctx.delta"), decoded.map { it["type"]!!.jsonPrimitive.content })

        val deltaBaseSeq = decoded[2]["payload"]!!.jsonObject["base_seq"]!!.jsonPrimitive.long
        assertEquals(1L, deltaBaseSeq) // the snapshot's seq, NOT the event's (2)
    }

    // ------------------------------------------------------------------
    // Events bypass debounce and conflation
    // ------------------------------------------------------------------

    @Test
    fun `events publish immediately without waiting on the conflated screen-state flow`() = runTest {
        val state = MutableStateFlow(AppContextState()) // screen stays null for the whole test
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        events.tryEmit(AppEvent.Tap(name = "pay_now_cta", ts = 1L, screen = "PaymentScreen"))
        runCurrent()

        assertEquals(1, channel.sent.size)
        val envelope = decode(channel.sent.single())
        assertEquals("ctx.event", envelope["type"]!!.jsonPrimitive.content)
        assertEquals(1L, envelope["seq"]!!.jsonPrimitive.long)
        val payload = envelope["payload"]!!.jsonObject
        assertEquals("tap", payload["type"]!!.jsonPrimitive.content)
        assertEquals("pay_now_cta", payload["name"]!!.jsonPrimitive.content)
        assertEquals("PaymentScreen", payload["screen"]!!.jsonPrimitive.content)
    }

    // ------------------------------------------------------------------
    // Gap recovery: requestFullSnapshot (docs/08 §3.3, docs/13 §4)
    // ------------------------------------------------------------------

    @Test
    fun `requestFullSnapshot re-sends the current screen immediately, even though nothing changed`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        val publisher = publisherUnderTest(state, events)
        publisher.start(channel, backgroundScope)
        runCurrent()

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()
        assertEquals(1, channel.sent.size)

        // The reconnect case docs/06 §6 is built around: the merchant has not
        // gone anywhere, so the StateFlow will not re-emit on its own. Arming
        // a flag for "the next publish" would leave the server holding
        // discarded state indefinitely; recovery has to push.
        publisher.requestFullSnapshot()
        runCurrent()

        assertEquals(2, channel.sent.size)
        val recovery = decode(channel.sent[1])
        assertEquals("ctx.snapshot", recovery["type"]!!.jsonPrimitive.content)
        assertEquals(2L, recovery["seq"]!!.jsonPrimitive.long)
        assertEquals("PaymentScreen", recovery["payload"]!!.jsonObject["screen"]!!.jsonPrimitive.content)
    }

    @Test
    fun `a delta after recovery bases itself on the recovery snapshot, not the pre-gap message`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        val publisher = publisherUnderTest(state, events)
        publisher.start(channel, backgroundScope)
        runCurrent()

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()

        publisher.requestFullSnapshot()
        runCurrent()
        val recoverySeq = decode(channel.sent[1])["seq"]!!.jsonPrimitive.long

        state.value = AppContextState(
            route = "PaymentScreen",
            flow = "vendor_payment",
            screen = paymentDeclineIr(dialogVisible = false, lastActionTarget = "Dismiss", lastActionTs = 2L),
        )
        runCurrent()

        // The whole point of re-anchoring in sendSnapshot: pointing base_seq
        // at a message the server discarded would fabricate a screen state
        // that never existed (docs/08 §3.2).
        val delta = decode(channel.sent[2])
        assertEquals("ctx.delta", delta["type"]!!.jsonPrimitive.content)
        assertEquals(recoverySeq, delta["payload"]!!.jsonObject["base_seq"]!!.jsonPrimitive.long)
    }

    @Test
    fun `requestFullSnapshot before any capture sends nothing but forces the first publish to be a snapshot`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        val publisher = publisherUnderTest(state, events)
        publisher.start(channel, backgroundScope)
        runCurrent()

        publisher.requestFullSnapshot()
        runCurrent()
        assertTrue("there is no screen to capture yet", channel.sent.isEmpty())

        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr())
        runCurrent()

        assertEquals(1, channel.sent.size)
        assertEquals("ctx.snapshot", decode(channel.sent.single())["type"]!!.jsonPrimitive.content)
    }

    @Test
    fun `requestFullSnapshot before start is a no-op, not a crash`() = runTest {
        val state = MutableStateFlow(
            AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = paymentDeclineIr()),
        )
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)

        // A `ctx.request_snapshot` can only arrive on a channel this publisher
        // was bound to, so this is defensive rather than reachable — but the
        // rule is "degrade context, never conversation" (docs/08 §7), and an
        // NPE here would run on the peer's collector.
        publisherUnderTest(state, events).requestFullSnapshot()
    }

    // ------------------------------------------------------------------
    // Capture exclusion holds at this layer too (docs/07 §2.1)
    // ------------------------------------------------------------------

    @Test
    fun `capture exclusion -- an unchanged retained screen publishes nothing, but events still do`() = runTest {
        val state = MutableStateFlow(AppContextState())
        val events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)
        val channel = FakeContextChannel()
        publisherUnderTest(state, events).start(channel, backgroundScope)
        runCurrent()

        val operationalScreen = paymentDeclineIr()
        state.value = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = operationalScreen)
        runCurrent()
        assertEquals(1, channel.sent.size) // the initial snapshot

        // Simulates AppStateManager's own retention contract: route/flow move
        // to an excluded surface, `screen` stays the SAME retained IR.
        state.value = AppContextState(route = "HelpScreen", flow = "support", screen = operationalScreen)
        runCurrent()
        assertEquals(1, channel.sent.size) // nothing new published while "on" HelpScreen

        events.tryEmit(AppEvent.Nav(name = "HelpScreen", ts = 2L, from = "PaymentScreen"))
        runCurrent()

        assertEquals(2, channel.sent.size) // the nav event still publishes immediately
        assertEquals("ctx.event", decode(channel.sent[1])["type"]!!.jsonPrimitive.content)
    }

    private companion object {
        private const val FIXED_TS = 1_784_536_441_000L
    }
}
