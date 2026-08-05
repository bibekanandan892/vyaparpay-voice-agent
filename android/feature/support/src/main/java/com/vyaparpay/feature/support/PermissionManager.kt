package com.vyaparpay.feature.support

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.core.app.ActivityCompat

/**
 * What the runtime permission round trip concluded, in the vocabulary the call
 * trigger actually branches on.
 *
 * Three outcomes rather than a boolean because the two denial branches need
 * genuinely different UI: one is "ask again", the other is "we can no longer
 * ask, here is the way out" (docs/03 §3.6). Collapsing them is how apps end up
 * with a Call button that silently does nothing forever.
 */
internal enum class CallPermissionOutcome {
    /** RECORD_AUDIO is granted — the call may start. */
    PROCEED,

    /** RECORD_AUDIO was denied, but the system will still show the dialog next time. */
    RETRYABLE_DENIAL,

    /**
     * RECORD_AUDIO was denied and the system will no longer prompt ("Don't ask
     * again" / policy-blocked). The only remaining path is app settings.
     */
    PERMANENT_DENIAL,
}

/**
 * [CallPermissionOutcome] plus the *non-fatal* half of the request.
 *
 * @param notificationsGranted whether the merchant will actually see the
 *   foreground-service notification. Never gates the call — see
 *   [PermissionManager]'s kdoc.
 */
internal data class CallPermissionDecision(
    val outcome: CallPermissionOutcome,
    val notificationsGranted: Boolean,
)

/**
 * docs/03 §3.6's `PermissionManager`: the pure decision half of the runtime
 * permission flow, deliberately framework-free so every branch is a plain JVM
 * test (the same "confine the framework, test the policy" split
 * `VoiceCallService`'s kdoc describes, applied to permissions).
 *
 * **The two permissions are not peers, and the asymmetry is the whole point.**
 *
 * - `RECORD_AUDIO` is a **hard** requirement: no mic, no call. There is no
 *   degraded voice call to fall back to, so a denial ends the attempt.
 * - `POST_NOTIFICATIONS` (API 33+) is **soft**. `:voice`'s own manifest states
 *   the rule this class implements, verbatim: "denial degrades to a headless
 *   call, never blocks it." **We proceed on denial.** The foreground service
 *   still starts and the call still connects — Android's FGS notification
 *   requirement is about *posting* the notification, which
 *   `ServiceCompat.startForeground` does regardless; a notifications denial
 *   only means the merchant does not *see* it. Blocking a support call over
 *   an invisible notification would be the tail wagging the dog, and it is
 *   the same "degrade, never refuse" reasoning docs/08 §7 applies to context.
 *   The merchant is told (see [CallNotice.NOTIFICATIONS_DENIED]) rather than
 *   silently given a call with no visible affordance to return to it.
 *
 * **Why the permanent-denial test is `!shouldShowRequestPermissionRationale`
 * evaluated *after* a result, never before.** Android exposes no
 * "permanently denied" query. The documented signal is the interaction of the
 * grant result with `shouldShowRequestPermissionRationale`: it returns `true`
 * only in the window between a first denial and a "don't ask again", so
 * `denied && !shouldShowRationale` read *after* a request means the system
 * will not prompt again. Read *before* the first request the same expression
 * is `true` for a never-asked permission and would misreport a fresh install
 * as permanently blocked — which is why [evaluate] takes grant results as its
 * input and is only ever called from the launcher callback.
 */
internal object PermissionManager {

    /** Hard requirement (docs/03 §3.6) — the call cannot happen without it. */
    const val RECORD_AUDIO: String = Manifest.permission.RECORD_AUDIO

    /**
     * Soft requirement, API 33+ only.
     *
     * `Manifest.permission.POST_NOTIFICATIONS` is a compile-time `String`
     * constant inlined by javac, so naming it here is safe on any runtime API
     * level — the same reasoning `VoiceCallService.promoteToForeground`
     * documents for the `FOREGROUND_SERVICE_TYPE_*` ints. [requiredPermissions]
     * is what actually keeps it off the request below API 33.
     */
    @Suppress("InlinedApi")
    const val POST_NOTIFICATIONS: String = Manifest.permission.POST_NOTIFICATIONS

    /**
     * What to hand `RequestMultiplePermissions`.
     *
     * `POST_NOTIFICATIONS` is omitted below API 33 rather than requested and
     * ignored: requesting a permission the platform does not define returns a
     * permanent `false` on some OEM builds, which would make [evaluate] report
     * a notifications denial on every pre-Tiramisu device.
     *
     * @param sdkInt injectable purely so the API-33 boundary is testable on a
     *   plain JVM, where `Build.VERSION.SDK_INT` is 0.
     */
    fun requiredPermissions(sdkInt: Int = Build.VERSION.SDK_INT): Array<String> =
        if (sdkInt >= Build.VERSION_CODES.TIRAMISU) {
            arrayOf(RECORD_AUDIO, POST_NOTIFICATIONS)
        } else {
            arrayOf(RECORD_AUDIO)
        }

    /**
     * Turn a `RequestMultiplePermissions` result into a decision.
     *
     * @param grants the launcher's result map. A permission absent from the map
     *   was never requested — which for [POST_NOTIFICATIONS] means "this device
     *   predates the runtime permission", i.e. notifications work, hence the
     *   `?: true` default. [RECORD_AUDIO] is always requested, so its absence
     *   can only mean a cancelled/malformed result and is treated as a denial.
     * @param canPromptAgain `shouldShowRequestPermissionRationale` for the given
     *   permission, read after the result. See this object's kdoc.
     */
    fun evaluate(
        grants: Map<String, Boolean>,
        canPromptAgain: (String) -> Boolean,
    ): CallPermissionDecision {
        val micGranted = grants[RECORD_AUDIO] == true
        val outcome = when {
            micGranted -> CallPermissionOutcome.PROCEED
            canPromptAgain(RECORD_AUDIO) -> CallPermissionOutcome.RETRYABLE_DENIAL
            else -> CallPermissionOutcome.PERMANENT_DENIAL
        }
        return CallPermissionDecision(
            outcome = outcome,
            notificationsGranted = grants[POST_NOTIFICATIONS] ?: true,
        )
    }

    /**
     * The escape hatch [CallPermissionOutcome.PERMANENT_DENIAL] offers instead
     * of a dead button: the system App Info page, where the merchant can flip
     * the microphone back on.
     *
     * `FLAG_ACTIVITY_NEW_TASK` because [context] may be the application
     * context (it is, when this is reached from a `@ApplicationContext`-scoped
     * caller). Failures are swallowed deliberately — a handful of OEM builds
     * ship without the settings activity resolvable, and crashing the app on a
     * "help me fix this" tap would be strictly worse than the button appearing
     * to do nothing on those devices, which is the state we were already in.
     */
    fun openAppSettings(context: Context) {
        val intent = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(intent) }
    }

    /**
     * Unwraps [ContextWrapper]s to reach the hosting [Activity].
     *
     * `shouldShowRequestPermissionRationale` needs a real `Activity`, and
     * `LocalContext.current` inside a composable is typically a
     * `ContextThemeWrapper` around one rather than the activity itself. `null`
     * (no activity in the chain) is folded to "cannot prompt again" by
     * [rememberCallPermissionGate], the conservative direction: it routes the
     * merchant to settings rather than into a retry loop against a prompt that
     * will never appear.
     */
    tailrec fun findActivity(context: Context?): Activity? = when (context) {
        is Activity -> context
        is ContextWrapper -> findActivity(context.baseContext)
        else -> null
    }
}

/**
 * The composable-scoped half of docs/03 §3.6's permission flow: returns a
 * `() -> Unit` that launches the request and routes the result to [onDecision].
 *
 * **Deliberately `rememberLauncherForActivityResult`, not a `MainActivity`
 * hook.** The Activity Result API registers against the nearest
 * `ActivityResultRegistryOwner` from the composition, which means the whole
 * permission flow lives inside `:feature:support` and survives configuration
 * change without `:app` knowing this feature asks for anything. The older
 * `onRequestPermissionsResult` route would have required an edit to `:app`'s
 * `MainActivity` and a request-code constant shared across a module boundary.
 *
 * **No `rememberUpdatedState` around [onDecision], deliberately.** The usual
 * stale-callback hazard — a launcher registered once, holding the first
 * composition's lambda forever — does not apply here:
 * `rememberLauncherForActivityResult` already wraps its `onResult` in
 * `rememberUpdatedState` internally and invokes the current value, so both
 * [onDecision] and the [context] captured below are refreshed on every
 * recomposition. Adding a second layer would only look like it was load
 * bearing.
 */
@Composable
internal fun rememberCallPermissionGate(onDecision: (CallPermissionDecision) -> Unit): () -> Unit {
    val context = LocalContext.current

    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { grants ->
        val activity = PermissionManager.findActivity(context)
        onDecision(
            PermissionManager.evaluate(grants) { permission ->
                activity != null && ActivityCompat.shouldShowRequestPermissionRationale(activity, permission)
            },
        )
    }

    return remember(launcher) {
        {
            // Always launched, even when RECORD_AUDIO is already granted: the
            // contract short-circuits an already-granted permission to an
            // immediate `true` result without showing a dialog, so the
            // "already granted" path costs nothing and this stays a single
            // code path instead of a checkSelfPermission branch that would
            // need its own tests.
            launcher.launch(PermissionManager.requiredPermissions())
        }
    }
}
