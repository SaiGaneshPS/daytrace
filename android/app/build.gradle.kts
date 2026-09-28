// DT-19: the app module. DT-25: the release signing key and the version, for signed APKs on GitHub Releases.
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp) // Room's code generator (DT-21)
}

// DT-25: the release key, from the environment (the release workflow) or android/keystore.properties (ignored by git,
// for a release built on this PC): storeFile, storePassword, keyAlias and keyPassword. Without one, a release build
// is left unsigned (app-release-unsigned.apk), as on every pull request.
val keyFile = rootProject.file("keystore.properties")
val keyProperties = Properties().apply { if (keyFile.isFile) keyFile.inputStream().use(::load) }

fun releaseKey(env: String, property: String): String? =
    providers.environmentVariable(env).orNull?.trim()?.takeIf { it.isNotEmpty() }
        ?: keyProperties.getProperty(property)?.trim()?.takeIf { it.isNotEmpty() }

val keyStore = releaseKey("DAYTRACE_KEYSTORE", "storeFile")
val keyStorePassword = releaseKey("DAYTRACE_KEYSTORE_PASSWORD", "storePassword")
val keyAliasName = releaseKey("DAYTRACE_KEY_ALIAS", "keyAlias")
val keyAliasPassword = releaseKey("DAYTRACE_KEY_PASSWORD", "keyPassword") ?: keyStorePassword // PKCS12: the same
val keyParts = mapOf(
    "DAYTRACE_KEYSTORE (storeFile)" to keyStore,
    "DAYTRACE_KEYSTORE_PASSWORD (storePassword)" to keyStorePassword,
    "DAYTRACE_KEY_ALIAS (keyAlias)" to keyAliasName,
)
val signed = keyParts.values.all { it != null }
if (!signed && keyParts.values.any { it != null }) { // half a key would quietly build an unsigned APK
    throw GradleException("A release key needs ${keyParts.keys.joinToString()}; missing: ${keyParts.filterValues { it == null }.keys.joinToString()}")
}
if (signed && !rootProject.file(keyStore!!).isFile) {
    throw GradleException(
        "No key file at ${rootProject.file(keyStore)}. In keystore.properties write the path with forward slashes " +
            "(D:/Hackathon/keys/daytrace-release.jks): a backslash there is an escape character.",
    )
}

// DT-25: a release's version comes from its tag (android-v0.2.1 gives 0.2.1), and its versionCode from that
// (major * 10000 + minor * 100 + patch), so every release installs over the one before. The same rule as the tag
// check in .github/workflows/android.yml: no leading zeros, at most 999.99.99, above 0.0.0.
val releaseVersion = providers.environmentVariable("DAYTRACE_VERSION").orNull?.trim()?.takeIf { it.isNotEmpty() }
val releaseCode = releaseVersion?.let { version ->
    val parts = Regex("(0|[1-9][0-9]{0,2})[.](0|[1-9][0-9]?)[.](0|[1-9][0-9]?)").matchEntire(version)?.groupValues?.drop(1)?.map(String::toInt)
    val code = parts?.let { it[0] * 10_000 + it[1] * 100 + it[2] }
    if (code == null || code == 0) {
        throw GradleException("DAYTRACE_VERSION must look like 0.2.1 (no leading zeros, at most 999.99.99, above 0.0.0), not '$version'")
    }
    code
}
if (signed && releaseVersion == null) { // else it would look like the real 0.1.0, with versionCode 1
    throw GradleException("A signed build is a release: set DAYTRACE_VERSION too (for example 0.1.0).")
}

android {
    namespace = "app.daytrace.android"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.daytrace.android"
        minSdk = 29
        targetSdk = 36
        versionCode = releaseCode ?: 1
        versionName = releaseVersion ?: "0.1.0"
    }

    signingConfigs {
        if (signed) {
            create("release") {
                storeFile = rootProject.file(keyStore!!)
                storePassword = keyStorePassword
                keyAlias = keyAliasName
                keyPassword = keyAliasPassword
            }
        }
    }

    buildTypes {
        release {
            // R8 removes unused code and resources (Compose, Room, WorkManager and Health Connect ship keep rules).
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            signingConfig = signingConfigs.findByName("release") // null without a key: unsigned
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        // Robolectric runs the Room and sync tests on the JVM; it needs the merged resources and manifest.
        unitTests.isIncludeAndroidResources = true
        unitTests.all {
            // Robolectric downloads a full Android jar (about 200 MB) to ~/.m2 by default. Keep it with Gradle's own
            // cache instead (GRADLE_USER_HOME), which also avoids a Robolectric bug with spaces in the home folder.
            it.systemProperty("maven.repo.local", gradle.gradleUserHomeDir.resolve("robolectric").absolutePath)
            // Newer JDKs (Android Studio's is 25) block the file descriptor internals Robolectric's SQLite uses.
            it.jvmArgs("--add-opens=java.base/jdk.internal.access=ALL-UNNAMED")
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons)
    implementation(libs.androidx.health.connect)
    implementation(libs.androidx.room.runtime)
    ksp(libs.androidx.room.compiler)
    implementation(libs.androidx.work.runtime)
    implementation(libs.okhttp)
    implementation(libs.play.services.code.scanner) // QR pairing: ML Kit inside Google Play services, no camera permission
    implementation(libs.androidx.webkit) // DT-58: the Dashboard tab's token, set for the hub's page only
    implementation(libs.androidx.swiperefreshlayout) // DT-58: pull to refresh the Dashboard tab
    implementation(libs.androidx.glance.appwidget) // DT-58: the home-screen widget
    debugImplementation(libs.androidx.compose.ui.tooling)
    testImplementation(libs.junit)
    testImplementation(libs.org.json) // Android's org.json is a stub in local unit tests
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.androidx.work.testing)
}
