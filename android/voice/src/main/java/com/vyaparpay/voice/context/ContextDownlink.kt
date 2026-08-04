package com.vyaparpay.voice.context

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * What an inbound `ctx` frame means to *this* task. Deliberately coarse: the
 * downlink carries four server-originated types (docs/13 §4) and only one of
 * them is context's business.
 */
public enum class ContextDownlinkFrame {

    /**
     * `ctx.request_snapshot` — the server hit a `seq` gap, discarded the
     * gapped message, and wants a fresh capture (docs/08 §3.3).
     */
    REQUEST_SNAPSHOT,

    /**
     * Everything else, folded into one outcome on purpose.
     *
     * `transcript.partial` / `transcript.final` / `agent.state` are **not
     * unknown** — they are the caption and indicator feed for
     * `ConversationOverlay`, which is a later UI task (docs/03 §3.12). This
     * enum has no member for them because a member would imply this task
     * routes them somewhere, and it does not; the seam is a deliberate,
     * documented hand-off, not a gap. When that task lands it adds its own
     * members here (or its own collector on `WebRtcClient.incoming`, which is
     * a `SharedFlow` and supports both) — nothing about the recovery path
     * below has to change.
     *
     * Malformed bytes land here too, by the same rule the backend applies to
     * *its* inbound direction: one bad message degrades context for that
     * message only and never propagates (`ContextDispatcher` judgment call 2).
     */
    IGNORED,
}

/**
 * Classifies one raw `ctx` downlink payload (docs/13 §4's server → client
 * envelope: `{v, type, seq, ts, payload}`).
 *
 * **Judgment call — this reads exactly one field and validates nothing
 * else.** The obvious alternative is deserializing the whole envelope into a
 * typed model the way `SignalWire` does for the signaling plane. It is the
 * wrong trade here for three reasons: `ctx.request_snapshot`'s payload
 * (`{last_good_seq}`) is information this client cannot act on — the client
 * "never gap-checks the downlink" (docs/03 §3.4, docs/08 §3.2), and the
 * response is always the same "send a fresh full snapshot" regardless of
 * which seq was last good; the other three types are another task's, so
 * modeling them here would be speculative generality; and every extra
 * required field is one more way a future additive-within-v1 change (docs/13
 * §9) turns a harmless unknown into a dropped recovery request.
 *
 * Nothing here can throw. Non-JSON bytes, valid JSON that is not an object,
 * an object with no `type`, a `type` that is not a string — all of them fall
 * through to [ContextDownlinkFrame.IGNORED]. A crash on this path would take
 * down a live call over a stray frame, which is the inverse of docs/08 §7's
 * "degrade context, never conversation".
 */
public object ContextDownlink {

    /** docs/13 §4; matches `backend/app/voice/context_dispatch.py`'s `DC_TYPE_CTX_REQUEST_SNAPSHOT`. */
    public const val TYPE_REQUEST_SNAPSHOT: String = "ctx.request_snapshot"

    private const val FIELD_TYPE: String = "type"

    public fun classify(bytes: ByteArray): ContextDownlinkFrame {
        val type = runCatching {
            Json.parseToJsonElement(bytes.toString(Charsets.UTF_8))
                .jsonObject[FIELD_TYPE]
                ?.jsonPrimitive
                ?.contentOrNull
        }.getOrNull()

        return if (type == TYPE_REQUEST_SNAPSHOT) {
            ContextDownlinkFrame.REQUEST_SNAPSHOT
        } else {
            ContextDownlinkFrame.IGNORED
        }
    }
}
