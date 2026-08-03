plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.vyaparpay.feature.support"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        minSdk = libs.versions.minSdk.get().toInt()
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // The one feature-to-:voice edge docs/03 §1 permits — and the reason :voice
    // is a mid-tier module rather than a tenth feature. Every other feature
    // reaching for :voice is a build failure, not a code-review catch.
    implementation(project(":voice"))

    implementation(project(":core:ui"))
    implementation(project(":core:network"))

    testImplementation(libs.junit)
}
