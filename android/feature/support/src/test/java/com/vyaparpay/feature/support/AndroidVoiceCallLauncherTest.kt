package com.vyaparpay.feature.support

import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

/**
 * [AndroidVoiceCallLauncher]'s own glue-level guarantees, under Robolectric
 * (a real `Context` is needed for the intents and `unbindService`).
 *
 * The [AndroidVoiceCallLauncher.bindServiceDelegate] seam exists for this
 * file: Android offers no reliable way to make a same-APK `bindService` fail
 * on demand, and the MEDIUM review finding this covers — a `bindService`
 * refusal silently ignored, leaving a permanent "Connecting…" — lives exactly
 * on that branch. The delegate also hands the test the [ServiceConnection]
 * the launcher registered, which is how the framework-callback cases are
 * driven without a real service.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class AndroidVoiceCallLauncherTest {

    private val context: Context = RuntimeEnvironment.getApplication()

    @Test
    fun `a bindService refusal reports a controller-less binding synchronously`() {
        // MEDIUM-fix regression (independent review of T8c): the pre-review
        // code dropped bindService's Boolean on the floor, so this scenario
        // produced NO callback at all — this test fails against it with an
        // empty results list.
        val launcher = AndroidVoiceCallLauncher(context, bindServiceDelegate = { _, _, _ -> false })
        val results = mutableListOf<BoundCall?>()

        launcher.bind(onBound = { results += it }, onUnbound = {})

        assertEquals(listOf<BoundCall?>(null), results)
    }

    @Test
    fun `a bindService exception is folded to the same controller-less report`() {
        // SecurityException is the documented throw; the fold must not depend
        // on which one an OEM build actually raises.
        val launcher = AndroidVoiceCallLauncher(
            context,
            bindServiceDelegate = { _, _, _ -> throw SecurityException("not allowed") },
        )
        val results = mutableListOf<BoundCall?>()

        launcher.bind(onBound = { results += it }, onUnbound = {})

        assertEquals(listOf<BoundCall?>(null), results)
    }

    @Test
    fun `a failed bind releases the connection so a later attempt can register again`() {
        // Android keeps a connection registered even when bindService returns
        // false; a launcher that did not release it would refuse every retry
        // through its own double-bind guard — turning one transient failure
        // into a permanent one.
        var attempts = 0
        val launcher = AndroidVoiceCallLauncher(
            context,
            bindServiceDelegate = { _, _, _ ->
                attempts++
                false
            },
        )

        launcher.bind(onBound = {}, onUnbound = {})
        launcher.bind(onBound = {}, onUnbound = {})

        assertEquals(2, attempts)
    }

    @Test
    fun `a successful bind request reports nothing until the framework connects`() {
        val launcher = AndroidVoiceCallLauncher(context, bindServiceDelegate = { _, _, _ -> true })
        val results = mutableListOf<BoundCall?>()

        launcher.bind(onBound = { results += it }, onUnbound = {})

        assertTrue("onBound must wait for onServiceConnected", results.isEmpty())
    }

    @Test
    fun `while a bind is pending a second bind does not register another connection`() {
        var attempts = 0
        val launcher = AndroidVoiceCallLauncher(
            context,
            bindServiceDelegate = { _, _, _ ->
                attempts++
                true
            },
        )

        launcher.bind(onBound = {}, onUnbound = {})
        launcher.bind(onBound = {}, onUnbound = {})

        assertEquals(1, attempts)
    }

    @Test
    fun `a binder that is not the voice LocalBinder folds to a controller-less report`() {
        // The `as?` fold: a null (or foreign) IBinder must never crash the
        // connection callback — it becomes the same honest "no call here"
        // the caller already handles.
        var connection: ServiceConnection? = null
        val launcher = AndroidVoiceCallLauncher(
            context,
            bindServiceDelegate = { _, conn, _ ->
                connection = conn
                true
            },
        )
        val results = mutableListOf<BoundCall?>()
        launcher.bind(onBound = { results += it }, onUnbound = {})

        assertNotNull(connection)
        connection!!.onServiceConnected(ComponentName(context, "test"), null)

        assertEquals(listOf<BoundCall?>(null), results)
    }

    @Test
    fun `a service disconnect reaches onUnbound`() {
        var connection: ServiceConnection? = null
        val launcher = AndroidVoiceCallLauncher(
            context,
            bindServiceDelegate = { _, conn, _ ->
                connection = conn
                true
            },
        )
        var unbound = 0
        launcher.bind(onBound = {}, onUnbound = { unbound++ })

        connection!!.onServiceDisconnected(ComponentName(context, "test"))

        assertEquals(1, unbound)
    }

    @Test
    fun `unbind is idempotent`() {
        val launcher = AndroidVoiceCallLauncher(context, bindServiceDelegate = { _, _, _ -> true })
        launcher.bind(onBound = {}, onUnbound = {})

        launcher.unbind()
        launcher.unbind() // second release must be a no-op, not a throw
    }
}
