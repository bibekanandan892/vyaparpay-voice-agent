package com.vyaparpay.voice.context

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [ContextDownlink] against the four server → client types docs/13 §4 defines
 * plus every malformed shape that can physically arrive on a data channel.
 * The recurring assertion is the same one twice over: recognise
 * `ctx.request_snapshot`, and never throw on anything else — a parse crash
 * here runs on the peer's collector and would end a live call over a stray
 * frame.
 */
class ContextDownlinkTest {

    private fun classify(json: String): ContextDownlinkFrame =
        ContextDownlink.classify(json.toByteArray(Charsets.UTF_8))

    @Test
    fun `the docs 13 §4 request_snapshot example is recognised`() {
        // Verbatim from docs/13 §4's example block.
        val frame = """{"v": 1, "type": "ctx.request_snapshot", "seq": 7, "ts": 1784536455900,
                        "payload": {"last_good_seq": 44}}"""

        assertEquals(ContextDownlinkFrame.REQUEST_SNAPSHOT, classify(frame))
    }

    @Test
    fun `a request_snapshot with no payload at all is still recognised`() {
        // last_good_seq is information this client cannot act on (it never
        // gap-checks the downlink, docs/08 §3.2), so its absence must not
        // cost a recovery round trip.
        assertEquals(ContextDownlinkFrame.REQUEST_SNAPSHOT, classify("""{"type":"ctx.request_snapshot"}"""))
    }

    @Test
    fun `the overlay's own frame types are ignored without erroring`() {
        // Not "unknown" — transcript.*/agent.state belong to the later
        // ConversationOverlay task (docs/03 §3.12). Ignoring them here is the
        // seam, deliberately.
        for (type in listOf("transcript.partial", "transcript.final", "agent.state")) {
            assertEquals(
                "$type must be ignored, not mistaken for a recovery request",
                ContextDownlinkFrame.IGNORED,
                classify("""{"v":1,"type":"$type","seq":11,"ts":1,"payload":{"text":"hi"}}"""),
            )
        }
    }

    @Test
    fun `a future unknown type folds to ignored - docs 13 §9's additive-within-v1 posture`() {
        assertEquals(ContextDownlinkFrame.IGNORED, classify("""{"v":1,"type":"agent.thinking_hard"}"""))
    }

    @Test
    fun `malformed frames are ignored quietly, never thrown`() {
        val malformed = listOf(
            "" to "empty payload",
            "   " to "whitespace only",
            "not json at all" to "not JSON",
            """{"type":"ctx.request_snapshot"""" to "truncated mid-object",
            """["ctx.request_snapshot"]""" to "valid JSON, but an array not an object",
            """"ctx.request_snapshot"""" to "valid JSON, but a bare string",
            """{"seq":7}""" to "object with no type field",
            """{"type":{"nested":"object"}}""" to "type present but not a primitive",
            """{"type":42}""" to "type present but numeric",
            """{"type":null}""" to "type explicitly null",
        )

        for ((frame, description) in malformed) {
            assertEquals(description, ContextDownlinkFrame.IGNORED, classify(frame))
        }
    }

    @Test
    fun `non-UTF8 bytes are ignored rather than crashing the decoder`() {
        val garbage = byteArrayOf(0xC3.toByte(), 0x28, 0xA0.toByte(), 0xA1.toByte())

        assertEquals(ContextDownlinkFrame.IGNORED, ContextDownlink.classify(garbage))
    }

    @Test
    fun `the recognised type string matches the backend's own constant`() {
        // backend/app/voice/context_dispatch.py:
        //   DC_TYPE_CTX_REQUEST_SNAPSHOT: Final = "ctx.request_snapshot"
        assertEquals("ctx.request_snapshot", ContextDownlink.TYPE_REQUEST_SNAPSHOT)
    }
}
