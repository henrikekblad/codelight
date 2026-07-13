package se.sensnology.codelight

import android.content.SharedPreferences
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.withTransform
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.Dp
import org.json.JSONObject

/** Branding for one agent, parsed from the daemon's config `agents` map. */
internal class AgentBranding(
    val display: String,
    val color: Color?,
    val viewBox: FloatArray,   // x, y, w, h
    val paths: List<Path>,
    val conversation: Boolean, // agent can produce a conversation feed
    val budgetSettable: Boolean, // usage-meter budget can be set from the app
    val promptCapable: Boolean,  // new instructions can be sent to the agent
)

/**
 * Parses the wire logo SVGs (a viewBox plus `<path d=…>` elements filled with
 * currentColor — the subset the companion ships) into Compose paths, cached
 * against the raw prefs JSON.
 */
internal object AgentBrandings {
    private val VIEWBOX = Regex("""viewBox="([^"]+)"""")
    private val PATH_D = Regex("""<path[^>]*?\sd="([^"]+)"""")
    private val HEX_COLOR = Regex("""#[0-9a-fA-F]{6}""")

    @Volatile private var cachedJson: String? = null
    @Volatile private var cached: Map<String, AgentBranding> = emptyMap()

    fun fromPrefs(prefs: SharedPreferences): Map<String, AgentBranding> {
        val json = prefs.getString(CodelightService.KEY_AGENTS_META, "{}") ?: "{}"
        if (json != cachedJson) {
            cached = parse(json)
            cachedJson = json
        }
        return cached
    }

    private fun parse(json: String): Map<String, AgentBranding> = try {
        val obj = JSONObject(json)
        buildMap {
            for (id in obj.keys()) {
                val meta = obj.optJSONObject(id) ?: continue
                val svg = meta.optString("logo_svg", "")
                val viewBox = VIEWBOX.find(svg)?.groupValues?.get(1)
                    ?.trim()?.split(Regex("[ ,]+"))
                    ?.mapNotNull { it.toFloatOrNull() }
                    ?.takeIf { it.size == 4 }?.toFloatArray()
                val paths = if (viewBox == null) emptyList() else
                    PATH_D.findAll(svg).mapNotNull { match ->
                        try {
                            PathParser().parsePathString(match.groupValues[1]).toPath()
                        } catch (_: Exception) {
                            null
                        }
                    }.toList()
                val color = meta.optString("color", "")
                    .takeIf { HEX_COLOR.matches(it) }
                    ?.let { Color(android.graphics.Color.parseColor(it)) }
                put(id, AgentBranding(
                    display = meta.optString("display", ""),
                    color = color,
                    viewBox = viewBox ?: floatArrayOf(0f, 0f, 1f, 1f),
                    paths = paths,
                    conversation = meta.optBoolean("conversation", false),
                    budgetSettable = meta.optBoolean("budget_settable", false),
                    promptCapable = meta.optBoolean("prompt_capable", false),
                ))
            }
        }
    } catch (_: Exception) {
        emptyMap()
    }
}

/** The agent's wire-supplied logo, tinted; renders nothing when unavailable. */
@Composable
internal fun AgentLogo(branding: AgentBranding?, tint: Color, size: Dp) {
    val paths = branding?.paths ?: return
    if (paths.isEmpty()) return
    val (vbX, vbY, vbW, vbH) = branding.viewBox
    if (vbW <= 0f || vbH <= 0f) return
    Canvas(Modifier.size(size)) {
        val scale = minOf(this.size.width / vbW, this.size.height / vbH)
        val dx = (this.size.width - vbW * scale) / 2f - vbX * scale
        val dy = (this.size.height - vbH * scale) / 2f - vbY * scale
        withTransform({
            translate(dx, dy)
            scale(scale, scale, pivot = Offset.Zero)
        }) {
            paths.forEach { drawPath(it, color = tint) }
        }
    }
}
