package com.vyaparpay.voice.webrtc

import com.vyaparpay.core.screencontext.ContextChannel
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CancellationException

/**
 * The one place the capture pipeline meets the wire (docs/03 §1, §3.10):
 * [ContextChannel] — the port `ScreenContextPublisher` publishes through —
 * backed by [WebRtcClient.sendContext] on the `ctx` data channel.
 *
 * This class is the entire reason the dependency runs `:voice ->
 * :core:screencontext` and never the reverse. `ContextChannel.send` and
 * [WebRtcClient.sendContext] have the identical signature on purpose, so the
 * adapter is a one-line delegation plus the failure policy below — everything
 * else about diffing, debouncing and `seq` stays framework-free upstream, and
 * `org.webrtc` stays behind [PeerConnectionWebRtcClient].
 *
 * **Judgment call — a failed send is swallowed, deliberately and loudly
 * enough to notice.** docs/08 §7's rule for this whole subsystem is "degrade
 * context, never conversation", and the backend's own `ContextDispatcher`
 * applies it in the mirror-image direction (`_send_request_snapshot`'s
 * `except Exception:` → "degrade loudly, never fatally"). On this side the
 * stakes are higher than a lost recovery hint: [WebRtcClient.sendContext] is
 * called from `ScreenContextPublisher`'s two collectors, which run on a
 * call-scoped `SupervisorJob`; an escaping exception there kills the
 * collector, and — with no handler in between — is delivered to the process's
 * uncaught-exception path. A screen the merchant swiped to while the data
 * channel happened to be mid-teardown would take down the *call*. That is
 * precisely backwards.
 *
 * The throws are real, not hypothetical: [PeerConnectionWebRtcClient]'s
 * `sendContext` deliberately "fails loud" — a [WebRtcException] before
 * `start()`, and another whenever the channel is not `OPEN`, which is the
 * routine state during the docs/06 §6 reconnect grace and for the whole
 * window between `PeerConnected` and SCTP actually opening. That loudness is
 * correct at *its* layer ("the caller owns lifecycle and must not silently
 * drop context") — this class is that caller, and this is where the decision
 * gets made. Native `DataChannel.send` can also throw on a disposed channel
 * during teardown races.
 *
 * [CancellationException] is re-thrown rather than counted: call teardown
 * cancels the publisher's scope, and swallowing cancellation would leave a
 * collector running past the end of the call — the exact privacy failure the
 * call-scoped binding exists to prevent. Only genuine failures are dropped.
 *
 * `Exception`, not `Throwable`: an `OutOfMemoryError` or a linkage error is
 * not something a context frame should paper over, and unlike
 * `VoiceCallService`'s one-shot `WebRtcClientFactory.create()` (which catches
 * `Throwable` for exactly the `UnsatisfiedLinkError` case) this runs on every
 * frame, where quietly absorbing an `Error` would hide a broken process
 * behind a merely stale agent.
 *
 * [droppedFrameCount] is the observability that keeps this from being a
 * silent failure — `:voice` carries no logger, so a monotonic counter on the
 * adapter instance is the honest minimum, and it is what
 * `WebRtcContextChannelTest` asserts against.
 */
public class WebRtcContextChannel(
    private val webRtc: WebRtcClient,
) : ContextChannel {

    private val dropped = AtomicLong(0)

    /** How many frames this channel gave up on. Non-zero means the agent's view of the screen is stale. */
    public val droppedFrameCount: Long get() = dropped.get()

    override suspend fun send(bytes: ByteArray) {
        try {
            webRtc.sendContext(bytes)
        } catch (cancellation: CancellationException) {
            throw cancellation
        } catch (_: Exception) {
            dropped.incrementAndGet()
        }
    }
}
