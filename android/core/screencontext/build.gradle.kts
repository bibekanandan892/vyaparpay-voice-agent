plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.vyaparpay.core.screencontext"
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
    // The two edges docs/03 §1 explicitly defends: the snapshot builder reads
    // the EventTracker ring buffer for `last_action` and the network layer's
    // last non-2xx for `last_api` — neither is derivable from a view-tree walk.
    // Both appear in AppContextState's public signature, hence `api`.
    api(project(":core:analytics"))
    api(project(":core:network"))

    // NavigationTracker.route/.flow are StateFlow (docs/03 §3.8).
    implementation(libs.kotlinx.coroutines.core)

    // NavController itself — the base artifact, not -compose: this module has
    // no Compose UI of its own, only a listener bound to a NavController that
    // :app constructs.
    implementation(libs.androidx.navigation.runtime)

    // @Inject/@Singleton on NavigationTracker, without the full Hilt plugin —
    // see NavigationTracker's kdoc for why the deeper Hilt wiring is deferred.
    implementation(libs.javax.inject)

    testImplementation(libs.junit)
}
