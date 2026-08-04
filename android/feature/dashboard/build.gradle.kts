plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.vyaparpay.feature.dashboard"
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
        // DashboardScreenTest drives real Compose semantics through
        // Robolectric, matching :feature:payments's build.gradle.kts.
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
    // docs/03 §1: features reach down into :core:* and never sideways.
    implementation(project(":core:ui"))
    implementation(project(":core:network"))
    implementation(project(":core:screencontext"))

    // DashboardViewModel: StateFlow + viewModelScope, and DashboardRoute's
    // hiltViewModel()-free viewModel() + collectAsStateWithLifecycle().
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)

    testImplementation(libs.junit)

    // DashboardViewModelTest: StandardTestDispatcher + Turbine, docs/03 §6's
    // ViewModel-unit-test layer.
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.turbine)

    // DashboardScreenTest: Compose semantics assertions under Robolectric
    // (isIncludeAndroidResources above), docs/03 §6's Compose-UI-test layer.
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    testImplementation(libs.androidx.activity.compose)
    testImplementation(libs.robolectric)
}
