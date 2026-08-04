package com.vyaparpay.voice.context

import com.vyaparpay.core.screencontext.ContextChannel
import com.vyaparpay.core.screencontext.ScreenContextPublisher
import kotlinx.coroutines.CoroutineScope

/**
 * The two things `CallController` needs from the capture pipeline, and
 * nothing else: bind it to a channel for the life of a call, and answer the
 * server's gap-recovery request.
 *
 * **Judgment call — why this interface exists at all, given
 * [ScreenContextPublisher] already has both methods verbatim.** Testability,
 * the same reason `CallAudioSession`/`CallNotifier` exist one layer over in
 * `service/`: a `ScreenContextPublisher` cannot be constructed from `:voice`'s
 * test source set. Its only non-`internal` constructor takes an
 * `AppStateManager`, whose only non-`internal` constructor takes a
 * `UiTreeCollector`, which `UiTreeCollectorTest`'s own kdoc establishes "has
 * no meaningful fake on a plain JVM" — it needs a live Compose composition to
 * emit anything. Typing `CallController` at the concrete class would make the
 * bind/teardown lifecycle — the highest-risk part of this wiring, since a
 * collector outliving a call keeps shipping the merchant's screen to a dead
 * channel — untestable on the JVM. This is the identical seam-narrowing
 * judgment [ScreenContextPublisher] and `AppStateManager` each make in their
 * own kdocs, applied across a module boundary where a secondary constructor
 * cannot reach.
 *
 * The interface is deliberately shaped as a *subset* of
 * [ScreenContextPublisher]'s own surface rather than a redesign, so
 * [ScreenContextPublisherBinding] below is pure delegation with no policy of
 * its own to get wrong.
 */
public interface CallContextPublisher {

    /**
     * Begin publishing to [channel] on [scope]. There is no `stop()`:
     * cancelling [scope] is the teardown mechanism, which is exactly why the
     * caller must supply a scope that dies with the call
     * (`CallController.bindContextPublisher`'s kdoc has the full reasoning).
     */
    public fun start(channel: ContextChannel, scope: CoroutineScope)

    /** Answer `ctx.request_snapshot` with a fresh full snapshot (docs/08 §3.3). */
    public suspend fun requestFullSnapshot()
}

/**
 * The production [CallContextPublisher]: the real
 * [ScreenContextPublisher] from `:core:screencontext`.
 *
 * `VoiceCallService` builds one per call from a `Provider`, never a
 * `@Singleton` — a fresh publisher restarts the client `seq` counter at
 * `GENESIS_SEQ + 1`, which is what the backend's own per-session
 * `SnapshotIngestor` anchor expects (see [ScreenContextPublisher]'s kdoc on
 * the genesis anchor). A publisher shared across calls would open the second
 * call with a `seq` the server reads as a gap on message one.
 */
public class ScreenContextPublisherBinding(
    private val publisher: ScreenContextPublisher,
) : CallContextPublisher {

    override fun start(channel: ContextChannel, scope: CoroutineScope) {
        publisher.start(channel, scope)
    }

    override suspend fun requestFullSnapshot() {
        publisher.requestFullSnapshot()
    }
}
