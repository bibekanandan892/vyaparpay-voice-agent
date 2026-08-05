package com.vyaparpay.feature.support

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.core.content.ContextCompat
import com.vyaparpay.voice.CallController
import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.service.VoiceCallService
import kotlinx.coroutines.flow.StateFlow

/**
 * The live call, as narrow as [CallViewModel] actually needs it.
 *
 * `CallController` is a big class with a session lifecycle, an effect loop and
 * a signaling/WebRTC surface; this ViewModel uses exactly three members of it.
 * Stating that in a type is not ceremony — it is what keeps this module from
 * quietly growing a second opinion about how a call is driven, and it is what
 * makes `CallViewModelTest` able to drive a terminal transition in one line
 * instead of simulating an entire SDP handshake to reach it. The transitions
 * themselves are already exhaustively covered by `CallStateMachineTest` and
 * `CallControllerTest` in `:voice`, where they belong; duplicating that here
 * would test `:voice` twice and this class's own policy not at all.
 */
internal interface BoundCall {
    /** The call's single source of truth (`CallController.state`). */
    val state: StateFlow<CallState>

    /** Context frames dropped so far — see `CallController.contextFramesDropped`. */
    val contextFramesDropped: Long

    /** The user tapped End. */
    fun hangUp()
}

/** Adapts the real [CallController] to [BoundCall]. */
internal class ControllerBoundCall(private val controller: CallController) : BoundCall {
    override val state: StateFlow<CallState> get() = controller.state
    override val contextFramesDropped: Long get() = controller.contextFramesDropped
    override fun hangUp(): Unit = controller.hangUp()
}

/**
 * Everything [CallViewModel] needs from the Android service framework, behind
 * one interface.
 *
 * This is the same split `:voice` already applies to itself — `VoiceCallService`
 * keeps only `Intent`/`Service` glue while `VoiceCallCoordinator` holds the
 * testable policy (see that service's kdoc, "confine the framework, test the
 * policy"). Here the glue is `startForegroundService`/`bindService`/
 * `unbindService`, and confining it to [AndroidVoiceCallLauncher] is what lets
 * `CallViewModelTest` assert the ordering and lifecycle guarantees that
 * actually matter — start-before-bind, exactly one unbind, no unbind without a
 * bind — on a plain JVM, against a fake, with no Robolectric service
 * controller in the way.
 */
internal interface VoiceCallLauncher {

    /**
     * Starts [VoiceCallService] with `ACTION_START` and the JSON-encoded
     * `SessionCreateRequestDto` the caller built from `AppStateManager`.
     */
    fun start(sessionRequestJson: String)

    /**
     * Binds to the service, creating it if need be.
     *
     * @param onBound receives the bound call, or `null` — **nullable by
     *   design**: a connected binding with no controller is a service that
     *   has not (or not yet, or never will have) processed a usable start
     *   intent; `CallViewModel.handleControllerlessBinding` owns the
     *   disambiguation. **Contract addition (MEDIUM fix, independent review
     *   of T8c): if the binding cannot even be requested — `bindService`
     *   returns `false` or throws — the implementation reports
     *   `onBound(null)` synchronously and releases itself**, so a bind
     *   failure lands in the caller's existing controller-less handling
     *   instead of leaving the UI on a spinner no callback will ever resolve.
     * @param onUnbound the service process died (`onServiceDisconnected`).
     */
    fun bind(onBound: (BoundCall?) -> Unit, onUnbound: () -> Unit)

    /** Releases the binding. Idempotent — safe to call when not bound. */
    fun unbind()
}

/**
 * The real [VoiceCallLauncher].
 *
 * Holds the application context (never an `Activity`): the binding must
 * outlive a rotation, and the merchant may leave `HelpScreen` while the call
 * runs — the call belongs to the service, not to a screen.
 */
internal class AndroidVoiceCallLauncher(
    private val context: Context,
    /**
     * The one `bindService` call, injectable so `AndroidVoiceCallLauncherTest`
     * can drive the refusal/throw branches on a plain Robolectric context —
     * Android offers no reliable way to make a same-APK `bindService` fail on
     * demand. Production always uses the default.
     */
    private val bindServiceDelegate: (Intent, ServiceConnection, Int) -> Boolean = { intent, connection, flags ->
        context.bindService(intent, connection, flags)
    },
) : VoiceCallLauncher {

    /**
     * Non-null exactly while a [ServiceConnection] is registered with the
     * framework. This field *is* the leak guard: [unbind] keys off it, and
     * [bind] refuses to register a second connection while one is live —
     * without that, a double `bind()` would strand the first connection with
     * no handle to release it, and `unbindService` is the only thing that ever
     * releases one.
     */
    private var connection: ServiceConnection? = null

    override fun start(sessionRequestJson: String) {
        val intent = Intent(context, VoiceCallService::class.java).apply {
            action = VoiceCallService.ACTION_START
            putExtra(VoiceCallService.EXTRA_SESSION_REQUEST_JSON, sessionRequestJson)
        }
        // startForegroundService, not startService: VoiceCallService is a
        // microphone-typed FGS and calls startForeground() synchronously in
        // handleStart(). This is only ever reached from a permission-result
        // callback while the merchant is looking at HelpScreen — i.e. from a
        // guaranteed-foreground context with RECORD_AUDIO already granted,
        // which is exactly the window docs/03 §3.3 requires an FGS start to
        // happen in.
        ContextCompat.startForegroundService(context, intent)
    }

    override fun bind(onBound: (BoundCall?) -> Unit, onUnbound: () -> Unit) {
        if (connection != null) return

        val newConnection = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                val controller = (binder as? VoiceCallService.LocalBinder)?.callController
                onBound(controller?.let(::ControllerBoundCall))
            }

            override fun onServiceDisconnected(name: ComponentName?) {
                onUnbound()
            }
        }

        // Assigned BEFORE the bindService call, and kept even if it returns
        // false or throws. Android's contract is that a connection passed to
        // bindService stays registered regardless of the return value — a
        // `false` means "the service could not be found", not "nothing was
        // registered" — so the only way to not leak it is to unbind it anyway.
        // Dropping it on the false branch is the textbook version of this bug.
        connection = newConnection

        // BIND_AUTO_CREATE, paired with the null-call handling in
        // CallViewModel.onBound. Without it a bind that lands before the
        // service is created never connects at all, leaving the UI on a
        // spinner with no call and no error — a silent hang. With it the
        // connection is guaranteed to arrive; if the start intent failed, what
        // arrives is a controller-less binder, which CallViewModel surfaces as
        // an honest failed call. The service-lifetime cost (a bound service is
        // not destroyed by its own stopSelf until every client unbinds) is
        // paid off by CallViewModel unbinding on the terminal state.
        val requested = runCatching {
            bindServiceDelegate(
                Intent(context, VoiceCallService::class.java),
                newConnection,
                Context.BIND_AUTO_CREATE,
            )
        }.getOrDefault(false)

        if (!requested) {
            // MEDIUM fix (independent review of T8c): the old code dropped
            // this result on the floor, and a bind that never connects is a
            // permanent "Connecting…" — no callback ever resolves it, and a
            // queued pending hang-up is never delivered. Failing over to the
            // caller's controller-less handling turns it into an honest
            // failure instead. unbind() first because Android keeps a
            // connection registered even when bindService returns false — the
            // documented contract is that the caller must still release it.
            unbind()
            onBound(null)
        }
    }

    override fun unbind() {
        val current = connection ?: return
        connection = null
        // Swallowed: unbindService throws IllegalArgumentException if the
        // framework already tore the connection down (process death races
        // onCleared). Nothing is left to release in that case, which is the
        // outcome this call wanted anyway.
        runCatching { context.unbindService(current) }
    }
}
