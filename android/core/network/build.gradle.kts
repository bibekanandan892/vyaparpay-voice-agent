plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

android {
    namespace = "com.vyaparpay.core.network"
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
    // docs/03 §1: :core:network -> :core:analytics. The edge is real, not
    // aspirational — ApiErrorReporter writes the `api_error` timeline entries
    // that SemanticSnapshotBuilder later reads back as `last_api`. `api` rather
    // than `implementation` because EventTracker appears in ApiErrorReporter's
    // public constructor signature.
    api(project(":core:analytics"))

    // `api` because JsonObject appears in public signatures (ApiResult.Failure
    // .details, SessionCreateRequestDto.screenContext) — consumers pattern-
    // matching a Failure need the type on their compile classpath.
    api(libs.kotlinx.serialization.json)

    implementation(libs.okhttp)
    implementation(libs.retrofit)
    implementation(libs.retrofit.kotlinx.serialization)
    // Retrofit's suspend-fun support resolves kotlinx-coroutines at runtime
    // without declaring it in its POM; the dependency is real, so it is
    // declared here rather than inherited by accident.
    implementation(libs.kotlinx.coroutines.core)

    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)

    testImplementation(libs.junit)
    testImplementation(libs.okhttp.mockwebserver)
}
