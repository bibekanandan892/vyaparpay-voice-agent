package com.vyaparpay

import android.os.Looper
import androidx.activity.compose.setContent
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.navigation.NavHostController
import androidx.navigation.compose.rememberNavController
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.screencontext.AppStateManager
import com.vyaparpay.core.screencontext.ContextChannel
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.core.screencontext.ScreenComponent
import com.vyaparpay.core.screencontext.ScreenComponentRole
import com.vyaparpay.core.screencontext.ScreenContextIr
import com.vyaparpay.core.screencontext.ScreenContextPublisher
import com.vyaparpay.core.ui.theme.VyaparTheme
import com.vyaparpay.feature.payments.PaymentDestination
import com.vyaparpay.feature.payments.SeededPaymentRepository
import com.vyaparpay.feature.support.SupportDestination
import com.vyaparpay.navigation.AppNavHost
import com.vyaparpay.navigation.AppRoute
import com.vyaparpay.voice.context.ScreenContextPublisherBinding
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import dagger.hilt.android.testing.HiltTestApplication
import dagger.hilt.components.SingletonComponent
import java.time.Duration
import javax.inject.Provider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Phase-4 T8e — the full-chain canary: docs/01 §7's canonical incident driven
 * end to end through the REAL capture pipeline, with a fake [ContextChannel]
 * as the only substituted component.
 *
 * **What is real here.** The `@AndroidEntryPoint` [MainActivity] under a real
 * Hilt graph; its Hilt-injected `@Singleton` `UiTreeCollector`,
 * `NavigationTracker` and [ChildWindowTracker]; the real `AppNavHost` and a
 * real `NavHostController`; the real `PaymentScreen`/`PaymentViewModel`/
 * `SeededPaymentRepository` behind `PaymentRoute()`; the real
 * `HelpScreen`/`SupportRoute()`; the real `@Singleton` [AppStateManager] and a
 * per-call [ScreenContextPublisher], both pulled out of the Hilt graph so they
 * are the same instances `CallViewModel` and `VoiceCallService` would get; and
 * `:voice`'s own production [ScreenContextPublisherBinding] adapter. Nothing
 * builds a `RawSemanticsTree`, a `ScreenContextIr`, or an `AppContextState` by
 * hand.
 *
 * **What building this found (Phase-4 T8e).** `UiTreeCollector` was missing
 * `@Singleton`, so Hilt gave `MainActivity` and `AppStateManager` two different
 * collectors — the manager's was never started and never had a root attached,
 * so `AppStateManager.state` never advanced past its seed and the app published
 * **no `ctx.snapshot` and no `ctx.delta` at all**, on any call, while shipping
 * `screen_context: null` in every session body. See [UiTreeCollector]'s own
 * kdoc for the full write-up. Every existing test constructed its own collector
 * by hand, so none could see it; this is the first test that reads
 * `AppStateManager` and `MainActivity` in the same breath, and all four cases
 * below fail without the fix.
 *
 * **What is substituted, and why that is the honest best available.**
 * docs/17 §2.4's exit criterion asks for a live ScreenContext snapshot
 * "captured on a real device". No device and no AVD exists in this
 * environment (`adb devices` and `emulator -list-avds` are both empty), so
 * this is a Robolectric stand-in — the same substitution
 * `UiTreeCollectorPaymentScreenCanaryTest`, `SettlementsScreenContextCanaryTest`
 * and [MainActivityDialogWindowCaptureTest] already make, and it does not
 * discharge that exit criterion, it only gets as close as this environment
 * allows. [FakeContextChannel] stands in for `WebRtcContextChannel`: a real
 * `PeerConnection` needs WebRTC's native library, which does not load under
 * Robolectric. Everything upstream of `ContextChannel.send` is production code.
 *
 * **What this canary deliberately does NOT prove.** It never opens a real
 * `RTCDataChannel`, so nothing here says the bytes survive the wire — that is
 * `WebRtcContextChannelTest`'s job. It never runs `VoiceCallService` or
 * `CallController`, so the call lifecycle around the publisher binding is out
 * of scope (`CallControllerContextTest` covers it). It never exercises
 * `requestFullSnapshot`/gap recovery. And it does not measure the raw-tree ->
 * IR token compression docs/17 §2.4 also asks for; `DropLadder`'s budget is
 * asserted against a real render in `SettlementsScreenContextCanaryTest`, not
 * here.
 *
 * **Why the nav graph is re-hosted instead of navigated in place.**
 * [MainActivity] calls `rememberNavController()` inside its own `setContent`
 * block and keeps no reference a test can reach, and `AppNavHost` declares no
 * UI edge from the start destination to either `PaymentScreen` or
 * `HelpScreen` (the only edge in the whole graph is
 * `OrdersScreen -> OrderTrackingScreen`) — so there is no sequence of taps
 * that walks a launched [MainActivity] through this scenario.
 * [startAppOnRealNavGraph] therefore re-runs `setContent` with the *same*
 * three lines [MainActivity.onCreate] uses — `rememberNavController()`,
 * `LaunchedEffect(Unit) { navigationTracker.bind(controller) }`,
 * `AppNavHost(navController = controller)` — differing only in that the
 * controller is hoisted where the test can call `navigate()` on it, which is
 * exactly what a UI edge would have called. `ComponentActivity.setContent`
 * reuses the activity's existing `ComposeView`, so the main-window root
 * [MainActivity.attachComposeRoot] already handed to the collector stays the
 * same valid instance (the reasoning [MainActivityDialogWindowCaptureTest]
 * documents at length). One honest side effect: the tracker ends up bound to
 * both [MainActivity]'s original controller and this one, so the timeline
 * carries two `nav -> DashboardScreen` entries at start-up. The original
 * controller's composition is disposed and it never navigates again, so no
 * later assertion sees a duplicate.
 *
 * **Why `sessionCreateBody` is called directly rather than through
 * `CallViewModel.startCall()`.** `startCall` is `internal` to
 * `:feature:support` and unreachable from `:app` — deliberately so (see
 * `CallViewModel`'s kdoc: making the permission-gate bypass unrepresentable).
 * `CallViewModelSessionBodyTest` already proves `startCall` ships
 * `AppStateManager.sessionCreateBody`'s output unmodified, so calling that
 * same production method here composes with that proof instead of duplicating
 * it. The "Call Support" CTA itself IS tapped through the real
 * [SupportDestination] screen, because the tap's timeline entry is part of
 * what this canary asserts.
 */
@HiltAndroidTest
@RunWith(RobolectricTestRunner::class)
@Config(
    application = HiltTestApplication::class,
    sdk = [34],
    // A normal phone size (Pixel-5-ish), matching SettlementsScreenContextTest's
    // own config: Robolectric's default window is too short for
    // SettlementsScreen's header plus weighted list, and the capture-policy
    // sweep below visits every route in the real graph, including that one.
    qualifiers = "w411dp-h891dp",
)
class FullChainScreenContextCanaryTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeTestRule = createAndroidComposeRule<MainActivity>()

    /**
     * Stands in for `CallController.bindContextPublisher`'s call-scoped scope
     * — `Dispatchers.Main.immediate`-backed like `ScreenContextModule`'s own
     * binding, and cancelled in [tearDown] because cancelling the scope is
     * [ScreenContextPublisher]'s only teardown mechanism (it has no `stop()`).
     */
    private val callScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private lateinit var navController: NavHostController
    private lateinit var appState: AppStateManager
    private lateinit var publishers: Provider<ScreenContextPublisher>
    private lateinit var eventTracker: EventTracker

    @After
    fun tearDown() {
        callScope.cancel()
    }

    // ------------------------------------------------------------------
    // 1. The decline dialog reaches the IR (docs/01 §7 steps 1-2)
    // ------------------------------------------------------------------

    /**
     * The flagship scenario, and the reason the project exists: Rajesh's ₹245
     * vendor payment is declined, and the resulting IR — the one that will ride
     * in `POST /v1/sessions` — carries the decline dialog, the amount, the
     * recipient and the `402 DAILY_LIMIT_EXCEEDED` that explains it.
     *
     * Checked against `protocol/fixtures/context/ctx_snapshot_payment_decline
     * .json`'s `payload`, which is that wire shape's authority. The fixture is
     * quoted as literals rather than read from disk: Android unit tests have no
     * wired-up path to `protocol/`, the convention `SemanticSnapshotBuilderTest`
     * and `ScreenContextPublisherTest` already established.
     *
     * **Production and the fixture disagree in five places, and this test pins
     * what production actually emits rather than what the fixture says.** Every
     * one is asserted explicitly below so the divergence is visible and fails
     * loudly if it changes; none is papered over by a loose assertion. They are
     * all pre-existing and none is in this task's scope to change:
     *
     * 1. `last_action.target` is `pay_now_cta`, the testTag, not `"Pay Now"` —
     *    `Modifier.trackedClickable` (`:core:ui`) records
     *    `AppEvent.Tap(name = testTag)`.
     * 2. The recipient's label is `"Paying"`, not `"To"` — `RecipientRow`'s own
     *    leading `Text`.
     * 3. The snackbar's label is `"Payment failed"`, not `"Payment Failed"`.
     * 4. `dirty_fields` is `["amount_input"]`, not `[]` — the fixture depicts
     *    the moment after the decline, but the amount field really has been
     *    edited by then.
     * 5. Production emits a `secondary_cta` the fixture has no component for.
     *
     * An earlier version of this kdoc claimed everything matched except (1);
     * that was false, and the assertions were loose enough not to notice.
     *
     * The dialog half of this is only reachable because T8f taught
     * [ChildWindowTracker] to discover the `AlertDialog`'s own window in
     * production — this test never names that window, never calls `attachRoot`,
     * and never touches Robolectric's dialog shadows.
     */
    @Test
    fun `the canonical decline produces an IR matching the fixture's payload`() {
        startAppOnRealNavGraph()
        navigateTo(AppRoute.PAYMENT.route)
        declineCanonicalPayment()

        val ir = retainedScreen()
        assertNotNull("no operational screen was ever retained -- the capture pipeline produced nothing", ir)
        assertEquals(PaymentDestination.ROUTE, ir!!.screen)
        assertEquals(PaymentDestination.FLOW, ir.flow)

        // The dialog: docs/01 §7 step 2, and the component T8f exists to make
        // reachable at all.
        val dialog = ir.components.filterIsInstance<ScreenComponent.Dialog>().singleOrNull()
        assertNotNull(
            "expected exactly one dialog component, roles were ${ir.components.map { it.role }}",
            dialog,
        )
        assertEquals(DECLINE_DIALOG_TITLE, dialog!!.label)
        assertTrue("the decline dialog must be reported visible", dialog.visible)

        // The amount and recipient: the two facts that let the agent open with
        // "your ₹245 payment to Amazon Business".
        val amount = ir.components.filterIsInstance<ScreenComponent.AmountField>().singleOrNull()
        assertNotNull("expected an amount_field, roles were ${ir.components.map { it.role }}", amount)
        assertTrue(
            "expected the amount field to carry ₹$CANONICAL_AMOUNT, got '${amount!!.value}'",
            amount.value.endsWith(CANONICAL_AMOUNT),
        )
        val recipient = ir.components.filterIsInstance<ScreenComponent.Recipient>().singleOrNull()
        assertNotNull("expected a recipient component", recipient)
        assertEquals(SeededPaymentRepository.CANONICAL_RECIPIENT, recipient!!.value)
        // Divergence 2: the fixture's label is "To".
        assertEquals("Paying", recipient.label)

        // The snackbar: PaymentViewModel.onPaymentDeclined sets it in the same
        // state update as the dialog, and the fixture carries both.
        // Divergence 3: the fixture capitalises this as "Payment Failed".
        val snackbar = ir.components.filterIsInstance<ScreenComponent.Snackbar>().singleOrNull()
        assertNotNull("expected a snackbar component, roles were ${ir.components.map { it.role }}", snackbar)
        assertEquals("Payment failed", snackbar!!.label)
        assertTrue("the snackbar must be reported visible", snackbar.visible)

        // The primary CTA, and divergence 5: production also emits a
        // secondary_cta, which the fixture has no component for at all.
        val primary = ir.components.filterIsInstance<ScreenComponent.PrimaryCta>().singleOrNull()
        assertNotNull("expected a primary_cta", primary)
        assertEquals("Pay Now", primary!!.label)
        assertTrue("the Pay Now CTA is enabled at 245", primary.enabled)
        assertTrue(
            "expected the secondary_cta production emits but the fixture omits, roles were " +
                "${ir.components.map { it.role }}",
            ir.components.any { it.role == ScreenComponentRole.SECONDARY_CTA },
        )

        // Envelope and top-level scalars. Divergence 4: the fixture pins
        // dirty_fields to [], but the amount field genuinely has been edited by
        // the time the decline lands.
        assertEquals("screen_context/v1", ir.v)
        assertEquals(listOf(AMOUNT_TEST_TAG), ir.dirtyFields)
        assertTrue("loading must be false once the decline has resolved", !ir.loading)

        // `last_api` is the single highest-value field in the whole payload --
        // it is *why* the merchant is calling. It reaches the IR only because
        // the real PaymentViewModel routed the decline through the real
        // ApiErrorReporter into the app-singleton EventTracker, which
        // AppStateManager then pulled inside its combine. Nothing in this test
        // records it.
        assertEquals("POST", ir.lastApi?.method)
        assertEquals("/payments", ir.lastApi?.path)
        assertEquals(402, ir.lastApi?.status)
        assertEquals(DAILY_LIMIT_EXCEEDED, ir.lastApi?.errorCode)

        // `last_action`: the real trackedClickable on the real Pay Now CTA.
        //
        // **`target` is the testTag, not the human label — a real divergence
        // from the fixture, asserted as-is rather than papered over.**
        // `ctx_snapshot_payment_decline.json` and docs/07 §3/§6 both show
        // `"target": "Pay Now"`, but `Modifier.trackedClickable` records
        // `AppEvent.Tap(name = testTag, ...)` (`:core:ui`, pinned by its own
        // `TrackedClickableTest`), and `SemanticSnapshotBuilder` maps
        // `AppEvent.name -> last_action.target` verbatim. So production ships
        // `pay_now_cta`. That is pre-existing behaviour owned by `:core:ui` and
        // out of this task's scope to change — this assertion pins what the app
        // actually does so the divergence is visible and greppable instead of
        // implied, and it is reported alongside this task rather than silently
        // encoded as correct.
        assertEquals("tap", ir.lastAction?.type)
        assertEquals(PAY_NOW_TEST_TAG, ir.lastAction?.target)
    }

    // ------------------------------------------------------------------
    // 2. Capture exclusion (docs/07 §2.1, docs/13 §2.1)
    // ------------------------------------------------------------------

    /**
     * The privacy-critical contract, end to end: on a support screen the
     * snapshot is the RETAINED pre-support IR, never the support screen's own
     * content — while the event timeline is never scrubbed.
     *
     * **The assertion that gives this test its teeth** is the raw-tree one: at
     * the moment the retained IR still says `PaymentScreen`, the collector's
     * raw capture demonstrably contains `HelpScreen`'s own root testTag. That
     * is what separates "exclusion actively refused to overwrite the retained
     * IR" from "no capture happened to arrive" — the latter would satisfy every
     * other assertion here while proving nothing. `AppStateManager`'s own
     * `isNewCapture` guard makes the difference invisible from the IR alone.
     *
     * Events are asserted in the opposite direction on purpose: docs/01 §5's
     * roles table ("captured for navigation history only"), docs/07 §6
     * ("events are not deltas") and docs/13 §2.1's canonical body — which ships
     * `nav -> HelpScreen` and `tap "Call Support"` alongside a `PaymentScreen`
     * `screen_context` — all say the exclusion scopes to the snapshot and
     * stops there. Note the split: the `nav` entry is asserted on the session
     * body, the `tap` on the live ring buffer, because `sessionCreateBody`
     * currently cannot see the newest events. That is a separate reported
     * defect, documented at the assertion rather than smoothed over.
     */
    @Test
    fun `on HelpScreen the retained screen stays PaymentScreen while events are never scrubbed`() {
        startAppOnRealNavGraph()
        navigateTo(AppRoute.PAYMENT.route)
        declineCanonicalPayment()
        navigateTo(AppRoute.SUPPORT.route)

        // The real inline CTA on the real HelpScreen. Tapped through the
        // production surface because its `tap` entry is part of what is
        // asserted below; the permission gate behind it never reaches a real
        // service under Robolectric, which this test does not depend on.
        composeTestRule.onNodeWithTag(CALL_SUPPORT_TEST_TAG).performClick()
        settle(millis = 500)

        // (a) The live navigation position really did move to the support
        //     surface -- without this the rest is vacuous, since an IR that
        //     "stayed on PaymentScreen" proves nothing if the app never left it.
        val state = appState.state.value
        assertEquals(SupportDestination.ROUTE, state.route)
        assertEquals(SupportDestination.FLOW, state.flow)

        // (b) The raw capture DID walk HelpScreen. Exclusion is a filter over
        //     real input, not an absence of input.
        val rawTestTags = latestRawTree().roots.flatMap(::allTestTags)
        assertTrue(
            "expected HelpScreen's own root testTag in the RAW captured tree, got $rawTestTags -- " +
                "without a real support-screen capture to refuse, the retention assertion below " +
                "would pass for the wrong reason",
            SupportDestination.ROOT_TEST_TAG in rawTestTags,
        )

        // (c) ...and the retained IR is still the pre-support operational
        //     screen, dialog and all (docs/13 §2.1: "the screen_context is
        //     PaymentScreen, not HelpScreen").
        val ir = retainedScreen()
        assertEquals(PaymentDestination.ROUTE, ir?.screen)
        assertEquals(PaymentDestination.FLOW, ir?.flow)
        assertTrue(
            "the retained IR must still carry the decline dialog the merchant is calling about",
            ir!!.components.any { it.role == ScreenComponentRole.DIALOG },
        )

        // (d) No trace of the support screen leaked into the IR's labels or
        //     values -- a component-level check, stronger than the screen name
        //     alone, and the one that would catch a partial overwrite.
        val irText = ir.components.flatMap { component ->
            listOf(component.label) + when (component) {
                is ScreenComponent.AmountField -> listOf(component.value)
                is ScreenComponent.Recipient -> listOf(component.value)
                is ScreenComponent.ListItem -> listOf(component.value)
                else -> emptyList()
            }
        }
        assertTrue(
            "support-screen content leaked into the retained IR: $irText",
            irText.none { CALL_SUPPORT_LABEL in it || SUPPORT_FAQ_HEADING in it },
        )

        // (e) The session body -- the exact production method CallViewModel
        //     .startCall() calls -- carries that same retained screen.
        val body = appState.sessionCreateBody(DEMO_USER_ID)
        val screenContext = body.screenContext
        assertNotNull("screenContext must carry the retained operational screen, not null", screenContext)
        assertEquals(
            PaymentDestination.ROUTE,
            screenContext!!["screen"]?.jsonPrimitive?.content,
        )

        // (f) Events are NOT scrubbed. The `nav -> HelpScreen` entry is checked
        //     in the session body; the `tap` is checked on the live ring buffer
        //     instead, for the reason spelled out just below -- which is a
        //     reported defect, not a shortcut.
        val eventNames = body.recentEvents.map { it.name }
        val navToHelp = body.recentEvents.lastOrNull { it.type == "nav" && it.name == SupportDestination.ROUTE }
        assertNotNull("the nav -> HelpScreen event must survive into recent_events, got $eventNames", navToHelp)
        assertEquals(PaymentDestination.ROUTE, navToHelp!!.from)

        // The tap on Call Support is asserted against the LIVE ring buffer, not
        // against `body.recentEvents`, and the difference is a finding rather
        // than a convenience.
        //
        // `sessionCreateBody` reads `state.value.recentEvents` — a list
        // snapshotted the last time `AppStateManager`'s combine ran, i.e. on the
        // last capture or route change. A tap that changes nothing on screen
        // wakes neither, so the newest events can be missing from the body. That
        // is not Robolectric-specific: `CallViewModel.onPermissionDecision`
        // calls `startCall()` synchronously, so on a device the body is built in
        // the same frame as the tap, long before any capture could refresh the
        // snapshot — the merchant's "Call Support" tap, which docs/13 §2.1's
        // canonical `recent_events` carries explicitly, is omitted from every
        // real session body. Reported with this task; deliberately NOT worked
        // around here, and deliberately not asserted as correct.
        //
        // What this test does prove is the thing capture exclusion is actually
        // about: a tap performed ON a support surface is recorded in full,
        // attributed to that surface, never scrubbed (docs/01 §5's "captured for
        // navigation history only", docs/07 §6's "events are not deltas").
        val liveTimeline = eventTracker.recent()
        val callSupportTap = liveTimeline.filterIsInstance<AppEvent.Tap>()
            .singleOrNull { it.name == CALL_SUPPORT_TEST_TAG }
        assertNotNull(
            "the tap on the support screen's Call Support CTA must be recorded, not scrubbed. " +
                "Timeline was ${liveTimeline.map { it.name }}",
            callSupportTap,
        )
        assertEquals(
            "the tap must be attributed to the support screen it happened on",
            SupportDestination.ROUTE,
            callSupportTap!!.screen,
        )
        assertTrue(
            "recent_events must be oldest-first (session_create_request.v1.json), timestamps were " +
                body.recentEvents.map { it.ts },
            body.recentEvents.map { it.ts }.zipWithNext().all { (older, newer) -> older <= newer },
        )
    }

    // ------------------------------------------------------------------
    // 3. The wire: sequence, seq monotonicity, snapshot-vs-delta
    // ------------------------------------------------------------------

    /**
     * What the agent actually receives, in order, through the production
     * publisher and `:voice`'s own binding adapter.
     *
     * Three of `ScreenContextPublisher`'s documented branches (docs/07 §6) are
     * driven here by real UI, in the order a merchant would produce them:
     *
     * 1. **Bind-time -> full `ctx.snapshot`.** The publisher has no memory of a
     *    previously published screen, so its first screen-state message is
     *    always a snapshot (that class's "genesis anchor" kdoc). `seq` starts
     *    at `GENESIS_SEQ + 1` = 1.
     * 2. **Same screen, changed components -> `ctx.delta`.** Dismissing the
     *    decline dialog is docs/07 §6's own canonical delta example. The
     *    dialog leaves via `removed` rather than `changed` because production
     *    builds components from the live tree, where a dismissed dialog is
     *    simply absent — the schema's `removed` array is exactly that case.
     *    `base_seq` must point at the snapshot's `seq`.
     * 3. **Navigate to an excluded route -> nothing.** This is capture
     *    exclusion arriving at the wire: `AppContextState.screen` structurally
     *    cannot change while the merchant is on a support route, so the
     *    publisher's diff sees no change and emits no screen-state frame — the
     *    "no second, duplicated implementation here" claim both
     *    `AppStateManager`'s and `ScreenContextPublisher`'s kdocs make, checked
     *    rather than asserted in prose. The `nav` event still ships, immediate
     *    and undebounced.
     * 4. **A tap ON the excluded route -> still only an event.** The same
     *    exclusion, now with the merchant interacting rather than arriving, and
     *    the frame that lets the `seq` assertion detect a counter that stops
     *    advancing (see the comment at that step).
     *
     * Publishing while the merchant stands on `PaymentScreen` is a real
     * in-call position, not a contrivance: `CallViewModel.onCleared`'s kdoc is
     * explicit that navigating away from `HelpScreen` mid-call must not end the
     * call, and docs/17 §2.4's own exit criterion names "a mid-call navigation"
     * as a thing that must work.
     */
    @Test
    fun `the in-call frame sequence is snapshot then delta then a bare nav event on the excluded route`() {
        startAppOnRealNavGraph()
        navigateTo(AppRoute.PAYMENT.route)
        declineCanonicalPayment()

        val channel = FakeContextChannel()
        startCallScopedPublisher(channel)

        // -- 1. bind time --------------------------------------------------
        assertTrue("the publisher shipped nothing at bind time", channel.frames.isNotEmpty())
        val snapshot = channel.frames.first()
        assertEquals("ctx.snapshot", snapshot.type)
        assertEquals(FIRST_SEQ, snapshot.seq)
        assertEquals(PaymentDestination.ROUTE, snapshot.payload["screen"]?.jsonPrimitive?.content)
        assertTrue(
            "the bind-time snapshot must carry the decline dialog -- it is the whole reason for the call",
            snapshot.payload.componentRoles().contains("dialog"),
        )
        assertEquals(
            "bind time must produce exactly one frame (the full snapshot), got ${channel.describe()}",
            1,
            channel.frames.size,
        )

        // -- 2. dismissing the dialog: same screen, changed components ------
        composeTestRule.onNodeWithTag(DIALOG_DISMISS_TEST_TAG).performClick()
        settle(millis = 500)

        val afterDismiss = channel.frames.drop(1)
        assertEquals(
            "dismissing the dialog must produce exactly one screen-state frame, got ${channel.describe()}",
            1,
            afterDismiss.size,
        )
        val delta = afterDismiss.single()
        assertEquals(
            "same screen with changed components is a delta, not a snapshot (docs/07 §6)",
            "ctx.delta",
            delta.type,
        )
        assertEquals(
            "base_seq must point at the last snapshot or delta (docs/13 §4)",
            FIRST_SEQ,
            delta.payload["base_seq"]?.jsonPrimitive?.long,
        )
        val removedRoles = delta.payload["removed"]!!.jsonArray
            .map { it.jsonObject["role"]!!.jsonPrimitive.content }
        assertTrue(
            "the dismissed dialog must leave the wire as a removal, removed roles were $removedRoles",
            "dialog" in removedRoles,
        )

        // -- 3. navigating to the excluded support route -------------------
        val beforeNav = channel.frames.size
        navigateTo(AppRoute.SUPPORT.route)

        val afterNav = channel.frames.drop(beforeNav)
        assertTrue(
            "navigating to an excluded route must publish NO screen-state frame -- the merchant's " +
                "support screen reached the agent. Frames were ${channel.describe()}",
            afterNav.none { it.type == "ctx.snapshot" || it.type == "ctx.delta" },
        )
        val navEvent = afterNav.singleOrNull { it.type == "ctx.event" }
        assertNotNull("the nav event must still ship, undebounced. Frames were ${channel.describe()}", navEvent)
        assertEquals("nav", navEvent!!.payload["type"]?.jsonPrimitive?.content)
        assertEquals(SupportDestination.ROUTE, navEvent.payload["name"]?.jsonPrimitive?.content)
        assertEquals(PaymentDestination.ROUTE, navEvent.payload["from"]?.jsonPrimitive?.content)

        // -- 4. a tap ON the excluded route: an event, still no snapshot ---
        // This step is also what gives the seq assertion below any teeth. With
        // the nav event as the last frame, a `nextSeq` that stopped advancing
        // on events would still leave a gapless list -- confirmed by mutation:
        // dropping the `++` in `publishEvent` failed nothing until a SECOND
        // event existed to collide with the first.
        val beforeTap = channel.frames.size
        composeTestRule.onNodeWithTag(TEXT_CHAT_TEST_TAG).performClick()
        settle(millis = 500)

        val afterTap = channel.frames.drop(beforeTap)
        assertTrue(
            "a tap on the support screen must not publish a screen-state frame either. " +
                "Frames were ${channel.describe()}",
            afterTap.none { it.type == "ctx.snapshot" || it.type == "ctx.delta" },
        )
        val tapEvent = afterTap.singleOrNull { it.type == "ctx.event" }
        assertNotNull("the support-screen tap must ship as a ctx.event. Frames were ${channel.describe()}", tapEvent)
        assertEquals("tap", tapEvent!!.payload["type"]?.jsonPrimitive?.content)
        assertEquals(
            "the tap must be attributed to the support screen it happened on -- events carry the " +
                "live route even though snapshots never do",
            SupportDestination.ROUTE,
            tapEvent.payload["screen"]?.jsonPrimitive?.content,
        )

        // -- seq: one client-monotonic counter spanning all three types ----
        val seqs = channel.frames.map { it.seq }
        assertEquals(
            "seq must be gapless and monotonic from GENESIS_SEQ + 1 across snapshot/delta/event " +
                "(docs/08 §3.2) -- a gap makes the backend discard and re-request. Frames were ${channel.describe()}",
            (FIRST_SEQ until FIRST_SEQ + seqs.size).toList(),
            seqs,
        )
    }

    // ------------------------------------------------------------------
    // 4. The capture-policy drift guard
    // ------------------------------------------------------------------

    /**
     * The cross-check T7b's review asked T8 to try for: three hand-maintained
     * tables decide whether a route is captured, and nothing made them agree.
     *
     * - each feature's own `<Feature>Destination.IS_CAPTURED` (public, and what
     *   `AppRoute` assembles),
     * - `NavigationTracker.ROUTE_TO_FLOW` (`private`) — which routes carry the
     *   `support` flow,
     * - `AppStateManager.EXCLUDED_ROUTES`/`EXCLUDED_FLOW` (`internal` to
     *   `:core:screencontext`, whose own comment notes it duplicates
     *   `NavigationTracker`'s table by hand).
     *
     * The last two ARE compared directly, in `AppStateManagerTest`'s
     * `every excluded route still resolves to the support flow in
     * NavigationTracker` — `flowFor` is `internal`, not private, and
     * `NavigationTrackerTest` already calls it, so nothing had to be widened.
     * An earlier version of this kdoc claimed that comparison was impossible
     * without widening `ROUTE_TO_FLOW`; that was simply wrong.
     *
     * That table cross-check and this test cover different gaps, which is why
     * both exist. `isExcludedFromCapture` is `route in EXCLUDED_ROUTES || flow
     * == EXCLUDED_FLOW`, so a route dropped from one table alone is still
     * excluded by the other — the cross-check catches the tables disagreeing,
     * including for `ConversationOverlay`, which has no `AppRoute` entry and is
     * therefore invisible to the sweep below. The dangerous case *it* cannot
     * see is a support surface missing from both tables.
     *
     * This guard therefore checks behaviour against the feature's own public
     * declaration, driving every route in the real `AppNavHost` through
     * the real `NavigationTracker` and asserting what [AppStateManager]
     * actually retained. It fails if any of the three drift: a support route
     * absent from both internal tables, a route wrongly added to
     * `EXCLUDED_ROUTES`, or a feature that flips `IS_CAPTURED` without the
     * pipeline following. (A `ROUTE_TO_FLOW` row that stops resolving to
     * `support` is deliberately NOT on that list: `isExcludedFromCapture` is an
     * OR, so `EXCLUDED_ROUTES` still catches it and this test would not fire.
     * `AppStateManagerTest`'s table cross-check is what covers that case.)
     *
     * **What it still does not catch,** stated plainly: a *new* support surface
     * that is added to the nav graph but declares `IS_CAPTURED = true` (or is
     * added to `AppRoute` with a fresh flow name that is neither `support` nor
     * excluded). No test of these tables can catch that, because at that point
     * all three agree with each other and all three are wrong together.
     */
    @Test
    fun `every route in the real nav graph is captured or excluded exactly as its feature declares`() {
        startAppOnRealNavGraph()

        // Start on a captured route so the retained IR is non-null and the
        // "did not advance" half below has a concrete value to hold at.
        navigateTo(AppRoute.PAYMENT.route)
        assertEquals(
            "precondition: a captured route must retain its own IR before the sweep begins",
            PaymentDestination.ROUTE,
            retainedScreen()?.screen,
        )

        AppRoute.entries.forEach { destination ->
            val retainedBefore = retainedScreen()
            navigateTo(destination.route)

            assertEquals(
                "NavigationTracker must report ${destination.name}'s live route regardless of capture policy",
                destination.route,
                appState.state.value.route,
            )

            if (destination.isCaptured) {
                assertEquals(
                    "${destination.name} declares IS_CAPTURED = true, but AppStateManager refused to " +
                        "retain it -- its route or flow is being treated as excluded",
                    destination.route,
                    retainedScreen()?.screen,
                )
            } else {
                assertEquals(
                    "${destination.name} declares IS_CAPTURED = false, but AppStateManager overwrote the " +
                        "retained operational screen with it -- capture exclusion does not know about " +
                        "this route (docs/07 §2.1)",
                    retainedBefore,
                    retainedScreen(),
                )
            }
        }
    }

    // ------------------------------------------------------------------
    // Driving the real app
    // ------------------------------------------------------------------

    /**
     * Re-hosts the real nav graph with a test-reachable controller and pulls
     * the pipeline components that have no [MainActivity] field out of the real
     * Hilt graph. See this class's kdoc for why the re-host is necessary and
     * why it is not a rewiring.
     */
    private fun startAppOnRealNavGraph() {
        composeTestRule.waitForIdle()

        val entryPoint = EntryPointAccessors.fromApplication(
            composeTestRule.activity.applicationContext,
            ScreenContextCanaryEntryPoint::class.java,
        )
        // The same `@Singleton` instances CallViewModel is injected with, and
        // the same per-call Provider VoiceCallService.resolveContextPublisher()
        // reads (a fresh publisher per call restarts seq at GENESIS_SEQ + 1).
        appState = entryPoint.appStateManager()
        publishers = entryPoint.screenContextPublishers()
        eventTracker = entryPoint.eventTracker()

        composeTestRule.runOnUiThread {
            composeTestRule.activity.setContent {
                val controller = rememberNavController()
                navController = controller
                LaunchedEffect(Unit) {
                    composeTestRule.activity.navigationTracker.bind(controller)
                }
                VyaparTheme { AppNavHost(navController = controller) }
            }
        }
        settle(millis = 500)
    }

    private fun navigateTo(route: String) {
        composeTestRule.runOnUiThread { navController.navigate(route) }
        settle(millis = 500)
    }

    /** docs/01 §7 steps 1-2: ₹245 to Amazon Business, declined on the daily limit. */
    private fun declineCanonicalPayment() {
        composeTestRule.onNodeWithTag(AMOUNT_TEST_TAG).performTextInput(CANONICAL_AMOUNT)
        composeTestRule.onNodeWithTag(PAY_NOW_TEST_TAG).performClick()
        settle(millis = 500)
    }

    /**
     * What `VoiceCallService.resolveContextPublisher()` does when a call comes
     * up: a fresh [ScreenContextPublisher] out of the `Provider`, wrapped in
     * `:voice`'s production [ScreenContextPublisherBinding], started on a
     * call-scoped scope.
     */
    private fun startCallScopedPublisher(channel: ContextChannel) {
        ScreenContextPublisherBinding(publishers.get()).start(channel, callScope)
        settle(millis = 500)
    }

    private fun retainedScreen(): ScreenContextIr? = appState.state.value.screen

    /**
     * `withTimeout`, never a bare `first()`: `tree` is a `filterNotNull`ed
     * StateFlow that simply never emits if capture regresses, and a hung CI job
     * names its own cause far worse than a failed assertion does — the exact
     * regression `MainActivityScreenContextTest`'s reviewer reproduced (150s+
     * hang instead of a failure).
     */
    private fun latestRawTree() = runBlocking {
        withTimeout(CAPTURE_TIMEOUT_MILLIS) {
            composeTestRule.activity.uiTreeCollector.tree.first()
        }
    }

    /**
     * Robolectric's main looper is PAUSED, so `waitForIdle()` alone runs only
     * what is already due. Advancing the clock is what lets
     * [ChildWindowTracker]'s coalesced scan, `UiTreeCollector`'s 300 ms
     * debounce, `AppStateManager`'s combine and the publisher's two collectors
     * all actually run — the same scheduling they use on a device, driven
     * deterministically.
     */
    private fun settle(millis: Long) {
        composeTestRule.waitForIdle()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(millis))
        composeTestRule.waitForIdle()
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap(::allTestTags)

    /**
     * Stands in for `WebRtcContextChannel`, decoding each frame on arrival so a
     * failure message can print the whole conversation. Robolectric runs every
     * publisher collector on the one paused main looper, so a plain list needs
     * no synchronization here.
     */
    private class FakeContextChannel : ContextChannel {
        val frames = mutableListOf<JsonObject>()

        override suspend fun send(bytes: ByteArray) {
            frames += Json.parseToJsonElement(String(bytes, Charsets.UTF_8)).jsonObject
        }

        fun describe(): String = frames.joinToString(prefix = "[", postfix = "]") { "${it.type}#${it.seq}" }
    }

    private companion object {
        /**
         * testTags owned by `:feature:payments`/`:feature:support`, where they
         * are `private`/`internal` and invisible from `:app`. Repeated as
         * literals rather than widened for a test — docs/03 §4's tag table is
         * the shared contract they both encode, and drift fails loudly here.
         * The same convention [MainActivityDialogWindowCaptureTest] uses.
         */
        const val AMOUNT_TEST_TAG = "amount_input"
        const val PAY_NOW_TEST_TAG = "pay_now_cta"
        const val DIALOG_DISMISS_TEST_TAG = "daily_limit_dismiss_secondary"
        const val CALL_SUPPORT_TEST_TAG = "call_support_cta"
        const val TEXT_CHAT_TEST_TAG = "text_chat_secondary"

        /** Rajesh's over-limit vendor payment (docs/01 §7 step 1). */
        const val CANONICAL_AMOUNT = "245"

        /** `PaymentViewModel`'s own title for `ApiError.DAILY_LIMIT_EXCEEDED`. */
        const val DECLINE_DIALOG_TITLE = "Daily Limit Exceeded"

        /** `ApiError.DAILY_LIMIT_EXCEEDED.name` — the fixture's `error_code`. */
        const val DAILY_LIMIT_EXCEEDED = "DAILY_LIMIT_EXCEEDED"

        /** `HelpScreen`'s inline CTA label, and the FAQ section heading. */
        const val CALL_SUPPORT_LABEL = "Call Support"
        const val SUPPORT_FAQ_HEADING = "Frequently asked questions"

        /**
         * `CallViewModel.DEMO_USER_ID` is `internal` to `:feature:support`, so
         * the canonical demo merchant (`backend/scripts/seed.py`'s
         * `MERCHANT_ID`) is repeated here rather than widened for a test.
         */
        const val DEMO_USER_ID = "usr_rajesh01"

        /** `ScreenContextPublisher`'s `GENESIS_SEQ + 1`, its first-ever `seq`. */
        const val FIRST_SEQ = 1L

        /** Far above the sub-second real capture; see [latestRawTree] for why it exists. */
        const val CAPTURE_TIMEOUT_MILLIS = 10_000L
    }
}

/** The `ctx.*` envelope's three fixed fields (docs/07 §6), read positionally off a decoded frame. */
private val JsonObject.type: String get() = this["type"]!!.jsonPrimitive.content
private val JsonObject.seq: Long get() = this["seq"]!!.jsonPrimitive.long
private val JsonObject.payload: JsonObject get() = this["payload"]!!.jsonObject

private fun JsonObject.componentRoles(): List<String> =
    this["components"]!!.jsonArray.map { it.jsonObject["role"]!!.jsonPrimitive.content }

/**
 * Reaches the two pipeline components that have no field on [MainActivity]:
 * the `@Singleton` [AppStateManager] `CallViewModel` is injected with, and the
 * per-call `Provider<ScreenContextPublisher>` `VoiceCallService` holds. Pulling
 * them from the real graph rather than constructing them is what makes this a
 * canary over production wiring instead of over a parallel assembly.
 */
@EntryPoint
@InstallIn(SingletonComponent::class)
internal interface ScreenContextCanaryEntryPoint {
    fun appStateManager(): AppStateManager
    fun screenContextPublishers(): Provider<ScreenContextPublisher>
    fun eventTracker(): EventTracker
}
