import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing key, kept outside the repo (keystore.properties is git-ignored). Without it,
// release builds fall back to the local debug key, which is fine for building your own copy.
val keystoreProps = Properties().apply {
    rootProject.file("keystore.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}

// Optional server the app tries first (-Pclaudecam.server=host:port, see scripts/build-apk.sh).
// The app also finds the server over mDNS, and the address can be changed in the app.
val defaultServer = (findProperty("claudecam.server") as String?).orEmpty()

android {
    namespace = "com.ssjrocks.claudecam"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.ssjrocks.claudecam"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.1.0"
        buildConfigField("String", "DEFAULT_SERVER", "\"$defaultServer\"")
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        if (keystoreProps.isNotEmpty()) {
            create("release") {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    val camerax = "1.5.3"
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.camera:camera-core:$camerax")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")
    implementation("androidx.camera:camera-video:$camerax")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
