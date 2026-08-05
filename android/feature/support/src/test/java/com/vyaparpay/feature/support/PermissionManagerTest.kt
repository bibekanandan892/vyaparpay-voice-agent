package com.vyaparpay.feature.support

import android.os.Build
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every branch of docs/03 §3.6's permission policy, on a plain JVM.
 *
 * [PermissionManager] is framework-free precisely so this file can exist: the
 * grant/deny/permanently-denied fork is the part that decides whether a
 * merchant can reach support at all, and it should not need an emulator to
 * prove.
 */
class PermissionManagerTest {

    // ---------------------------------------------------------------
    // requiredPermissions — the API 33 boundary
    // ---------------------------------------------------------------

    @Test
    fun `below API 33 only RECORD_AUDIO is requested`() {
        // POST_NOTIFICATIONS does not exist as a runtime permission before
        // Tiramisu; requesting it anyway is what produces phantom denials.
        val required = PermissionManager.requiredPermissions(sdkInt = Build.VERSION_CODES.S_V2)

        assertEquals(listOf(PermissionManager.RECORD_AUDIO), required.toList())
    }

    @Test
    fun `from API 33 both permissions are requested`() {
        val required = PermissionManager.requiredPermissions(sdkInt = Build.VERSION_CODES.TIRAMISU)

        assertEquals(
            listOf(PermissionManager.RECORD_AUDIO, PermissionManager.POST_NOTIFICATIONS),
            required.toList(),
        )
    }

    @Test
    fun `the minSdk floor still asks for the microphone`() {
        // minSdk is 24 (gradle/libs.versions.toml). A regression that made the
        // list empty below some threshold would silently produce calls with no
        // mic prompt at all on the oldest supported devices.
        val required = PermissionManager.requiredPermissions(sdkInt = 24)

        assertTrue(PermissionManager.RECORD_AUDIO in required)
    }

    // ---------------------------------------------------------------
    // evaluate — the three outcomes
    // ---------------------------------------------------------------

    @Test
    fun `microphone granted proceeds to the call`() {
        val decision = PermissionManager.evaluate(
            grants = mapOf(PermissionManager.RECORD_AUDIO to true),
            canPromptAgain = { error("must not be consulted once the permission is granted") },
        )

        assertEquals(CallPermissionOutcome.PROCEED, decision.outcome)
    }

    @Test
    fun `microphone denied while the system will still prompt is a retryable denial`() {
        val decision = PermissionManager.evaluate(
            grants = mapOf(PermissionManager.RECORD_AUDIO to false),
            canPromptAgain = { true },
        )

        assertEquals(CallPermissionOutcome.RETRYABLE_DENIAL, decision.outcome)
    }

    @Test
    fun `microphone denied with no further prompt is a permanent denial`() {
        // shouldShowRequestPermissionRationale == false *after* a denial is
        // Android's only signal for "don't ask again" — see PermissionManager's
        // kdoc. This is the branch that must route to app settings rather than
        // to a button that silently does nothing.
        val decision = PermissionManager.evaluate(
            grants = mapOf(PermissionManager.RECORD_AUDIO to false),
            canPromptAgain = { false },
        )

        assertEquals(CallPermissionOutcome.PERMANENT_DENIAL, decision.outcome)
    }

    @Test
    fun `the rationale probe is asked about the microphone specifically`() {
        // A regression that probed POST_NOTIFICATIONS (or a hardcoded string)
        // would classify the mic denial off the wrong permission's state.
        val probed = mutableListOf<String>()

        PermissionManager.evaluate(
            grants = mapOf(
                PermissionManager.RECORD_AUDIO to false,
                PermissionManager.POST_NOTIFICATIONS to false,
            ),
            canPromptAgain = { permission ->
                probed += permission
                true
            },
        )

        assertEquals(listOf(PermissionManager.RECORD_AUDIO), probed)
    }

    @Test
    fun `a result missing RECORD_AUDIO entirely is treated as a denial, never as a grant`() {
        // A cancelled or malformed result must fail closed. Defaulting the
        // other way would start a microphone foreground service on a
        // permission we never actually got.
        val decision = PermissionManager.evaluate(
            grants = emptyMap(),
            canPromptAgain = { true },
        )

        assertEquals(CallPermissionOutcome.RETRYABLE_DENIAL, decision.outcome)
    }

    // ---------------------------------------------------------------
    // POST_NOTIFICATIONS — soft, never fatal
    // ---------------------------------------------------------------

    @Test
    fun `notifications denied still proceeds when the microphone is granted`() {
        // :voice's own manifest states the rule: "denial degrades to a
        // headless call, never blocks it." This is that rule, asserted.
        val decision = PermissionManager.evaluate(
            grants = mapOf(
                PermissionManager.RECORD_AUDIO to true,
                PermissionManager.POST_NOTIFICATIONS to false,
            ),
            canPromptAgain = { error("must not be consulted once the microphone is granted") },
        )

        assertEquals(CallPermissionOutcome.PROCEED, decision.outcome)
        assertFalse(decision.notificationsGranted)
    }

    @Test
    fun `notifications granted is reported as granted`() {
        val decision = PermissionManager.evaluate(
            grants = mapOf(
                PermissionManager.RECORD_AUDIO to true,
                PermissionManager.POST_NOTIFICATIONS to true,
            ),
            canPromptAgain = { false },
        )

        assertTrue(decision.notificationsGranted)
    }

    @Test
    fun `notifications absent from the result counts as granted`() {
        // Below API 33 the permission is never requested, so it never appears
        // in the result map — and notifications work fine there. Defaulting to
        // "denied" would show every pre-Tiramisu merchant a warning about a
        // restriction that does not apply to them.
        val decision = PermissionManager.evaluate(
            grants = mapOf(PermissionManager.RECORD_AUDIO to true),
            canPromptAgain = { false },
        )

        assertTrue(decision.notificationsGranted)
    }
}
