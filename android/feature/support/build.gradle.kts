plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
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

    // Phase-4 T8c: CallViewModel injects AppStateManager and calls
    // sessionCreateBody() -- the production injection point that finally makes
    // the aggregation half of the capture pipeline reachable. Declared
    // directly rather than leaned on transitively through :voice's
    // `api(project(":core:screencontext"))`: this module names the type
    // itself, and :feature:* -> :core:* is a first-class permitted edge
    // (docs/03 §1), so an implicit dependency would only obscure that.
    implementation(project(":core:screencontext"))

    // Phase-4 T8a: HelpViewModel is now @HiltViewModel, and SupportRoute
    // resolves it via hiltViewModel() (androidx-hilt-navigation-compose)
    // instead of the old DI-free viewModel() factory.
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.androidx.hilt.lifecycle.viewmodel.compose)

    // Phase-4 T8c, the call trigger:
    //  - activity-compose: rememberLauncherForActivityResult, so the runtime
    //    permission flow is composable-scoped and needs no MainActivity hook
    //    (see rememberCallPermissionGate's kdoc).
    //  - core-ktx: ContextCompat.startForegroundService and
    //    ActivityCompat.shouldShowRequestPermissionRationale -- the same
    //    AndroidX shims :voice already uses to keep SDK_INT branching out of
    //    call-start code.
    //  - serialization-json: the ACTION_START extra is a JSON-encoded
    //    SessionCreateRequestDto, and VoiceCallService decodes it with the
    //    matching bare Json.
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.core.ktx)
    implementation(libs.kotlinx.serialization.json)

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
