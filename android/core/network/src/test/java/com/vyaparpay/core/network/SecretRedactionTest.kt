package com.vyaparpay.core.network

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The bearer material in a connect bundle must never reach a log line.
 * `toString()` is the leak path — an f-string of a whole bundle in a crash
 * report or a debug log — so these tests pin the masking, mirroring the
 * backend's `repr=False` on the same fields (backend/app/api/routes/sessions.py).
 */
class SecretRedactionTest {

    private val bundle = ConnectBundleDto(
        sessionId = "a1f3c9",
        signalingUrl = "wss://voice.vyapar.local/v1/signal",
        signalingToken = "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        iceServers = listOf(
            IceServerDto(urls = listOf("stun:turn.vyapar.local:3478")),
            IceServerDto(
                urls = listOf("turn:turn.vyapar.local:3478?transport=udp"),
                username = "1784537062:a1f3c9",
                credential = "kWx0mB4vQ2nT8hZJc6yUq1RfLpE=",
            ),
        ),
        expires = "2026-07-24T14:19:22+05:30",
    )

    @Test
    fun `the signaling token never appears in the bundle's toString`() {
        val rendered = bundle.toString()

        assertFalse(rendered.contains("st_hV4nQ9pXzL2mR8cT0kWyB6uJ"))
        assertTrue("masked marker must show the field existed", rendered.contains("signalingToken=<redacted>"))
    }

    @Test
    fun `the TURN credential never appears in any toString, direct or nested`() {
        assertFalse(bundle.iceServers[1].toString().contains("kWx0mB4vQ2nT8hZJc6yUq1RfLpE="))
        // The nested render is the realistic leak: logging the whole bundle.
        assertFalse(bundle.toString().contains("kWx0mB4vQ2nT8hZJc6yUq1RfLpE="))
    }

    @Test
    fun `redaction keeps the log-safe fields readable`() {
        val rendered = bundle.toString()

        assertTrue(rendered.contains("a1f3c9"))
        assertTrue(rendered.contains("wss://voice.vyapar.local/v1/signal"))
        // The TURN username is deliberately unmasked — it is the attributable
        // half of the pair and appears in coturn logs by design (docs/13 §7).
        assertTrue(rendered.contains("1784537062:a1f3c9"))
    }

    @Test
    fun `a credential-free STUN entry renders its null without a fake mask`() {
        assertTrue(bundle.iceServers[0].toString().contains("credential=null"))
    }

    @Test
    fun `masking is display-only — the properties and the wire round-trip keep the real values`() {
        assertEquals("st_hV4nQ9pXzL2mR8cT0kWyB6uJ", bundle.signalingToken)
        assertEquals("kWx0mB4vQ2nT8hZJc6yUq1RfLpE=", bundle.iceServers[1].credential)

        // Re-encoding must still carry the real secret: the token's whole job
        // is to be sent once on the signaling WS connect (docs/13 §6.2).
        val encoded = Json.encodeToString(ConnectBundleDto.serializer(), bundle)
        assertTrue(encoded.contains("st_hV4nQ9pXzL2mR8cT0kWyB6uJ"))
    }

    @Test
    fun `a bundle decoded from the wire masks exactly like a constructed one`() {
        val decoded = Json { ignoreUnknownKeys = true }.decodeFromString(
            ConnectBundleDto.serializer(),
            Json.encodeToString(ConnectBundleDto.serializer(), bundle),
        )

        assertFalse(decoded.toString().contains("st_hV4nQ9pXzL2mR8cT0kWyB6uJ"))
        assertFalse(decoded.toString().contains("kWx0mB4vQ2nT8hZJc6yUq1RfLpE="))
    }
}
