package com.vyaparpay.voice.service

import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.os.Binder
import android.os.IBinder
import androidx.core.app.ServiceCompat
import androidx.lifecycle.LifecycleService
import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.network.SessionEndDto
import com.vyaparpay.core.network.SessionSummaryDto
import com.vyaparpay.core.network.VyaparApi
import com.vyaparpay.core.network.VyaparApiFactory
import com.vyaparpay.core.screencontext.ScreenContextPublisher
import com.vyaparpay.voice.CallController
import com.vyaparpay.voice.audio.AndroidCallAudioSession
import com.vyaparpay.voice.context.ScreenContextPublisherBinding
import com.vyaparpay.voice.notification.AndroidCallNotifier
import com.vyaparpay.voice.signaling.OkHttpSignalingClient
import com.vyaparpay.voice.webrtc.WebRtcClientFactory
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import javax.inject.Provider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient

/**
 * The foreground service that *is* the call (docs/03 §3.3). It hosts a
 * call-scoped [CallController] and lets [VoiceCallCoordinator] — the testable
 * policy — decide when the service is in the foreground, when audio focus is
 * held, what the notification says, and when to stop.
 *
 * **The invariant this class exists to enforce:** the service cannot outlive
 * a terminal `CallState` — a leaked service is a live mic (docs/03 §2.2). It
 * only ever stops itself through [VoiceCallCoordinator]'s `onCallEnded`
 * callback (or the null-intent restart path below), so "the service is
 * running" and "the call is non-terminal" stay the same fact.
 *
 * **Why so little of this class is unit tested.** Everything that is *policy*
 * — focus ordering, notification lifecycle, stop-on-terminal — lives in
 * [VoiceCallCoordinator] and is exhaustively covered on a plain JVM. What is
 * left here is Android glue: `Service` lifecycle callbacks, `AudioManager`/
 * `NotificationManager` construction, and `Intent` plumbing — the same
 * "confine the framework, test the policy" split `:voice` already applies to
 * `PeerConnectionWebRtcClient` (untested directly) versus `CallController`
 * (extensively tested).
 */
@AndroidEntryPoint
public class VoiceCallService : LifecycleService() {

    /**
     * A `Provider`, not a plain injected instance: one fresh
     * [ScreenContextPublisher] per call.
     *
     * The publisher is unscoped in the graph, so a `Provider.get()` per
     * [handleStart] hands each call its own `seq` counter starting at
     * `GENESIS_SEQ + 1` — which is exactly the anchor the backend's
     * `SnapshotIngestor` seeds per session (see [ScreenContextPublisher]'s own
     * kdoc). Sharing one publisher across calls would open call two with a
     * `seq` the server reads as a gap on the very first message, triggering a
     * `ctx.request_snapshot` round trip before the conversation starts. Its
     * `AppStateManager` dependency *is* a `@Singleton` and is correctly
     * shared — the timeline must survive between calls; only the wire-level
     * counter must not.
     */
    @Inject
    public lateinit var screenContextPublishers: Provider<ScreenContextPublisher>

    /**
     * The `:core:network`-provided client (DemoAuthInterceptor +
     * ApiErrorReportingInterceptor), Hilt-injected since this is an
     * `@AndroidEntryPoint` service — used for REST only. Found missing
     * while placing the first real device call: `buildApi()` previously
     * built [VyaparApi] on the companion's bare `sharedOkHttp`, which
     * carries no interceptors at all, so every session-create request
     * went out with no Authorization header and 401'd. Deliberately a
     * SEPARATE client from `sharedOkHttp` rather than folding auth into
     * it: `sharedOkHttp` also opens the signaling WebSocket, and
     * ApiErrorReportingInterceptor's `!response.isSuccessful` check
     * treats a 101 Switching Protocols upgrade as a failure — reusing
     * one client for both would misfire that interceptor on every
     * successful signaling connect.
     */
    @Inject
    public lateinit var restOkHttpClient: OkHttpClient

    // A single confined worker, not the raw Default pool: CallController's
    // own event loop, VoiceCallCoordinator's two collectors, and a mute
    // toggle arriving on the main thread via ACTION_MUTE all share this
    // scope, and VoiceCallCoordinator's threading contract (its own KDoc)
    // requires exactly one confined executor underneath it so those never
    // interleave mid-decision (code-reviewer finding: a per-field @Volatile
    // was not enough — this is the actual fix).
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Default.limitedParallelism(1))

    private lateinit var notifier: AndroidCallNotifier

    /** The exact instance that acquired focus for the live call — [onDestroy] must release *this* one, not a fresh one. */
    private var audioSession: AndroidCallAudioSession? = null

    /** Non-null once a call has started — the seam a future `:feature:support` binder client observes. */
    private var controller: CallController? = null
    private var coordinator: VoiceCallCoordinator? = null

    private val binder = LocalBinder()

    override fun onCreate() {
        super.onCreate()
        // Constructing it here (rather than per-call) both creates the
        // notification channel exactly once and gives promoteToForeground a
        // ready Notification the instant the first call starts.
        notifier = AndroidCallNotifier(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        if (intent == null) {
            // §7's OS-restart-with-null-intent case: the process (and with it
            // the PeerConnection) is gone, so there is no live call this
            // instance can be driving. Full grace-window reconnection is the
            // not-yet-built docs/03 §3.5 task (CallStateMachine's own KDoc
            // says as much); until it lands, the honest behaviour — "end the
            // call honestly, never fake a live one" (docs/03 §7) — is to not
            // resurrect a phantom call and stop cleanly rather than sit as a
            // silent leaked service.
            stopSelf()
            return START_NOT_STICKY
        }

        when (intent.action) {
            ACTION_START -> handleStart(intent)
            ACTION_MUTE -> coordinator?.toggleMute()
            ACTION_END -> controller?.hangUp()
            else -> {
                // Review fix (MEDIUM): an unrecognized/absent action on a
                // fresh instance (no ACTION_START yet reached `controller !=
                // null`) is the same "started but never foregrounded" hazard
                // the malformed-payload branch in handleStart() exists to
                // close — this closes the other way to reach it: the wrong
                // action instead of a bad extra.
                if (controller == null) stopSelf()
            }
        }

        // A real in-progress call: the OS should recreate this service (with
        // a null intent, handled above) if it is killed under memory
        // pressure, rather than silently drop the call notification.
        return START_STICKY
    }

    private fun handleStart(intent: Intent) {
        if (controller != null) return // A call is already in flight; ignore a stray second START — and definitely do not stop it.

        val request = decodeSessionRequest(intent)
        if (request == null) {
            // A malformed or missing start payload: there is nothing to
            // serve. A mic-type FGS must call startForeground() promptly
            // after a foreground-context start — sitting started-but-never-
            // foregrounded risks ForegroundServiceDidNotStartInTimeException
            // on API 31+ (which crashes the *host app*, not just this
            // service) and is a leaked idle service on older ones either way.
            stopSelf()
            return
        }

        // HIGH fix (independent review of c767074): promote to foreground
        // synchronously here, before ANY of the construction below runs.
        // WebRtcClientFactory.create() does real synchronous native work
        // (System.loadLibrary / PeerConnectionFactory.initialize), and the
        // previous code only reached startForeground() indirectly — through
        // VoiceCallCoordinator observing the Requesting state on a
        // background dispatcher, itself reachable only after that native
        // work plus a channel hop through CallController's event loop. On
        // API 31+ a mic-type FGS that doesn't call startForeground()
        // promptly after startForegroundService() crashes the *host app*
        // (ForegroundServiceDidNotStartInTimeException); notifier.initial()
        // has zero dependency on CallState, so there is no reason to make
        // that race depend on state-machine timing. VoiceCallCoordinator's
        // own onForegroundServiceRequired callback still fires later —
        // guarded by its `everForegrounded` flag, so the resulting second
        // ServiceCompat.startForeground() call below is a harmless
        // notification refresh — and that later call is still what keeps
        // audio-focus acquisition correctly ordered before
        // WebRtcClient.start() opens the mic.
        promoteToForeground()

        val session: AndroidCallAudioSession
        val newController: CallController
        val newCoordinator: VoiceCallCoordinator
        try {
            session = AndroidCallAudioSession(getSystemService(AudioManager::class.java))
            newController = CallController(
                api = buildApi(),
                signaling = OkHttpSignalingClient(sharedOkHttp, serviceScope),
                webRtc = WebRtcClientFactory.create(applicationContext),
                scope = serviceScope,
                // Bound to the `ctx` channel on the Connecting -> InCall
                // transition and torn down with the call — see
                // CallController.bindContextPublisher()'s kdoc.
                contextPublisher = resolveContextPublisher(),
            )
            newCoordinator = VoiceCallCoordinator(
                callState = newController.state,
                audioSession = session,
                notifier = notifier,
                scope = serviceScope,
                setMuted = newController::setMuted,
                hangUp = newController::hangUp,
                onForegroundServiceRequired = ::promoteToForeground,
                onCallEnded = ::stopSelf,
            )
        } catch (error: Throwable) {
            // HIGH fix (independent review of c767074): WebRtcClientFactory
            // .create() does real native init that can throw — e.g. an
            // UnsatisfiedLinkError on a packaging/ABI problem, which is an
            // Error, not an Exception, hence catching Throwable rather than
            // Exception. We are already foreground at this point
            // (promoteToForeground() above already ran), so letting this
            // propagate would crash the process with a live foreground
            // notification and nothing behind it — the same "started but
            // broken" hazard the malformed-payload branch above exists to
            // avoid. No CallController/VoiceCallCoordinator field was
            // assigned yet, so there is nothing to release beyond the
            // notification itself.
            notifier.clear()
            stopSelf()
            return
        }

        audioSession = session
        controller = newController
        coordinator = newCoordinator

        // start() before startCall(): the coordinator must be collecting
        // before the first event fires so the Idle -> Requesting edge is
        // observed, not missed (VoiceCallCoordinator's own contract note).
        newCoordinator.start()
        newController.startCall(request)
    }

    /**
     * The capture pipeline, or `null` if the graph cannot produce one.
     *
     * Deliberately its own `runCatching` rather than sharing [handleStart]'s
     * surrounding try/catch, because the two failures deserve opposite
     * outcomes. A failed `WebRtcClientFactory.create()` means there is no call
     * to have — the block below ends the service. A failed capture graph
     * (`Provider.get()` blowing up in a `@Singleton` constructor deep under
     * `AppStateManager`, or `screenContextPublishers` never injected at all)
     * means only that the agent will be blind: docs/08 §7's "degrade context,
     * never conversation" says the merchant still gets to talk to support.
     * `CallController` takes a nullable publisher for exactly this case, so
     * this is a supported state, not a swallowed error.
     *
     * `Throwable`, matching [handleStart]'s own reasoning: an `@Inject` graph
     * failure surfaces as an `Error` (`ExceptionInInitializerError`,
     * `UninitializedPropertyAccessException`) at least as often as an
     * `Exception`, and neither is worth a dead call.
     */
    private fun resolveContextPublisher(): ScreenContextPublisherBinding? =
        try {
            ScreenContextPublisherBinding(screenContextPublishers.get())
        } catch (_: Throwable) {
            null
        }

    private fun decodeSessionRequest(intent: Intent): SessionCreateRequestDto? {
        val json = intent.getStringExtra(EXTRA_SESSION_REQUEST_JSON) ?: return null
        return runCatching { Json.decodeFromString(SessionCreateRequestDto.serializer(), json) }.getOrNull()
    }

    @Suppress("InlinedApi") // The FOREGROUND_SERVICE_TYPE_* fields are plain inlined ints, safe to reference on any minSdk; ServiceCompat.startForeground ignores `type` below the API level it applies to.
    private fun promoteToForeground() {
        val notification = notifier.initial()
        // docs/03 §3.3: microphone for the uplink, mediaPlayback for Asha's
        // downlink. Android 14+ additionally requires
        // FOREGROUND_SERVICE_MICROPHONE in the manifest for the microphone
        // half — declared alongside this service. No SDK_INT branch needed
        // here: ServiceCompat.startForeground is the AndroidX shim built
        // exactly for this — it drops the type argument itself on API levels
        // that predate foreground service types.
        val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
        ServiceCompat.startForeground(this, AndroidCallNotifier.NOTIFICATION_ID, notification, type)
    }

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    override fun onDestroy() {
        // Defensive, not load-bearing: VoiceCallCoordinator already releases
        // focus and clears the notification on the Ended transition. This
        // covers the case the process is torn down before that transition is
        // observed — a leaked AudioFocusRequest is exactly the "live mic"
        // failure mode docs/03 §2.2 calls out, so releasing the *same*
        // instance again is the cheap side to err on (release() is
        // idempotent — see AndroidCallAudioSession).
        audioSession?.release()
        serviceScope.cancel()
        super.onDestroy()
    }

    private fun buildApi(): VyaparApi {
        val baseUrl = resolveConfiguredBaseUrl() ?: return UnconfiguredVyaparApi
        return runCatching {
            VyaparApiFactory(restOkHttpClient, Json { ignoreUnknownKeys = true }).create(baseUrl)
        }.getOrElse { UnconfiguredVyaparApi }
    }

    /**
     * Reads the `<meta-data>` the host app declares on this service's
     * manifest entry. `:voice → :core:* only` forbids depending on `:app` to
     * read a `BuildConfig` field directly, so the base URL — an `:app`-owned
     * decision by the same reasoning `VyaparApiFactory`'s KDoc gives for not
     * hardcoding it — arrives through the one channel a manifest-declared
     * component can receive host configuration through without a
     * compile-time edge.
     */
    private fun resolveConfiguredBaseUrl(): String? = runCatching {
        val info = packageManager.getServiceInfo(
            ComponentName(this, VoiceCallService::class.java),
            PackageManager.GET_META_DATA,
        )
        info.metaData?.getString(META_DATA_BASE_URL)
    }.getOrNull()

    /** Exposes the call for a future `:feature:support` bound client (docs/03 §3.12's `CallViewModel`). */
    public inner class LocalBinder : Binder() {
        public val callController: CallController? get() = controller
    }

    public companion object {
        public const val ACTION_START: String = "com.vyaparpay.voice.action.START_CALL"
        public const val ACTION_MUTE: String = "com.vyaparpay.voice.action.MUTE"
        public const val ACTION_END: String = "com.vyaparpay.voice.action.END"

        /** JSON-encoded [SessionCreateRequestDto] — see [SessionCreateRequestDto.serializer]. */
        public const val EXTRA_SESSION_REQUEST_JSON: String = "com.vyaparpay.voice.extra.SESSION_REQUEST_JSON"

        /** `<meta-data>` key the host app sets on this service's manifest entry to supply the API base URL. */
        public const val META_DATA_BASE_URL: String = "com.vyaparpay.voice.BASE_URL"

        // One process-lifetime OkHttpClient, matching :core:network's own
        // NetworkModule reasoning: connection pooling shared across every
        // signaling WebSocket this module opens. REST calls use the
        // Hilt-injected restOkHttpClient instance field instead (see its
        // own kdoc for why the two are no longer one client) — this one
        // stays deliberately bare, no interceptors, since
        // ApiErrorReportingInterceptor misreads a 101 Switching Protocols
        // upgrade as a failed response.
        private val sharedOkHttp: OkHttpClient by lazy { OkHttpClient() }
    }
}

/**
 * Stands in for [VyaparApi] when no base URL has been configured yet — every
 * call fails with [ApiError.UNKNOWN], which the already-merged
 * `CallStateMachine` turns into an honest "couldn't start the call" instead
 * of a crash (docs/03 §3.2's `Requesting → Ended` row).
 */
private object UnconfiguredVyaparApi : VyaparApi {
    private val failure = ApiResult.Failure(
        code = ApiError.UNKNOWN,
        message = "Voice API base URL is not configured (VoiceCallService.META_DATA_BASE_URL).",
    )

    override suspend fun createSession(body: SessionCreateRequestDto): ApiResult<ConnectBundleDto> = failure
    override suspend fun remintSessionToken(sessionId: String): ApiResult<ConnectBundleDto> = failure
    override suspend fun endSession(sessionId: String): ApiResult<SessionEndDto> = failure
    override suspend fun getSessionSummary(sessionId: String): ApiResult<SessionSummaryDto> = failure
}
