plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
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

    // RoleMapper resolves testTags through the SAME TestTagRoles.roleFor
    // :core:ui's lint check and every screen already use (docs/07 §5 rule 3
    // tier (a)) — not a parallel/divergent mapping.
    implementation(project(":core:ui"))

    // NavigationTracker.route/.flow are StateFlow (docs/03 §3.8); UiTreeCollector's debounce.
    implementation(libs.kotlinx.coroutines.core)

    // ScreenContextModule's app-scoped CoroutineScope is Dispatchers.Main-backed
    // (see that file's own kdoc for why); the `core` artifact only ships the
    // Dispatchers.Main *declaration* -- this is the artifact that supplies a
    // working Android Main-thread dispatcher at runtime instead of throwing
    // "Module with the Main dispatcher is missing".
    implementation(libs.kotlinx.coroutines.android)

    // NavController itself — the base artifact, not -compose: this module has
    // no Compose UI of its own, only a listener bound to a NavController that
    // :app constructs.
    implementation(libs.androidx.navigation.runtime)

    // UiTreeCollector: RootForTest/SemanticsOwner/SemanticsNode (`ui`),
    // Snapshot.registerApplyObserver (`runtime`) — see the libs.versions.toml
    // comment on androidx-compose-runtime for why buildFeatures.compose is
    // NOT enabled here despite these.
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.runtime)

    // ScreenContextIr.toJson()/DropLadder's JsonObject wire representation
    // (docs/07 §5 rule 8, §7) — hand-built, not derived, so no
    // kotlin.serialization compiler plugin is needed, only the runtime types.
    implementation(libs.kotlinx.serialization.json)

    // Phase-4 T8a: the full Hilt Gradle plugin (above) now processes this
    // module's @Inject constructors (NavigationTracker, AppStateManager,
    // ScreenContextPublisher, UiTreeCollector) and ScreenContextModule's
    // @Provides -- the plain javax.inject-only setup NavigationTracker's own
    // kdoc originally described is superseded by this; hilt-android already
    // re-exports javax.inject transitively, so the standalone dependency this
    // module carried for it is gone rather than duplicated.
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
}
