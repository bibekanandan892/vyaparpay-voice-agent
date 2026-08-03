package com.vyaparpay.core.screencontext

/**
 * The port that keeps WebRTC out of the capture path (docs/03 §1, §3.10).
 *
 * `ScreenContextPublisher` speaks to this interface; `:voice` supplies the
 * data-channel-backed implementation. The direction of the dependency is the
 * whole point — `:voice -> :core:screencontext`, never the reverse — so the
 * diff/debounce logic stays framework-free and unit-testable and only the
 * byte-shipping adapter ever imports `org.webrtc`.
 */
public interface ContextChannel {

    /** Ships one encoded frame. Reliable and ordered; the caller assigns `seq`. */
    public suspend fun send(bytes: ByteArray)
}

/**
 * The three client-to-server frame kinds that share one `seq` counter, so the
 * backend can run a single gap detector over the stream (docs/08 §3.2).
 */
public enum class ContextFrameType {
    /** A new screen: the full `screen_context/v1` IR. */
    SNAPSHOT,

    /** Same screen, changed components — `base_seq`-tagged. */
    DELTA,

    /** A discrete timeline fact; bypasses both the capture debounce and conflation. */
    EVENT,
}
