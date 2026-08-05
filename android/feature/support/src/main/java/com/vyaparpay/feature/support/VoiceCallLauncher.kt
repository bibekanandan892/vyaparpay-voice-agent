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
 * `CallViewModelTest` assert the lifecycle guarantees that actually matter —
 * start-before-bind within a start attempt, exactly one unbind, no unbind
 * without a bind — on a plain JVM, against a fake, with no Robolectric
 * service controller in the way. `FakeVoiceCallLauncher` keeps a single
 * ordered `callLog` across both methods so start-before-bind is a real
 * assertion rather than a claim (LOW-1, second independent review of T8c:
 * the fake previously tracked the two in separate, unordered fields, which
 * made that guarantee structurally unassertable).
 */
internal interface VoiceCallLauncher {

    /**
     * Starts [VoiceCallService] with `ACTION_START` and the JSON-encoded
     * `SessionCreateRequestDto` the caller built from `AppStateManager`.
     *
     * @return `false` if the platform refused the start — the caller must
     *   treat that as a failed call and **must not** go on to bind, since
     *   there is no service coming to bind to. See [AndroidVoiceCallLauncher.start]
     *   for why a foreground-service start is genuinely refusable here.
     */
    fun start(sessionRequestJson: String): Boolean

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
    /**
     * The one `unbindService` call. Injectable for the same reason as
     * [bindServiceDelegate], plus one of its own: [unbind] deliberately
     * swallows through `runCatching`, so without this seam a test could only
     * observe that the internal handle was cleared — never that the framework
     * was actually told to release the connection. That distinction *is* the
     * leak (LOW-5, second independent review of T8c).
     */
    private val unbindServiceDelegate: (ServiceConnection) -> Unit = { connection ->
        context.unbindService(connection)
    },
    /**
     * The one `startForegroundService` call, injectable so the
     * platform-refusal branch documented on [start] is reachable in a test —
     * Robolectric will not throw `ForegroundServiceStartNotAllowedException`
     * on demand.
     */
    private val startServiceDelegate: (Intent) -> Unit = { intent ->
        ContextCompat.startForegroundService(context, intent)
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

    override fun start(sessionRequestJson: String): Boolean {
        val intent = Intent(context, VoiceCallService::class.java).apply {
            action = VoiceCallService.ACTION_START
            putExtra(VoiceCallService.EXTRA_SESSION_REQUEST_JSON, sessionRequestJson)
        }

        // startForegroundService, not startService: VoiceCallService is a
        // microphone-typed FGS and calls startForeground() synchronously in
        // handleStart() (docs/03 §3.3).
        //
        // **HIGH fix (second independent review of T8c) — this start is
        // genuinely refusable, and the previous comment here claimed
        // otherwise.** It said this is "only ever reached from a
        // guaranteed-foreground context". That is false, and the falsifier is
        // the permission plumbing itself: when RECORD_AUDIO is already granted
        // (every call after the first), `RequestMultiplePermissions`
        // short-circuits through `getSynchronousResult`, and
        // `ActivityResultRegistry.launch` still dispatches that result via
        // `Handler(Looper.getMainLooper()).post {}` — asynchronously. A
        // merchant who taps Call Support and presses Home before that post
        // drains lands us here with the process already backgrounded, which
        // is precisely the state API 31+ answers with
        // ForegroundServiceStartNotAllowedException (an IllegalStateException;
        // API 26-30 throw the plain form).
        //
        // Uncaught, that exception unwinds through the ActivityResult dispatch
        // and takes down the HOST APP — the same failure class
        // VoiceCallService's own handleStart already carries two HIGH fixes
        // for ("crashes the *host app*, not just this service"). Catching
        // Throwable rather than Exception for the same reason that file gives:
        // a platform/init failure surfaces as an Error at least as often.
        // Returning false routes this into the caller's existing failed-call
        // handling instead.
        return runCatching { startServiceDelegate(intent) }.isSuccess
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
        runCatching { unbindServiceDelegate(current) }
    }
}
