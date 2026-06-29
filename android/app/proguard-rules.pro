-keep class se.sensnology.codelight.** { *; }

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-keep class okhttp3.** { *; }

# Glance / Compose
-keep class androidx.glance.** { *; }
-dontwarn androidx.glance.**
