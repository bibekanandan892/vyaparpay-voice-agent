plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
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

    testImplementation(libs.junit)
    testImplementation(libs.turbine)
    testImplementation(libs.kotlinx.coroutines.test)
}
