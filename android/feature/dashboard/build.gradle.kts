plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
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

    // Phase-4 T8a: DashboardViewModel is now @HiltViewModel, and
    // DashboardRoute resolves it via hiltViewModel() (androidx-hilt-
    // navigation-compose) instead of the old DI-free viewModel() factory.
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.androidx.hilt.lifecycle.viewmodel.compose)

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
