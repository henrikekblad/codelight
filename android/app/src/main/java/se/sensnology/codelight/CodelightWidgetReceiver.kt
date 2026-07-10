package se.sensnology.codelight

import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import androidx.glance.appwidget.GlanceAppWidgetReceiver

class CodelightWidgetReceiver : GlanceAppWidgetReceiver() {

    override val glanceAppWidget = CodelightWidget()

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        try {
            context.startService(Intent(context, CodelightService::class.java))
        } catch (_: IllegalStateException) {
            // Background start not allowed (Android 12+). The widget renders
            // from stored state; the service starts on next app open / boot.
        }
    }

    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
    }

    override fun onDisabled(context: Context) {
        super.onDisabled(context)
        context.stopService(Intent(context, CodelightService::class.java))
    }
}
