package se.sensnology.codelight

import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import androidx.glance.appwidget.GlanceAppWidgetReceiver

class CodelightWidgetReceiver : GlanceAppWidgetReceiver() {

    override val glanceAppWidget = CodelightWidget()

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        context.startService(Intent(context, CodelightService::class.java))
    }

    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
    }

    override fun onDisabled(context: Context) {
        super.onDisabled(context)
        context.stopService(Intent(context, CodelightService::class.java))
    }
}
