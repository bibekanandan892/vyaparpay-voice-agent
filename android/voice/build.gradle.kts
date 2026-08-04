plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
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
    implementation(project(":core:screencontext"))

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

    testImplementation(libs.junit)
    testImplementation(libs.turbine)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}
