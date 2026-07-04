package se.sensnology.codelight

import android.content.Context
import android.util.Log
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.action.actionStartActivity
import androidx.glance.currentState
import androidx.glance.state.GlanceStateDefinition
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.LinearProgressIndicator
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.*
import androidx.glance.text.*
import androidx.glance.unit.ColorProvider

class CodelightWidget : GlanceAppWidget() {

    override val stateDefinition: GlanceStateDefinition<*> = PreferencesGlanceStateDefinition

    companion object {
        val KEY_TICK = intPreferencesKey("tick")
    }

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent { WidgetContent(context) }
    }

    @Composable
    private fun WidgetContent(context: Context) {
        // Reading tick from Glance state makes this composable reactive —
        // Glance re-executes it whenever the tick changes.
        currentState<Preferences>()[KEY_TICK]
        val prefs = context.getSharedPreferences(CodelightService.STATE_PREFS, Context.MODE_PRIVATE)
        val now          = System.currentTimeMillis() / 1000
        val sessionResetAt = prefs.getLong(CodelightService.KEY_SESSION_RESET_AT, 0)
        val weeklyResetAt  = prefs.getLong(CodelightService.KEY_WEEKLY_RESET_AT, 0)
        // Once a usage window's reset time has passed, the limit has been
        // restored — show 0% even while offline so you know you can resume.
        val sessionPct   = if (sessionResetAt in 1 until now) 0f
                           else prefs.getFloat(CodelightService.KEY_SESSION_PCT, 0f)
        val weeklyPct    = if (weeklyResetAt in 1 until now) 0f
                           else prefs.getFloat(CodelightService.KEY_WEEKLY_PCT, 0f)
        // Count down live from the absolute reset time; fall back to the
        // daemon's snapshot string for pre-timestamp payloads.
        val sessionReset = countdown(sessionResetAt, now)
            ?: prefs.getString(CodelightService.KEY_SESSION_RESET, "--") ?: "--"
        val weeklyReset  = countdown(weeklyResetAt, now)
            ?: prefs.getString(CodelightService.KEY_WEEKLY_RESET, "--") ?: "--"
        val status       = prefs.getString(CodelightService.KEY_STATUS, "idle") ?: "idle"
        val connected    = prefs.getBoolean(CodelightService.KEY_CONNECTED, false)
        Log.d("Codelight", "WidgetContent render: connected=$connected status=$status session=${(sessionPct*100).toInt()}% weekly=${(weeklyPct*100).toInt()}%")

        val statusColor = when {
            !connected          -> Color(0xFF2A2A2A)
            status == "working" -> Color(0xFFFF8C00)
            status == "waiting" -> Color(0xFFFF2200)
            else                -> Color(0xFF00C800)
        }
        val statusLabel = when {
            !connected          -> "OFF"
            else -> status.uppercase()
        }
        val statusTextColor = if (!connected) Color(0xFF555555) else Color.Black

        val bgColor    = Color(0xFF1A1A1A)
        val textColor  = ColorProvider(Color.White)
        val mutedColor = ColorProvider(Color(0xFF888888))

        Column(
            modifier = GlanceModifier
                .fillMaxSize()
                .background(ColorProvider(bgColor)),
        ) {
            // ── Top: meters ───────────────────────────────────────────────────
            Column(modifier = GlanceModifier.fillMaxWidth().padding(10.dp)
                .clickable(actionStartActivity<SettingsActivity>())) {
                /* Row(
                    modifier = GlanceModifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("codelight",
                         style = TextStyle(color = mutedColor, fontSize = 10.sp),
                         modifier = GlanceModifier.defaultWeight())
                    if (!connected) {
                        Text("○", style = TextStyle(
                            color = ColorProvider(Color(0xFF555555)), fontSize = 10.sp))
                    }
                } */
                Spacer(GlanceModifier.height(6.dp))
                MeterRow("Weekly",  weeklyPct,  weeklyReset,  textColor)
                Spacer(GlanceModifier.height(5.dp))
                MeterRow("Session", sessionPct, sessionReset, textColor)
                Spacer(GlanceModifier.height(8.dp))
            }

            // ── Bottom: status fills remaining space ──────────────────────────
            Box(
                modifier = GlanceModifier
                    .fillMaxWidth()
                    .defaultWeight()
                    .background(ColorProvider(statusColor))
                    .clickable(actionStartActivity<SettingsActivity>()),
                contentAlignment = Alignment.Center,
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        statusLabel,
                        style = TextStyle(
                            color      = ColorProvider(statusTextColor),
                            fontSize   = 18.sp,
                            fontWeight = FontWeight.Bold,
                        ),
                    )
                }
            }
        }
    }

    /** Live countdown to an epoch-seconds reset time (e.g. "3h 45m", "now").
     *  Returns null when no timestamp is available (older daemon). */
    private fun countdown(resetAt: Long, now: Long): String? {
        if (resetAt <= 0) return null
        val diff = resetAt - now
        if (diff <= 0) return "now"
        val days = diff / 86400
        val hours = (diff % 86400) / 3600
        val mins = (diff % 3600) / 60
        return when {
            days > 0  -> "${days}d ${hours}h"
            hours > 0 -> "${hours}h ${mins}m"
            else      -> "${mins}m"
        }
    }

    private fun usageColor(pct: Float): Color {
        val stops = arrayOf(Color(0xFF00C800), Color(0xFFFFFF00), Color(0xFFFF8C00), Color(0xFFFF2200))
        val edges = floatArrayOf(0f, 0.5f, 0.75f, 1f)
        val p = pct.coerceIn(0f, 1f)
        for (i in 0..2) {
            if (p <= edges[i + 1]) {
                val t = (p - edges[i]) / (edges[i + 1] - edges[i])
                return lerp(stops[i], stops[i + 1], t)
            }
        }
        return stops[3]
    }

    @Composable
    private fun MeterRow(
        label:     String,
        pct:       Float,
        reset:     String,
        textColor: ColorProvider,
    ) {
        val emptyColor  = Color(0xFF444444)
        val clamped     = pct.coerceIn(0f, 1f)
        val fillColor   = usageColor(clamped)

        Column(modifier = GlanceModifier.fillMaxWidth()) {
            Row(modifier = GlanceModifier.fillMaxWidth()) {
                Text("$label — ${(clamped * 100).toInt()}%",
                     style = TextStyle(color = textColor, fontSize = 11.sp),
                     modifier = GlanceModifier.defaultWeight())
                Text("↻ $reset",
                     style = TextStyle(color = textColor, fontSize = 11.sp))
            }
            Spacer(GlanceModifier.height(3.dp))
            LinearProgressIndicator(
                progress = clamped,
                modifier = GlanceModifier.fillMaxWidth().height(5.dp),
                color = ColorProvider(fillColor),
                backgroundColor = ColorProvider(emptyColor),
            )
        }
    }
}
