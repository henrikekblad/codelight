package se.sensnology.codelight

import android.content.SharedPreferences
import org.json.JSONObject

internal data class UsageLimit(
    val label: String,
    val pct: Float,
    val reset: String,
    val resetAt: Long,
)

internal data class AgentUsage(
    val id: String,
    val display: String,
    val status: String,
    val limits: List<UsageLimit>,
    val sessionResetSupported: Boolean = false,
    val rateLimitResetCreditsAvailableCount: Int? = null,
)

internal fun loadAgentUsage(prefs: SharedPreferences, now: Long): List<AgentUsage> {
    val usage = try {
        JSONObject(prefs.getString(CodelightService.KEY_PER_AGENT_USAGE, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }
    val statuses = try {
        JSONObject(prefs.getString(CodelightService.KEY_PER_AGENT_STATUS, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }
    val activeId = prefs.getString(CodelightService.KEY_AGENT_ID, "") ?: ""
    // Whatever agents the daemon reports, in payload order — no client-side list.
    val ids = buildSet {
        addAll(usage.keys().asSequence())
        addAll(statuses.keys().asSequence())
        if (activeId.isNotBlank()) add(activeId)
    }

    return ids.map { id ->
        val value = usage.optJSONObject(id)
        val rateLimitResetCredits = value?.optJSONObject("rateLimitResetCredits")
        val limits = buildList {
            val generic = value?.optJSONArray("limits")
            if (generic != null) {
                for (i in 0 until generic.length()) {
                    val limit = generic.optJSONObject(i) ?: continue
                    val resetAt = limit.optLong("reset_at", 0)
                    add(UsageLimit(
                        limit.optString("label", "Limit"),
                        if (resetAt in 1 until now) 0f
                        else limit.optDouble("pct", 0.0).toFloat(),
                        countdown(resetAt, now) ?: limit.optString("reset", "--"),
                        resetAt,
                    ))
                }
            } else if (id == activeId && !prefs.contains(CodelightService.KEY_PER_AGENT_USAGE)) {
                add(UsageLimit(
                    "Weekly",
                    topLevelPct(prefs, CodelightService.KEY_WEEKLY_PCT,
                        CodelightService.KEY_WEEKLY_RESET_AT, now),
                    countdown(prefs.getLong(CodelightService.KEY_WEEKLY_RESET_AT, 0), now)
                        ?: prefs.getString(CodelightService.KEY_WEEKLY_RESET, "--") ?: "--",
                    prefs.getLong(CodelightService.KEY_WEEKLY_RESET_AT, 0),
                ))
                add(UsageLimit(
                    "Session",
                    topLevelPct(prefs, CodelightService.KEY_SESSION_PCT,
                        CodelightService.KEY_SESSION_RESET_AT, now),
                    countdown(prefs.getLong(CodelightService.KEY_SESSION_RESET_AT, 0), now)
                        ?: prefs.getString(CodelightService.KEY_SESSION_RESET, "--") ?: "--",
                    prefs.getLong(CodelightService.KEY_SESSION_RESET_AT, 0),
                ))
            }
        }
        AgentUsage(
            id = id,
            display = value?.optString("agent_display")
                ?.takeIf { it.isNotBlank() }
                ?: id.replaceFirstChar { it.uppercase() },
            status = statuses.optString(
                id, if (id == activeId) prefs.getString(CodelightService.KEY_STATUS, "idle")
                    ?: "idle" else "idle"),
            limits = limits,
            sessionResetSupported = value?.optBoolean("session_reset_supported", false) == true,
            rateLimitResetCreditsAvailableCount = when {
                rateLimitResetCredits != null -> rateLimitResetCredits.optInt("availableCount", 0)
                value?.has("rate_limit_reset_available_count") == true ->
                    value.optInt("rate_limit_reset_available_count", 0)
                else -> null
            },
        )
    }.sortedWith(compareByDescending<AgentUsage> { it.id == activeId }
        .thenByDescending { statusRank(it.status) }
        .thenBy { it.display.lowercase() })
}

private fun statusRank(status: String): Int = when (status) {
    "working" -> 3
    "waiting" -> 2
    "idle" -> 1
    else -> 0
}

private fun topLevelPct(
    prefs: SharedPreferences,
    pctKey: String,
    resetKey: String,
    now: Long,
): Float {
    val resetAt = prefs.getLong(resetKey, 0)
    return if (resetAt in 1 until now) 0f else prefs.getFloat(pctKey, 0f)
}

internal fun countdown(resetAt: Long, now: Long): String? {
    if (resetAt <= 0) return null
    val diff = resetAt - now
    if (diff <= 0) return "now"
    val days = diff / 86400
    val hours = (diff % 86400) / 3600
    val mins = (diff % 3600) / 60
    return when {
        days > 0 -> "${days}d ${hours}h"
        hours > 0 -> "${hours}h ${mins}m"
        else -> "${mins}m"
    }
}
