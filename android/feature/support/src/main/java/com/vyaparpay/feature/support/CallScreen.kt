package com.vyaparpay.feature.support

import android.provider.Settings
import androidx.compose.animation.core.EaseInOut
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.VolumeOff
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material.icons.filled.SupportAgent
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable
import java.util.Locale
import kotlinx.coroutines.delay

/** testTag for the full-screen call surface as a whole. */
internal const val CALL_SCREEN_TEST_TAG = "call_screen"

/** testTag for the status line ("Calling…"/"Connected · 01:04"/"Reconnecting…"). */
internal const val CALL_SCREEN_STATUS_TEST_TAG = "call_screen_status_text"

/** testTag for the mute toggle. */
internal const val CALL_MUTE_TOGGLE_TEST_TAG = "call_mute_toggle"

/** testTag for the speaker toggle. */
internal const val CALL_SPEAKER_TOGGLE_TEST_TAG = "call_speaker_toggle"

/**
 * docs/03 §3.12's fuller in-call surface, scoped to what Phases 1-2 actually
 * built: mute, speaker, end-call, and a live elapsed timer — deliberately
 * still not [CallStatusPanel]'s kdoc-documented `ConversationOverlay`
 * (transcript + agent-state indicator), which stays the open seam that
 * kdoc names.
 *
 * **Replaces [CallStatusPanel], not a peer of it.** [SupportRoute] mounts
 * exactly one of the two, chosen by [CallUiState.isFullScreenCall]: this
 * screen while a call could be running, the banner for a terminal summary or
 * a permission notice. [CallStatusPanel] itself is untouched — its own tests
 * exercise it standalone and stay valid regardless of which phases
 * [SupportRoute] actually routes to it.
 *
 * Ending [End Call][CALL_HANG_UP_TEST_TAG] deliberately reuses
 * [CallStatusPanel]'s own tag rather than minting a new one: it is the same
 * control in a different layout, and anything keying off that tag (this
 * module's own tests, any future tooling) should not need to know which
 * surface is currently showing it.
 *
 * **No minimize control, and none intercepts the back gesture.** docs/03
 * §3.12's fuller spec describes an overlay that collapses to a chip on
 * minimize; here system back simply leaves the screen, and that is safe
 * with zero code: [CallViewModel.onCleared]'s own kdoc is what guarantees
 * leaving never hangs up the call (the service keeps running; only the
 * observation stops) — [CallSurfaceTest] asserts exactly that. The way
 * *back into* an in-progress call from elsewhere in the app is not this
 * composable's job either: it is `:app`'s `ReturnToCallChip`, hosted above
 * the `NavHost` in `MainActivity` and driven by [CallActivitySignal] (the
 * one signal this module publishes for that purpose) — the same
 * above-the-NavHost placement [SupportButton]'s own kdoc names for the
 * floating FAB it still defers.
 */
@Composable
internal fun BoxScope.CallSurface(
    state: CallUiState,
    events: EventTracker,
    onHangUp: () -> Unit,
    onDismiss: () -> Unit,
    onToggleMute: () -> Unit,
    onToggleSpeaker: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    if (state.isFullScreenCall) {
        // Mounted only while a call could be running, not kept around
        // hidden: CallScreen ticks a live elapsed-time timer, and that timer
        // must start/stop with the screen's own composition lifecycle rather
        // than run in the background whenever it isn't shown.
        CallScreen(
            state = state,
            events = events,
            onHangUp = onHangUp,
            onToggleMute = onToggleMute,
            onToggleSpeaker = onToggleSpeaker,
            modifier = Modifier.fillMaxSize(),
        )
    } else {
        CallStatusPanel(
            state = state,
            events = events,
            onHangUp = onHangUp,
            onDismiss = onDismiss,
            onOpenSettings = onOpenSettings,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(16.dp),
        )
    }
}

@Composable
internal fun CallScreen(
    state: CallUiState,
    events: EventTracker,
    onHangUp: () -> Unit,
    onToggleMute: () -> Unit,
    onToggleSpeaker: () -> Unit,
    modifier: Modifier = Modifier,
    /** Injectable for deterministic tests — see [rememberElapsedSeconds]. */
    clock: () -> Long = System::currentTimeMillis,
) {
    val elapsedSeconds = rememberElapsedSeconds(state.inCallSinceMillis, clock)

    // A Surface, not a Column with a background: [SupportRoute] composes
    // this OVER a still-live HelpScreen in the same Box, and Compose
    // hit-testing keeps descending to earlier siblings until something with
    // a pointer-input node claims the touch. A plain background modifier is
    // not one, so a tap on any blank part of the call screen would land on
    // the full-width "Call Support" button sitting directly underneath —
    // recording a phantom tap on the agent's timeline and, on API 33+,
    // popping the notification-permission dialog over the live call.
    // Material3's non-clickable Surface installs an empty pointerInput
    // precisely to swallow such touches; CallSurfaceTest pins it.
    Surface(
        modifier = modifier
            .fillMaxSize()
            .testTag(CALL_SCREEN_TEST_TAG),
        color = MaterialTheme.colorScheme.surface,
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = "Asha — VyaparPay Support",
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.padding(top = 32.dp),
            )

            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(20.dp),
            ) {
                // Pulsing while the media plane is still coming up or being
                // recovered; calm (no ring) once InCall — a steady avatar
                // reads as "connected" without needing a second visual
                // language.
                CallAvatar(pulsing = state.phase != CallPhase.IN_CALL)
                Text(
                    text = state.statusLine(elapsedSeconds),
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.testTag(CALL_SCREEN_STATUS_TEST_TAG),
                )
            }

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(bottom = 24.dp),
                horizontalArrangement = Arrangement.spacedBy(32.dp, Alignment.CenterHorizontally),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                CallControlButton(
                    icon = if (state.muted) Icons.Filled.MicOff else Icons.Filled.Mic,
                    label = if (state.muted) "Unmute" else "Mute",
                    testTag = CALL_MUTE_TOGGLE_TEST_TAG,
                    active = state.muted,
                    events = events,
                    onClick = onToggleMute,
                )
                EndCallButton(events = events, onClick = onHangUp)
                CallControlButton(
                    icon = if (state.speakerOn) Icons.AutoMirrored.Filled.VolumeUp else Icons.AutoMirrored.Filled.VolumeOff,
                    label = "Speaker",
                    testTag = CALL_SPEAKER_TOGGLE_TEST_TAG,
                    active = state.speakerOn,
                    events = events,
                    onClick = onToggleSpeaker,
                )
            }
        }
    }
}

/**
 * The one line describing the call — the [CallScreen] analog of
 * [CallStatusPanel]'s own `statusLine`, but only ever asked for the three
 * phases [CallUiState.isFullScreenCall] routes here. [CallPhase.IDLE] and
 * [CallPhase.ENDED] fall through to an empty string rather than a `when`
 * branch that can't be reached from [SupportRoute]'s wiring — not exhaustive
 * on purpose, so a future [CallPhase] addition fails loudly somewhere that
 * actually renders it ([CallStatusPanel]'s own exhaustive `when`) instead of
 * silently here too.
 */
private fun CallUiState.statusLine(elapsedSeconds: Long): String = when (phase) {
    CallPhase.CONNECTING -> "Calling…"
    CallPhase.IN_CALL -> if (inCallSinceMillis != null) "Connected · ${formatElapsed(elapsedSeconds)}" else "Connected"
    CallPhase.RECONNECTING -> "Reconnecting…"
    CallPhase.IDLE, CallPhase.ENDED -> ""
}

/** `125` -> `"02:05"`. Free function so it is unit-testable without Compose. */
internal fun formatElapsed(totalSeconds: Long): String {
    val minutes = totalSeconds / SECONDS_PER_MINUTE
    val seconds = totalSeconds % SECONDS_PER_MINUTE
    // Locale.ROOT, not the default locale: some locales render digit
    // glyphs that are not ASCII 0-9, which a timer display should not do.
    return String.format(Locale.ROOT, "%02d:%02d", minutes, seconds)
}

private const val SECONDS_PER_MINUTE = 60L
private const val TIMER_TICK_MILLIS = 1_000L

/**
 * Seconds elapsed since [inCallSinceMillis], ticking once a second while the
 * call is live. `null` renders as `0` — [statusLine] only consults this when
 * [inCallSinceMillis] is non-null, so the zero is never shown.
 *
 * Restarted on every distinct [inCallSinceMillis] (the `remember`/
 * `LaunchedEffect` key): a reconnect never changes that value (Phase 1's
 * [com.vyaparpay.voice.CallController.inCallSinceMillis] is stamped once per
 * call), so the timer is not reachable to reset mid-call — only a genuinely
 * new call, which is a new composition of [CallScreen] entirely, restarts it.
 */
@Composable
private fun rememberElapsedSeconds(inCallSinceMillis: Long?, clock: () -> Long): Long {
    var elapsedSeconds by remember(inCallSinceMillis) { mutableLongStateOf(0L) }
    LaunchedEffect(inCallSinceMillis) {
        if (inCallSinceMillis == null) return@LaunchedEffect
        while (true) {
            elapsedSeconds = (clock() - inCallSinceMillis).coerceAtLeast(0L) / TIMER_TICK_MILLIS
            delay(TIMER_TICK_MILLIS)
        }
    }
    return elapsedSeconds
}

/**
 * The system's "Remove animations" accessibility setting, expressed the way
 * Android actually exposes it: `ANIMATOR_DURATION_SCALE == 0`. Compose has no
 * first-party reduced-motion signal, and this is the standard read for it.
 */
@Composable
private fun rememberAnimationsEnabled(): Boolean {
    val context = LocalContext.current
    return remember {
        Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) != 0f
    }
}

@Composable
private fun CallAvatar(pulsing: Boolean, modifier: Modifier = Modifier) {
    val animationsEnabled = rememberAnimationsEnabled()
    val shouldPulse = pulsing && animationsEnabled

    // State<Float>, not .value read here: reading .value at composition
    // scope would subscribe CallAvatar's own recomposition to the animated
    // value, defeating graphicsLayer's whole purpose (deferred reads so only
    // the draw phase re-runs per frame). Keeping the State object and
    // reading .value *inside* the graphicsLayer lambda below is what keeps
    // this composable itself from recomposing ~60x/sec while pulsing.
    val pulseScale: State<Float>
    val pulseAlpha: State<Float>
    if (shouldPulse) {
        val transition = rememberInfiniteTransition(label = "call_avatar_pulse")
        pulseScale = transition.animateFloat(
            initialValue = 1f,
            targetValue = 1.5f,
            animationSpec = infiniteRepeatable(tween(PULSE_DURATION_MILLIS, easing = EaseInOut), RepeatMode.Restart),
            label = "call_avatar_pulse_scale",
        )
        pulseAlpha = transition.animateFloat(
            initialValue = 0.45f,
            targetValue = 0f,
            animationSpec = infiniteRepeatable(tween(PULSE_DURATION_MILLIS, easing = EaseInOut), RepeatMode.Restart),
            label = "call_avatar_pulse_alpha",
        )
    } else {
        pulseScale = rememberUpdatedState(1f)
        pulseAlpha = rememberUpdatedState(0f)
    }

    Box(contentAlignment = Alignment.Center, modifier = modifier.size(140.dp)) {
        Box(
            modifier = Modifier
                .size(96.dp)
                .graphicsLayer {
                    scaleX = pulseScale.value
                    scaleY = pulseScale.value
                    alpha = pulseAlpha.value
                }
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.primary),
        )
        Box(
            modifier = Modifier
                .size(96.dp)
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.primary),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                imageVector = Icons.Filled.SupportAgent,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onPrimary,
                modifier = Modifier.size(48.dp),
            )
        }
    }
}

private const val PULSE_DURATION_MILLIS = 1_200

@Composable
private fun CallControlButton(
    icon: ImageVector,
    label: String,
    testTag: String,
    active: Boolean,
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = modifier) {
        Surface(
            shape = CircleShape,
            color = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
            contentColor = if (active) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .size(64.dp)
                .trackedClickable(
                    events = events,
                    screen = SupportDestination.ROUTE,
                    testTag = testTag,
                    role = Role.Button,
                    onClick = onClick,
                ),
        ) {
            Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
                Icon(imageVector = icon, contentDescription = label)
            }
        }
        Spacer(modifier = Modifier.height(6.dp))
        // clearAndSetSemantics {}: the Surface above already carries `label`
        // as its merged contentDescription (via the Icon), and clickable's
        // mergeDescendants pulls that into one accessibility node. This Text
        // is a sibling outside that merge boundary — without clearing its
        // semantics, TalkBack would announce the control twice: once for the
        // Surface's merged node, once more for this Text on its own.
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier.clearAndSetSemantics {},
        )
    }
}

@Composable
private fun EndCallButton(events: EventTracker, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        shape = CircleShape,
        color = MaterialTheme.colorScheme.error,
        contentColor = MaterialTheme.colorScheme.onError,
        modifier = modifier
            .size(72.dp)
            .trackedClickable(
                events = events,
                screen = SupportDestination.ROUTE,
                testTag = CALL_HANG_UP_TEST_TAG,
                role = Role.Button,
                onClick = onClick,
            ),
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Icon(
                imageVector = Icons.Filled.CallEnd,
                contentDescription = "End Call",
                modifier = Modifier.size(32.dp),
            )
        }
    }
}
