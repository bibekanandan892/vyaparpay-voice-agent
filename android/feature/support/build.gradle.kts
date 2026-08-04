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

    testOptions {
        // HelpScreenTest drives real Compose semantics through Robolectric,
        // matching :feature:payments's build.gradle.kts.
        unitTests {
            isIncludeAndroidResources = true
        }
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

    // HelpViewModel: StateFlow + viewModelScope, and SupportRoute's
    // hiltViewModel()-free viewModel() + collectAsStateWithLifecycle().
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)

    testImplementation(libs.junit)

    // HelpViewModelTest: StandardTestDispatcher + Turbine, docs/03 §6's
    // ViewModel-unit-test layer.
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.turbine)

    // HelpScreenTest: Compose semantics assertions under Robolectric
    // (isIncludeAndroidResources above), docs/03 §6's Compose-UI-test layer.
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    testImplementation(libs.androidx.activity.compose)
    testImplementation(libs.robolectric)
}
