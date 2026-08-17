import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

// The demo bearer token the app sends (DemoAuthInterceptor, :core:network)
// comes from OUTSIDE the tracked tree: `vyaparpay.demoBearerToken` in the
// gitignored android/local.properties, or the VYAPARPAY_DEMO_BEARER_TOKEN
// environment variable, else empty (= no Authorization header at all, the
// interceptor's documented "absent or blank" behaviour). Until 2026-08-17
// the value lived in AndroidManifest.xml itself behind a REPLACE_WITH_...
// placeholder that had to be hand-edited before every device run -- which
// left a live, signed JWT sitting as an uncommitted diff in a TRACKED file,
// one `git add -A` away from history (flagged CRITICAL by a security
// review; it was caught before it ever landed, twice). A manifest
// placeholder moves the secret to where secrets already live here.
val localProperties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}
val demoBearerToken: String = (
    localProperties.getProperty("vyaparpay.demoBearerToken")
        ?: System.getenv("VYAPARPAY_DEMO_BEARER_TOKEN")
        ?: ""
    ).trim()

android {
    namespace = "com.vyaparpay"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        applicationId = "com.vyaparpay"
        minSdk = libs.versions.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
        versionCode = 1
        versionName = "0.1.0"
        manifestPlaceholders["demoBearerToken"] = demoBearerToken
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }

    testOptions {
        // MainActivityScreenContextTest (Phase-4 T8a) drives the real
        // MainActivity + AppNavHost through real Compose semantics under
        // Robolectric, matching the pattern :feature:payments's canary tests
        // already use.
        unitTests {
            isIncludeAndroidResources = true
        }
    }

    packaging {
        resources {
            // kotlinx.coroutines ships duplicate licence markers that otherwise
            // collide during packaging once more than one artifact carries them.
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // :app is the assembly root — the one module allowed to reach every tier.
    implementation(project(":feature:dashboard"))
    implementation(project(":feature:payments"))
    implementation(project(":feature:support"))
    implementation(project(":voice"))
    implementation(project(":core:ui"))
    // Phase-4 T8a: MainActivity injects UiTreeCollector/NavigationTracker
    // directly (see MainActivity's own kdoc) -- :app previously only reached
    // :core:screencontext transitively (implementation, not api, on every
    // :feature:* edge), which hid those types from :app's own compile
    // classpath.
    implementation(project(":core:screencontext"))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.navigation.compose)

    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)

    // :core:ui exports the BOM, but :app names versionless Compose coordinates
    // of its own — declaring the platform here means those resolve from an
    // explicit constraint rather than a transitively inherited one.
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui.tooling.preview)
    debugImplementation(libs.androidx.compose.ui.tooling)

    testImplementation(libs.junit)

    // MainActivityScreenContextTest (Phase-4 T8a): the first test that boots
    // the real Hilt graph + real Compose content together outside a
    // feature-scoped canary.
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    testImplementation(libs.robolectric)
    testImplementation(libs.hilt.android.testing)
    kspTest(libs.hilt.compiler)
}
