package com.vyaparpay.voice.signaling

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Envelope encode/decode against the docs/13 §6 example shapes — the same
 * frames the backend's `SignalingServer` validates on its side of the wire.
 */
class SignalWireTest {

    private val json = Json

    // ------------------------------------------------------------------
    // Encoding — client → server frames
    // ------------------------------------------------------------------

    @Test
    fun `offer encodes to the docs 13 §6 envelope shape`() {
        val encoded = SignalWire.encode(OutboundSignal.Offer("v=0\r\no=- 4611 2 IN IP4 127.0.0.1\r\ns=-\r\n"))

        val root = json.parseToJsonElement(encoded).jsonObject
        assertEquals(1, root.getValue("v").jsonPrimitive.content.toInt())
        assertEquals("offer", root.getValue("type").jsonPrimitive.content)
        assertEquals(
            "v=0\r\no=- 4611 2 IN IP4 127.0.0.1\r\ns=-\r\n",
            root.getValue("payload").jsonObject.getValue("sdp").jsonPrimitive.content,
        )
    }

    @Test
    fun `local ice encodes candidate, sdpMid and sdpMLineIndex`() {
        val candidate =
            "candidate:842163049 1 udp 1677729535 49.36.112.8 61042 typ srflx raddr 10.0.4.17 rport 50213 generation 0"
        val encoded = SignalWire.encode(
            OutboundSignal.LocalIce(candidate = candidate, sdpMid = "0", sdpMLineIndex = 0),
        )

        val root = json.parseToJsonElement(encoded).jsonObject
        assertEquals("ice", root.getValue("type").jsonPrimitive.content)
        val payload = root.getValue("payload").jsonObject
        assertEquals(candidate, payload.getValue("candidate").jsonPrimitive.content)
        assertEquals("0", payload.getValue("sdpMid").jsonPrimitive.content)
        assertEquals(0, payload.getValue("sdpMLineIndex").jsonPrimitive.content.toInt())
    }

    @Test
    fun `bye encodes the reason`() {
        val encoded = SignalWire.encode(OutboundSignal.Bye(BYE_REASON_USER_HANGUP))

        val root = json.parseToJsonElement(encoded).jsonObject
        assertEquals("bye", root.getValue("type").jsonPrimitive.content)
        assertEquals(
            "user_hangup",
            root.getValue("payload").jsonObject.getValue("reason").jsonPrimitive.content,
        )
    }

    @Test
    fun `ping encodes the ts payload of docs 13 §6`() {
        val encoded = SignalWire.encodePing(1784536470000L)

        val root = json.parseToJsonElement(encoded).jsonObject
        assertEquals(1, root.getValue("v").jsonPrimitive.content.toInt())
        assertEquals("ping", root.getValue("type").jsonPrimitive.content)
        assertEquals(
            1784536470000L,
            root.getValue("payload").jsonObject.getValue("ts").jsonPrimitive.content.toLong(),
        )
    }

    @Test
    fun `outbound frames round-trip through their own decoder`() {
        val ice = OutboundSignal.LocalIce("candidate:1 1 udp 1 10.0.0.2 50000 typ host", "0", 0)

        val decoded = SignalWire.decode(SignalWire.encode(ice))

        val frame = (decoded as SignalWire.Decoded.Frame).frame as SignalFrame.RemoteIce
        assertEquals(ice.candidate, frame.candidate)
        assertEquals(ice.sdpMid, frame.sdpMid)
        assertEquals(ice.sdpMLineIndex, frame.sdpMLineIndex)
    }

    // ------------------------------------------------------------------
    // Decoding — server → client frames, shapes from docs/13 §6
    // ------------------------------------------------------------------

    @Test
    fun `answer decodes to an Answer frame`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "answer", "payload": {"sdp": "v=0\r\no=- 3958984660 3958984660 IN IP4 0.0.0.0\r\ns=-\r\n"}}""",
        )

        val frame = (decoded as SignalWire.Decoded.Frame).frame
        assertEquals(
            SignalFrame.Answer("v=0\r\no=- 3958984660 3958984660 IN IP4 0.0.0.0\r\ns=-\r\n"),
            frame,
        )
    }

    @Test
    fun `trickled ice decodes with all three fields`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "ice", "payload": {
              "candidate": "candidate:842163049 1 udp 1677729535 49.36.112.8 61042 typ srflx raddr 10.0.4.17 rport 50213 generation 0",
              "sdpMid": "0", "sdpMLineIndex": 0}}""",
        )

        val frame = (decoded as SignalWire.Decoded.Frame).frame
        assertEquals(
            SignalFrame.RemoteIce(
                candidate = "candidate:842163049 1 udp 1677729535 49.36.112.8 61042 typ srflx raddr 10.0.4.17 rport 50213 generation 0",
                sdpMid = "0",
                sdpMLineIndex = 0,
            ),
            frame,
        )
    }

    @Test
    fun `the end-of-candidates marker decodes as a null-candidate RemoteIce`() {
        // Exactly what PeerSession sends after its answer
        // (backend/app/voice/peer_session.py judgment call 3).
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "ice", "payload": {"candidate": null, "sdpMid": null, "sdpMLineIndex": null}}""",
        )

        val frame = (decoded as SignalWire.Decoded.Frame).frame
        assertEquals(SignalFrame.RemoteIce(candidate = null, sdpMid = null, sdpMLineIndex = null), frame)
    }

    @Test
    fun `server bye decodes with its reason`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "bye", "payload": {"reason": "agent_hangup"}}""",
        )

        assertEquals(
            SignalFrame.Bye("agent_hangup"),
            (decoded as SignalWire.Decoded.Frame).frame,
        )
    }

    @Test
    fun `a bye with a junk payload still ends the call rather than being dropped`() {
        val decoded = SignalWire.decode("""{"v": 1, "type": "bye", "payload": {}}""")

        assertEquals(
            SignalFrame.Bye("unknown"),
            (decoded as SignalWire.Decoded.Frame).frame,
        )
    }

    @Test
    fun `error decodes with the docs 13 §1_1 code vocabulary`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "error", "payload": {"code": "AUTH_EXPIRED_TOKEN",
               "message": "Signaling token expired. Re-mint via POST /v1/sessions/{id}/token."}}""",
        )

        val frame = (decoded as SignalWire.Decoded.Frame).frame as SignalFrame.ServerError
        assertEquals("AUTH_EXPIRED_TOKEN", frame.code)
        assertTrue(frame.message.startsWith("Signaling token expired"))
    }

    @Test
    fun `pong decodes for the keepalive loop, not the frame stream`() {
        val decoded = SignalWire.decode("""{"v": 1, "type": "pong", "payload": {"ts": 1784536470000}}""")

        assertEquals(SignalWire.Decoded.Pong(1784536470000L), decoded)
    }

    @Test
    fun `a server ping is surfaced for a pong reply`() {
        val decoded = SignalWire.decode("""{"v": 1, "type": "ping", "payload": {"ts": 42}}""")

        assertEquals(SignalWire.Decoded.PingFromServer(42L), decoded)
    }

    // ------------------------------------------------------------------
    // Forward compatibility and malformed input (docs/13 §9)
    // ------------------------------------------------------------------

    @Test
    fun `unknown envelope types are ignored, never an error`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "transcript.hint", "payload": {"anything": true}}""",
        )

        assertTrue(decoded is SignalWire.Decoded.Ignored)
    }

    @Test
    fun `unknown payload fields on a known type are ignored`() {
        val decoded = SignalWire.decode(
            """{"v": 1, "type": "answer", "payload": {"sdp": "v=0", "future_field": 7}}""",
        )

        assertEquals(SignalFrame.Answer("v=0"), (decoded as SignalWire.Decoded.Frame).frame)
    }

    @Test
    fun `malformed JSON is ignored`() {
        assertTrue(SignalWire.decode("{not json") is SignalWire.Decoded.Ignored)
    }

    @Test
    fun `an envelope missing required keys is ignored`() {
        assertTrue(SignalWire.decode("""{"payload": {}}""") is SignalWire.Decoded.Ignored)
    }

    @Test
    fun `an unsupported envelope version is ignored`() {
        val decoded = SignalWire.decode("""{"v": 2, "type": "answer", "payload": {"sdp": "v=0"}}""")

        assertTrue(decoded is SignalWire.Decoded.Ignored)
    }

    @Test
    fun `an ice payload missing the candidate key entirely is malformed, not an end marker`() {
        // The null marker carries an explicit "candidate": null (docs/13 §6);
        // an absent key is a schema violation, same as the server's own check.
        val decoded = SignalWire.decode("""{"v": 1, "type": "ice", "payload": {"sdpMid": "0"}}""")

        assertTrue(decoded is SignalWire.Decoded.Ignored)
    }

    @Test
    fun `encoded local ice keeps explicit keys the server validates`() {
        // The server rejects ice payloads without candidate — make sure a
        // candidate with a null sdpMid still carries the key explicitly.
        val encoded = SignalWire.encode(
            OutboundSignal.LocalIce("candidate:1 1 udp 1 10.0.0.2 50000 typ host", null, 0),
        )

        val payload = json.parseToJsonElement(encoded).jsonObject.getValue("payload").jsonObject
        assertTrue("sdpMid key must be present", payload.containsKey("sdpMid"))
        assertEquals(JsonNull, payload.getValue("sdpMid"))
        assertEquals(0, payload.getValue("sdpMLineIndex").jsonPrimitive.content.toInt())
    }
}
