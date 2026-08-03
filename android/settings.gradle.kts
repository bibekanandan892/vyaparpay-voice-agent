pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    // Modules must not declare their own repositories — every coordinate this
    // build resolves comes from the two repositories named here, so a review of
    // this file is a complete review of the build's supply chain.
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "vyaparpay-android"

// The nine modules of docs/03-android-architecture.md §1. The dependency rule
// that keeps this graph acyclic is enforced by the `checkModuleDependencyRules`
// task in build.gradle.kts, not by convention alone.
include(
    ":app",
    ":core:analytics",
    ":core:network",
    ":core:screencontext",
    ":core:ui",
    ":feature:dashboard",
    ":feature:payments",
    ":feature:support",
    ":voice",
)
