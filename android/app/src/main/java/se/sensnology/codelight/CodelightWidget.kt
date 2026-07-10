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
import androidx.glance.appwidget.lazy.LazyColumn
import androidx.glance.appwidget.lazy.itemsIndexed
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
        // fits the current widget dimensions:
        //
        //  SMALL     (100× 80) – always fits — active agent only
        //  TALL      (100×200) – height ≥ 200 dp — all agents stacked, status below
        //  WIDE_TALL (240×200) – width ≥ 240 dp AND height ≥ 200 dp — agents
        //                         stacked, status column on the right
        //  EXTRA_WIDE_TALL (300×200) – meters capped at a fixed width on the
        //                         left, status window consumes the extra width
        //
        // Area order: EXTRA_WIDE_TALL > WIDE_TALL > TALL > SMALL.
        // A narrow+tall widget can’t fit WIDE_TALL,
        // so TALL wins → status below.
        // A wide+tall widget fits WIDE_TALL → status right.
        // A wider widget fits EXTRA_WIDE_TALL → status grows while meters stay capped.
        // A short or landscape widget fits only SMALL → active agent only.
        val SIZE_SMALL     = DpSize(100.dp, 80.dp)
        val SIZE_TALL      = DpSize(100.dp, 200.dp)
        val SIZE_WIDE_TALL = DpSize(240.dp, 200.dp)
        val SIZE_EXTRA_WIDE_TALL = DpSize(300.dp, 200.dp)
    }

    override val sizeMode: SizeMode = SizeMode.Responsive(
        setOf(SIZE_SMALL, SIZE_TALL, SIZE_WIDE_TALL, SIZE_EXTRA_WIDE_TALL)
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
        val agentDisplay = prefs.getString(CodelightService.KEY_AGENT_DISPLAY, "Agent") ?: "Agent"
        val activeId     = prefs.getString(CodelightService.KEY_AGENT_ID, "") ?: ""
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

        if (size == SIZE_WIDE_TALL || size == SIZE_EXTRA_WIDE_TALL) {
            val extraWide = size == SIZE_EXTRA_WIDE_TALL
            // Wide+tall: agents stacked on the left, prominent status column on
            // the right. Exactly one side may be weighted: the meters column is
            // capped at a fixed width when extra wide (status absorbs the rest),
            // otherwise the status keeps a fixed width and the meters grow —
            // a fixed width inside a weighted slot overflows and clips.
            Row(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(ColorProvider(bgColor)),
            ) {
                Column(
                    modifier = GlanceModifier
                        .then(if (extraWide) GlanceModifier.width(200.dp)
                              else GlanceModifier.defaultWeight())
                        .fillMaxHeight()
                        .padding(10.dp)
                        .clickable(actionStartActivity<MainActivity>()),
                ) {
                    LazyColumn(modifier = GlanceModifier.fillMaxWidth().fillMaxHeight()) {
                        // Each lazy item must be a single composable (a trailing
                        // Spacer is silently dropped), and the list consumes
                        // taps — so spacing and the open-app click both live on
                        // the item itself.
                        itemsIndexed(shownAgents) { index, agent ->
                            AgentBlock(
                                agent, textColor, mutedColor,
                                modifier = GlanceModifier
                                    .then(if (index > 0) GlanceModifier.padding(top = 9.dp)
                                          else GlanceModifier)
                                    .clickable(actionStartActivity<MainActivity>()),
                            )
                        }
                    }
                }
                Box(
                    modifier = GlanceModifier
                        .then(if (extraWide) GlanceModifier.defaultWeight() else GlanceModifier.width(90.dp))
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
        } else if (showAll) {
            // ── TALL: scrollable agent list on top, status banner below. The
            // list (a ListView) greedily fills any weighted slot, so the
            // banner gets a fixed height instead of a weight.
            Column(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(ColorProvider(bgColor)),
            ) {
                Column(modifier = GlanceModifier.fillMaxWidth().defaultWeight().padding(10.dp)
                    .clickable(actionStartActivity<MainActivity>())) {
                    LazyColumn(modifier = GlanceModifier.fillMaxWidth().fillMaxHeight()) {
                        itemsIndexed(shownAgents) { index, agent ->
                            AgentBlock(
                                agent, textColor, mutedColor,
                                modifier = GlanceModifier
                                    .then(if (index > 0) GlanceModifier.padding(top = 9.dp)
                                          else GlanceModifier)
                                    .clickable(actionStartActivity<MainActivity>()),
                            )
                        }
                    }
                }
                Box(
                    modifier = GlanceModifier
                        .fillMaxWidth()
                        .height(56.dp)
                        .background(ColorProvider(statusColor))
                        .clickable(actionStartActivity<MainActivity>()),
                    contentAlignment = Alignment.Center,
                ) {
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
        } else {
            // ── SMALL: the active agent only — no list needed, so the status
            // box can keep soaking up the remaining height as before.
            Column(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(ColorProvider(bgColor)),
            ) {
                Column(modifier = GlanceModifier.fillMaxWidth().padding(10.dp)
                    .clickable(actionStartActivity<MainActivity>())) {
                    AgentBlock(activeAgent, textColor, mutedColor)
                    Spacer(GlanceModifier.height(5.dp))
                }
                Box(
                    modifier = GlanceModifier
                        .fillMaxWidth()
                        .defaultWeight()
                        .background(ColorProvider(statusColor))
                        .clickable(actionStartActivity<MainActivity>()),
                    contentAlignment = Alignment.Center,
                ) {
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
