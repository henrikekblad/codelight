package se.sensnology.codelight

import android.content.Context
import android.util.Log
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.glance.GlanceId
import androidx.glance.LocalSize
import androidx.glance.GlanceModifier
import androidx.glance.action.actionStartActivity
import androidx.glance.currentState
import androidx.glance.state.GlanceStateDefinition
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.LinearProgressIndicator
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.*
import androidx.glance.text.*
import androidx.glance.unit.ColorProvider

class CodelightWidget : GlanceAppWidget() {

    override val stateDefinition: GlanceStateDefinition<*> = PreferencesGlanceStateDefinition

    companion object {
        val KEY_TICK = intPreferencesKey("tick")

        // Responsive size buckets. Glance picks the largest-area bucket that
        // fits the current widget dimensions. Three buckets cover all cases:
        //
        //  SMALL     (100× 80) – always fits — active agent only
        //  TALL      (100×200) – height ≥ 200 dp — all agents stacked, status below
        //  WIDE_TALL (280×200) – width ≥ 280 dp AND height ≥ 200 dp — all agents
        //                         stacked, status column on the right
        //
        // Area order: WIDE_TALL (56 000) > TALL (20 000) > SMALL (8 000)
        // A narrow+tall widget (e.g. 218×235) can’t fit WIDE_TALL (280 > 218),
        // so TALL wins → status below.
        // A wide+tall widget (e.g. 360×235) fits both; WIDE_TALL wins → status right.
        // A short or landscape widget fits only SMALL → active agent only.
        val SIZE_SMALL     = DpSize(100.dp, 80.dp)
        val SIZE_TALL      = DpSize(100.dp, 200.dp)
        val SIZE_WIDE_TALL = DpSize(280.dp, 200.dp)
    }

    override val sizeMode: SizeMode = SizeMode.Responsive(
        setOf(SIZE_SMALL, SIZE_TALL, SIZE_WIDE_TALL)
    )

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
        val agentDisplay = prefs.getString(CodelightService.KEY_AGENT_DISPLAY, "Claude") ?: "Claude"
        val activeId     = prefs.getString(CodelightService.KEY_AGENT_ID, "claude") ?: "claude"
        val status       = prefs.getString(CodelightService.KEY_STATUS, "idle") ?: "idle"
        val connected    = prefs.getBoolean(CodelightService.KEY_CONNECTED, false)
        val size          = LocalSize.current
        val allAgents     = loadAgentUsage(prefs, now)
        val activeAgent   = allAgents.firstOrNull { it.id == activeId }
            ?: AgentUsage(activeId, agentDisplay, status, emptyList())
        // size == one of the three responsive buckets.
        val showAll       = size != SIZE_SMALL
        val shownAgents   = if (showAll) allAgents else listOf(activeAgent)
        Log.d("Codelight", "WidgetContent render: ${size.width}×${size.height} connected=$connected agents=${shownAgents.size}")

        val statusColor = when {
            !connected          -> Color(0xFF2A2A2A)
            status == "working" -> Color(0xFFFF8C00)
            status == "waiting" -> Color(0xFFFF2200)
            else                -> Color(0xFF00C800)
        }
        val statusLabel = when {
            !connected          -> "OFFLINE"
            else -> "$agentDisplay ${status.uppercase()}"
        }
        val statusTextColor = if (!connected) Color(0xFF555555) else Color.Black

        val bgColor    = Color(0xFF1A1A1A)
        val textColor  = ColorProvider(Color.White)
        val mutedColor = ColorProvider(Color(0xFF888888))

        if (size == SIZE_WIDE_TALL) {
            // Wide+tall: agents stacked on the left, prominent status column on the right.
            Row(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(ColorProvider(bgColor)),
            ) {
                Column(
                    modifier = GlanceModifier
                        .defaultWeight()
                        .fillMaxHeight()
                        .padding(10.dp)
                        .clickable(actionStartActivity<MainActivity>()),
                ) {
                    shownAgents.forEachIndexed { index, agent ->
                        if (index > 0) Spacer(GlanceModifier.height(9.dp))
                        AgentBlock(agent, textColor, mutedColor)
                    }
                }
                Box(
                    modifier = GlanceModifier
                        .width(90.dp)
                        .fillMaxHeight()
                        .background(ColorProvider(statusColor))
                        .clickable(actionStartActivity<MainActivity>()),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        statusLabel,
                        style = TextStyle(
                            color      = ColorProvider(statusTextColor),
                            fontSize   = 14.sp,
                            fontWeight = FontWeight.Bold,
                            textAlign  = TextAlign.Center,
                        ),
                        maxLines = 3,
                    )
                }
            }
        } else {
            Column(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(ColorProvider(bgColor)),
            ) {
                // ── Top: meters ───────────────────────────────────────────────────
                Column(modifier = GlanceModifier.fillMaxWidth().padding(10.dp)
                    .clickable(actionStartActivity<MainActivity>())) {
                    shownAgents.forEachIndexed { index, agent ->
                        if (index > 0) Spacer(GlanceModifier.height(9.dp))
                        AgentBlock(agent, textColor, mutedColor)
                    }
                    Spacer(GlanceModifier.height(5.dp))
                }

                // ── Bottom: status fills remaining space ──────────────────────────
                Box(
                    modifier = GlanceModifier
                        .fillMaxWidth()
                        .defaultWeight()
                        .background(ColorProvider(statusColor))
                        .clickable(actionStartActivity<MainActivity>()),
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
    private fun AgentBlock(
        agent: AgentUsage,
        textColor: ColorProvider,
        mutedColor: ColorProvider,
        modifier: GlanceModifier = GlanceModifier,
    ) {
        Column(modifier = modifier.fillMaxWidth()) {
            Row(modifier = GlanceModifier.fillMaxWidth()) {
                Text(agent.display,
                    style = TextStyle(color = textColor, fontSize = 11.sp,
                        fontWeight = FontWeight.Bold),
                    modifier = GlanceModifier.defaultWeight())
                Text(agent.status.uppercase(),
                    style = TextStyle(color = mutedColor, fontSize = 9.sp))
            }
            agent.limits.forEach { limit ->
                Spacer(GlanceModifier.height(3.dp))
                MeterRow(limit.label, limit.pct, limit.reset, textColor)
            }
        }
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
