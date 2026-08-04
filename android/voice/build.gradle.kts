plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    // Phase-4 T8b: VoiceCallService is now an @AndroidEntryPoint — it is the
    // one place a real ScreenContextPublisher can be obtained for a call, and
    // a Service can only be field-injected through Hilt's generated base
    // class. Same plugin pair :core:screencontext took on in T8a.
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

android {
    namespace = "com.vyaparpay.voice"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        minSdk = libs.versions.minSdk.get().toInt()
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // docs/03 §1: :voice is a mid-tier module — above :core:*, below features —
    // so :feature:support can reach the call machinery without a feature-to-
    // feature edge. It may depend on :core:* and nothing else; the rule task in
    // the root build script fails the build if that ever stops being true.
    implementation(project(":core:analytics"))
    implementation(project(":core:network"))

    // `api`, not `implementation`, since Phase-4 T8b: :voice's own public
    // surface now names :core:screencontext types — CallContextPublisher.start
    // takes a ContextChannel, and WebRtcContextChannel *is* a ContextChannel.
    // Hiding the supertype of a public class behind `implementation` is the
    // classic way to hand consumers a type they cannot resolve; the same
    // reasoning :core:screencontext's own `api(project(":core:analytics"))`
    // gives ("both appear in AppContextState's public signature, hence
    // `api`"). The architectural edge is unchanged and still :voice ->
    // :core:*, which is what checkModuleDependencyRules actually checks.
    api(project(":core:screencontext"))

    // The maintained libwebrtc build (canon ADR-1, docs/03 §3.4). Deliberately
    // `implementation`, never `api`: WebRtcClient is the only org.webrtc
    // importer in the app, and none of :voice's public signatures may leak an
    // org.webrtc type (docs/03 §3.4's isolation rule).
    implementation(libs.webrtc.android)

    implementation(libs.okhttp)
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.kotlinx.serialization.json)

    // VoiceCallService task (docs/03 §3.3): LifecycleService as the doc's own
    // sketch specifies; core-ktx for NotificationCompat/NotificationChannelCompat/
    // ServiceCompat.startForeground, the AudioManager-adjacent glue confined to
    // the audio/ and notification/ packages (docs/03 §3.4's isolation rule,
    // applied to android.media/android.app.Notification the same way it is
    // applied to org.webrtc).
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.service)

    // Phase-4 T8b: @AndroidEntryPoint on VoiceCallService + the
    // Provider<ScreenContextPublisher> it field-injects. Nothing else in
    // :voice is Hilt-aware — CallController/VoiceCallCoordinator stay
    // constructor-injected by hand precisely so they remain JVM-testable.
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)

    testImplementation(libs.junit)
    testImplementation(libs.turbine)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}
