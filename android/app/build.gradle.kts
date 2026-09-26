// DT-19: the app module. DT-25 adds the release signing config (keystore from environment variables).
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp) // Room's code generator (DT-21)
}

android {
    namespace = "app.daytrace.android"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.daytrace.android"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            // R8 removes unused code and resources (Compose, Room, WorkManager and Health Connect ship keep rules).
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
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
    debugImplementation(libs.androidx.compose.ui.tooling)
    testImplementation(libs.junit)
    testImplementation(libs.org.json) // Android's org.json is a stub in local unit tests
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.androidx.work.testing)
}
