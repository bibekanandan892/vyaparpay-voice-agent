# android/ — VyaparPay Android App

Kotlin + Jetpack Compose merchant app with the floating voice-support button.
Architecture: [docs/03-android-architecture.md](../docs/03-android-architecture.md).

Planned Gradle module map:

```
:app                      # shell, DI graph, navigation host
:core:ui                  # design system, Compose theme
:core:network             # Retrofit/OkHttp, API clients, error mapping
:core:analytics           # EventTracker — user-action timeline
:core:screencontext       # UiTreeCollector + SemanticSnapshotBuilder (signature feature)
:feature:dashboard        # merchant home: balance, settlements, orders
:feature:payments         # payments, vendor payouts, failure states
:feature:support          # floating SupportButton + ConversationOverlay
:voice                    # WebRtcClient (org.webrtc/libwebrtc), SignalingClient, CallStateMachine, VoiceCallService
```

Code lands in Phase 3 — see [docs/17-roadmap.md](../docs/17-roadmap.md).
