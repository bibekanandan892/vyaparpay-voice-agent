plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

android {
    namespace = "com.vyaparpay.core.analytics"
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

// The bottom of the graph: docs/03 §1 has :core:analytics depending on nothing
// project-wise. Hilt is added here (mirroring :core:network's own setup) so
// RingBufferEventTracker's @Inject constructor and AnalyticsModule's @Binds
// are real, resolvable Dagger metadata rather than annotations that merely
// compile — :app:assembleDebug needs that once ApiErrorReportingInterceptor
// (in :core:network) pulls EventTracker into the graph transitively.
dependencies {
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)

    testImplementation(libs.junit)
}
