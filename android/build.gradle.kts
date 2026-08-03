plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.android.library) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.ksp) apply false
    alias(libs.plugins.hilt) apply false
}

// ---------------------------------------------------------------------------
// docs/03-android-architecture.md §1 — module dependency rule, enforced.
//
// The doc calls for "a build-time check (a dependency-analysis Gradle plugin)".
// This is that check, written directly against the Gradle model instead of
// pulling a third-party plugin: the rule is four lines long, the failure message
// is the part that actually matters, and a scaffold that cannot build offline
// is worse than one with a hand-rolled 60-line verification task.
//
// The rule:
//     :app       -> anything (it is the assembly root)
//     :feature:* -> :core:* only, except :feature:support which may also use :voice
//     :voice     -> :core:* only
//     :core:*    -> :core:* only
// ---------------------------------------------------------------------------

/**
 * Configurations whose project-to-project edges are architecturally meaningful.
 * Test configurations are deliberately excluded — a test reaching sideways is a
 * test-fixture smell, not the production coupling this rule exists to prevent.
 */
val architecturallySignificantConfigurations = setOf(
    "api",
    "implementation",
    "compileOnly",
    "runtimeOnly",
)

fun isEdgeAllowed(from: String, to: String): Boolean = when {
    from == ":app" -> true
    from.startsWith(":feature:") ->
        to.startsWith(":core:") || (from == ":feature:support" && to == ":voice")
    from == ":voice" -> to.startsWith(":core:")
    from.startsWith(":core:") -> to.startsWith(":core:")
    else -> false
}

fun explainViolation(from: String, to: String): String = when {
    from.startsWith(":feature:") && to.startsWith(":feature:") ->
        "features must not depend on each other; move the shared code into :core:*"
    from.startsWith(":feature:") && to == ":voice" ->
        ":feature:support is the only feature allowed to depend on :voice"
    from == ":voice" ->
        ":voice sits above :core:* and below features; it may only depend on :core:*"
    from.startsWith(":core:") ->
        ":core:* is the bottom tier; it may only depend on other :core:* modules"
    else ->
        "not an edge permitted by docs/03 §1"
}

val checkModuleDependencyRules = tasks.register("checkModuleDependencyRules") {
    group = "verification"
    description = "Fails the build if the module graph violates docs/03 §1."
}

gradle.projectsEvaluated {
    // Snapshot the graph once every module is configured, then hand a plain
    // immutable list to the task so execution never reaches back into the
    // project model.
    val edges: List<Pair<String, String>> = subprojects
        .flatMap { module ->
            module.configurations
                .filter { it.name in architecturallySignificantConfigurations }
                .flatMap { configuration -> configuration.dependencies }
                .filterIsInstance<ProjectDependency>()
                .map { dependency -> module.path to dependency.path }
        }
        .distinct()
        .sortedBy { (from, to) -> "$from -> $to" }
    val moduleCount = subprojects.size

    checkModuleDependencyRules.configure {
        doLast {
            val violations = edges.filterNot { (from, to) -> isEdgeAllowed(from, to) }
            if (violations.isNotEmpty()) {
                val detail = violations.joinToString(separator = "\n") { (from, to) ->
                    "  $from  ->  $to\n      ${explainViolation(from, to)}"
                }
                throw GradleException(
                    "Module dependency rule violated (docs/03-android-architecture.md §1):\n" +
                        detail + "\n",
                )
            }
            logger.lifecycle(
                "Module dependency rules OK — ${edges.size} project edges checked " +
                    "across $moduleCount projects.",
            )
        }
    }
}

subprojects {
    // The rule is a gate, not a report: `check` runs it, and so does any
    // assemble, because `preBuild` is upstream of every variant's compilation.
    tasks.matching { it.name == "check" || it.name == "preBuild" }.configureEach {
        dependsOn(checkModuleDependencyRules)
    }
}
