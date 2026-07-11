plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace   = "se.sensnology.codelight"
    compileSdk  = 35

    defaultConfig {
        applicationId  = "se.sensnology.codelight"
        minSdk         = 26
        targetSdk      = 35
        versionCode    = 19
        versionName    = "1.4.3"
    }

    signingConfigs {
        create("release") {
            val kPath  = System.getenv("SIGNING_STORE_PATH")
            val kPass  = System.getenv("SIGNING_STORE_PASSWORD")
            val kAlias = System.getenv("SIGNING_KEY_ALIAS")
            val kKey   = System.getenv("SIGNING_KEY_PASSWORD")
            if (kPath != null && kPass != null && kAlias != null && kKey != null) {
                storeFile     = file(kPath)
                storePassword = kPass
                keyAlias      = kAlias
                keyPassword   = kKey
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            val rel = signingConfigs.getByName("release")
            if (rel.storeFile != null) signingConfig = rel
        }
    }

    buildFeatures { compose = true }
    composeOptions { kotlinCompilerExtensionVersion = "1.5.14" }

    kotlinOptions { jvmTarget = "17" }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.9.2")

    implementation("androidx.glance:glance-appwidget:1.1.0")
    implementation("androidx.glance:glance-material3:1.1.0")

    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-service:2.8.5")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
