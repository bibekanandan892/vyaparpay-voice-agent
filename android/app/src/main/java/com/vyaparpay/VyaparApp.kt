package com.vyaparpay

import android.app.Application
import dagger.hilt.android.HiltAndroidApp

/**
 * The Hilt graph root (docs/03-android-architecture.md §1, §2.2).
 *
 * The `@Singleton` tier lives here for its whole process lifetime — the
 * `EventTracker` ring buffer, `AppStateManager`, the repositories — because a
 * screen swap must never lose the timeline the next call's context depends on.
 * The call-scoped objects (`WebRtcClient`, `SignalingClient`) deliberately do
 * *not* live here: they are built and released by `VoiceCallService`, since a
 * leaked `PeerConnection` is a live mic.
 */
@HiltAndroidApp
public class VyaparApp : Application()
