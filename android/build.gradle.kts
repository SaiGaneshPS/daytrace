// DT-19: plugin versions live in gradle/libs.versions.toml.
plugins {
    alias(libs.plugins.android.application) apply false
    // AGP 9 compiles Kotlin itself but bundles an older Kotlin; putting the Kotlin plugin on the classpath
    // makes the whole build use one Kotlin version, the one the Compose compiler plugin below needs.
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
