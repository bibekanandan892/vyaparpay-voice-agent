plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.vyaparpay.feature.payments"
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
        // PaymentScreenTest drives real Compose semantics (typing, tapping,
        // asserting dialog/snackbar text) through Robolectric so it runs as a
        // JVM `testDebugUnitTest`, matching what android.yml's CI actually
        // gates on — there is no connectedAndroidTest job, and no device in
        // this environment to run one against.
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

    // PaymentViewModel: StateFlow + viewModelScope, and PaymentRoute's
    // hiltViewModel()-free viewModel() + collectAsStateWithLifecycle().
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)

    testImplementation(libs.junit)

    // PaymentViewModelTest: StandardTestDispatcher + Turbine, docs/03 §6's
    // ViewModel-unit-test layer.
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.turbine)

    // PaymentScreenTest: Compose semantics assertions under Robolectric
    // (isIncludeAndroidResources above), docs/03 §6's Compose-UI-test layer.
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    testImplementation(libs.androidx.activity.compose)
    testImplementation(libs.robolectric)
}
