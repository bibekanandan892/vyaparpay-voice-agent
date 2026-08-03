# android/ — VyaparPay Android App

Kotlin + Jetpack Compose merchant app with the floating voice-support button.
Architecture: [docs/03-android-architecture.md](../docs/03-android-architecture.md).

## Module map

The nine modules of [docs/03 §1](../docs/03-android-architecture.md), with the
dependencies each one actually declares:

| Module | Depends on |
|---|---|
| `:app` | `:feature:dashboard`, `:feature:payments`, `:feature:support`, `:voice`, `:core:ui` |
| `:core:ui` | `:core:analytics` |
| `:core:network` | `:core:analytics` |
| `:core:analytics` | — |
| `:core:screencontext` | `:core:analytics`, `:core:network` |
| `:feature:dashboard` | `:core:ui`, `:core:network`, `:core:screencontext` |
| `:feature:payments` | `:core:ui`, `:core:network`, `:core:screencontext` |
| `:feature:support` | `:voice`, `:core:ui`, `:core:network` |
| `:voice` | `:core:analytics`, `:core:network`, `:core:screencontext` |

### The dependency rule is enforced, not documented

`docs/03 §1` fixes one rule: **features depend on `:core:*`, never on each
other; `:voice` depends on `:core:*` only; `:feature:support` is the single
permitted feature→`:voice` edge.** The `checkModuleDependencyRules` task in
[build.gradle.kts](build.gradle.kts) walks every module's `api`,
`implementation`, `compileOnly` and `runtimeOnly` configurations, collects the
project-to-project edges, and fails the build with the offending edge and the
reason it is not allowed. It is wired into every module's `check` and `preBuild`,
so an illegal edge cannot survive an `assembleDebug` either.

```
./gradlew checkModuleDependencyRules
```

## Building

```
./gradlew :app:assembleDebug     # debug APK
./gradlew testDebugUnitTest      # JVM unit tests, every module
./gradlew lint                   # Android Lint
```

CI runs all four in [.github/workflows/android.yml](../.github/workflows/android.yml)
and uploads `app-debug.apk` as a workflow artifact — currently the only way to
get an installable build onto a phone, since no one on the project has a local
Android SDK.

## Version pinning

Every version lives in [gradle/libs.versions.toml](gradle/libs.versions.toml)
and is pinned exactly — no ranges, no dynamic versions.

The constraint that shapes the whole catalog is **AGP 8.13.2**, the last stable
8.x, whose maximum API level is 36.1. AndroidX artifacts publish a `minCompileSdk`
in their AAR metadata and AGP hard-fails below it, so every AndroidX pin here is
the newest release that still fits under `compileSdk 36`. `androidx.core` 1.19.0,
for instance, already requires `compileSdk 37` and AGP 9.1.0, and is therefore
out of reach until this project moves to AGP 9.

Kotlin is 2.3.21 rather than the newest 2.x specifically because the Kotlin
Gradle plugin's published compatibility table brackets our AGP: KGP 2.2.x tops
out at AGP 8.11.1, which would put 8.13.2 outside the supported range.

The Gradle wrapper jar **is committed**. CI needs a wrapper it can run before it
has resolved anything, and `gradle/actions/setup-gradle` validates the jar
against Gradle's published checksums on every run, so the usual argument against
committing a binary — that nobody can tell what it is — does not apply here.

## Status

This is the Phase-3 scaffold: the module graph, the build, and the enforcement
around them. The voice components of [docs/03 §3](../docs/03-android-architecture.md)
— `VoiceCallService`, `WebRtcClient`, `SignalingClient`, `CallStateMachine`,
`UiTreeCollector`, `SemanticSnapshotBuilder`, `ScreenContextPublisher` — and the
five seeded demo screens of §4 land in the tasks that follow; see
[docs/17-roadmap.md](../docs/17-roadmap.md).

Two consequences of that worth knowing:

- **No permissions are declared yet** beyond `INTERNET` (owned by
  `:core:network`). `RECORD_AUDIO`, `FOREGROUND_SERVICE_MICROPHONE` and
  `POST_NOTIFICATIONS` arrive in `:voice`'s manifest alongside the service that
  needs them.
- **`io.github.webrtc-sdk:android` is pinned in the catalog but consumed by no
  module.** Pulling a ~30 MB native AAR into every CI run before a single line
  imports `org.webrtc` would buy build time and a packaging surface for nothing.
