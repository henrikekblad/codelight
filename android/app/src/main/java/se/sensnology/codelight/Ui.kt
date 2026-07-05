package se.sensnology.codelight

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject

// Shared colour palette (previously duplicated across the activities).
object Palette {
    val bg     = Color(0xFF121212)
    val card   = Color(0xFF1E1E1E)
    val accent = Color(0xFF44CCAA)
    val text   = Color(0xFFEEEEEE)
    val muted  = Color(0xFF888888)
}

// ── Settings ────────────────────────────────────────────────────────────────

/**
 * The settings form. [onClose] is non-null only when hosted in a standalone
 * activity (shows a Cancel button and finishes on save); in a tab it is null so
 * Save just persists + restarts the service and stays put.
 */
@Composable
fun SettingsScreen(onClose: (() -> Unit)? = null) {
    val context  = LocalContext.current
    val settings = context.getSharedPreferences(CodelightService.SETTINGS_PREFS, 0)
    val state    = context.getSharedPreferences(CodelightService.STATE_PREFS, 0)

    var secret         by remember { mutableStateOf(settings.getString(CodelightService.KEY_SECRET, "") ?: "") }
    var host           by remember { mutableStateOf(settings.getString(CodelightService.KEY_HOST,   "") ?: "") }
    var portStr        by remember { mutableStateOf(
        settings.getInt(CodelightService.KEY_PORT, 0).let { if (it > 0) it.toString() else "" }
    ) }
    var selectedName   by remember { mutableStateOf(settings.getString(CodelightService.KEY_SELECTED_NAME, null)) }
    var notifyIdle     by remember { mutableStateOf(settings.getBoolean(CodelightService.KEY_NOTIFY_ON_IDLE,    false)) }
    var notifyWaiting  by remember { mutableStateOf(settings.getBoolean(CodelightService.KEY_NOTIFY_ON_WAITING, false)) }
    var notifyDelay    by remember { mutableStateOf(settings.getInt(CodelightService.KEY_NOTIFY_DELAY_SECS, 30).toString()) }
    var permPrompts    by remember { mutableStateOf(settings.getBoolean(CodelightService.KEY_PERMISSION_PROMPTS, true)) }
    var questionPrompts by remember { mutableStateOf(settings.getBoolean(CodelightService.KEY_QUESTION_PROMPTS, true)) }
    var autoOpen        by remember { mutableStateOf(settings.getBoolean(CodelightService.KEY_AUTO_OPEN, false)) }
    var convLines       by remember { mutableStateOf(settings.getInt(CodelightService.KEY_CONV_LINES, 50).toString()) }
    var allowedSsids   by remember { mutableStateOf(
        (settings.getStringSet(CodelightService.KEY_ALLOWED_SSIDS, emptySet()) ?: emptySet())
            .sorted().joinToString(", ")
    ) }
    var showPw         by remember { mutableStateOf(false) }

    var connected     by remember { mutableStateOf(state.getBoolean(CodelightService.KEY_CONNECTED, false)) }
    var connectedHost by remember { mutableStateOf(state.getString(CodelightService.KEY_CONNECTED_HOST, "") ?: "") }
    var connectedPort by remember { mutableStateOf(state.getInt(CodelightService.KEY_CONNECTED_PORT, 0)) }

    DisposableEffect(Unit) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { prefs, _ ->
            connected     = prefs.getBoolean(CodelightService.KEY_CONNECTED, false)
            connectedHost = prefs.getString(CodelightService.KEY_CONNECTED_HOST, "") ?: ""
            connectedPort = prefs.getInt(CodelightService.KEY_CONNECTED_PORT, 0)
        }
        state.registerOnSharedPreferenceChangeListener(listener)
        onDispose { state.unregisterOnSharedPreferenceChangeListener(listener) }
    }

    val discovered = remember { loadDiscovered(state) }

    val accent = Palette.accent
    val card   = Palette.card
    val text   = Palette.text
    val muted  = Palette.muted

    fun save() {
        val port = portStr.toIntOrNull() ?: 0
        val ssids = allowedSsids.split(",").map { it.trim() }.filter { it.isNotBlank() }.toSet()
        settings.edit()
            .putString(CodelightService.KEY_SECRET,           secret.trim())
            .putString(CodelightService.KEY_HOST,             host.trim())
            .putInt(CodelightService.KEY_PORT,                port)
            .putBoolean(CodelightService.KEY_NOTIFY_ON_IDLE,    notifyIdle)
            .putBoolean(CodelightService.KEY_NOTIFY_ON_WAITING, notifyWaiting)
            .putInt(CodelightService.KEY_NOTIFY_DELAY_SECS,     notifyDelay.toIntOrNull() ?: 30)
            .putBoolean(CodelightService.KEY_PERMISSION_PROMPTS, permPrompts)
            .putBoolean(CodelightService.KEY_QUESTION_PROMPTS, questionPrompts)
            .putBoolean(CodelightService.KEY_AUTO_OPEN, autoOpen)
            .putInt(CodelightService.KEY_CONV_LINES, convLines.toIntOrNull()?.coerceIn(5, 500) ?: 50)
            .putStringSet(CodelightService.KEY_ALLOWED_SSIDS,   ssids)
            .apply {
                if (selectedName != null) putString(CodelightService.KEY_SELECTED_NAME, selectedName)
                else remove(CodelightService.KEY_SELECTED_NAME)
            }
            .apply()
        context.stopService(Intent(context, CodelightService::class.java))
        context.startService(Intent(context, CodelightService::class.java))

        // Auto-open needs the overlay ("draw over other apps") permission so the
        // service can launch the request screen from the background. Send the
        // user to grant it if not yet granted.
        if (autoOpen && !android.provider.Settings.canDrawOverlays(context)) {
            try {
                context.startActivity(Intent(
                    android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    android.net.Uri.parse("package:${context.packageName}")))
            } catch (_: Exception) {}
        }
        onClose?.invoke()
    }

    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        // ── Connection (status + discovered + manual host) ────────────────
        SettingsCard(card, muted, label = "Connection") {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    if (connected) "●" else "○",
                    style = TextStyle(color = if (connected) accent else muted, fontSize = 14.sp),
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    if (connected && connectedHost.isNotEmpty()) "Connected  $connectedHost:$connectedPort"
                    else if (connected) "Connected"
                    else "Not connected",
                    style = TextStyle(color = if (connected) accent else muted, fontSize = 13.sp),
                )
            }

            HorizontalDivider(color = muted.copy(alpha = 0.2f),
                              modifier = Modifier.padding(vertical = 4.dp))

            Text("DISCOVERED", style = TextStyle(color = muted, fontSize = 10.sp, letterSpacing = 1.sp))
            if (discovered.isEmpty()) {
                Text("No daemons found yet. Make sure codelight.py is running.",
                     style = TextStyle(color = muted, fontSize = 12.sp))
            } else {
                discovered.forEach { svc ->
                    val isSelected = selectedName == svc.name
                    Row(
                        modifier = Modifier.fillMaxWidth()
                            .clickable { selectedName = svc.name }
                            .padding(vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        RadioButton(
                            selected = isSelected,
                            onClick  = { selectedName = svc.name },
                            colors   = RadioButtonDefaults.colors(selectedColor = accent),
                        )
                        Spacer(Modifier.width(8.dp))
                        Column {
                            Text(svc.name, style = TextStyle(color = text, fontSize = 13.sp))
                            Text("${svc.host}:${svc.port}", style = TextStyle(color = muted, fontSize = 11.sp))
                        }
                    }
                }
                if (selectedName != null) {
                    TextButton(onClick = { selectedName = null }) {
                        Text("Clear (auto-connect to first found)", color = muted, fontSize = 11.sp)
                    }
                }
            }

            HorizontalDivider(color = muted.copy(alpha = 0.2f),
                              modifier = Modifier.padding(vertical = 4.dp))

            Text("MANUAL HOST", style = TextStyle(color = muted, fontSize = 10.sp, letterSpacing = 1.sp))
            Text("Leave empty to use mDNS auto-discovery.",
                 style = TextStyle(color = muted, fontSize = 11.sp))
            Spacer(Modifier.height(4.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = host, onValueChange = { host = it },
                    placeholder = { Text("192.168.x.y", color = muted, fontSize = 13.sp) },
                    singleLine = true, label = { Text("IP", color = muted, fontSize = 11.sp) },
                    colors = fieldColors(accent, muted, text), modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = portStr, onValueChange = { portStr = it },
                    placeholder = { Text("8765", color = muted, fontSize = 13.sp) },
                    singleLine = true, label = { Text("Port", color = muted, fontSize = 11.sp) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    colors = fieldColors(accent, muted, text), modifier = Modifier.width(90.dp),
                )
            }
        }

        // ── Password ──────────────────────────────────────────────────────
        SettingsCard(card, muted, label = "Password") {
            OutlinedTextField(
                value = secret, onValueChange = { secret = it },
                placeholder = { Text(stringResource(R.string.pref_secret_hint), color = muted, fontSize = 13.sp) },
                singleLine = true,
                visualTransformation = if (showPw) VisualTransformation.None else PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                trailingIcon = {
                    TextButton(onClick = { showPw = !showPw }) {
                        Text(if (showPw) "hide" else "show", color = accent, fontSize = 12.sp)
                    }
                },
                colors = fieldColors(accent, muted, text), modifier = Modifier.fillMaxWidth(),
            )
        }

        // ── Notifications ─────────────────────────────────────────────────
        SettingsCard(card, muted, label = "Notifications") {
            Text("Notify when Claude Code changes to:", style = TextStyle(color = muted, fontSize = 11.sp))
            Spacer(Modifier.height(4.dp))
            CheckRow("IDLE",    notifyIdle,    accent, text) { notifyIdle    = it }
            CheckRow("WAITING", notifyWaiting, accent, text) { notifyWaiting = it }
            Spacer(Modifier.height(8.dp))
            Text("Delay before notifying (seconds):", style = TextStyle(color = muted, fontSize = 11.sp))
            Spacer(Modifier.height(4.dp))
            OutlinedTextField(
                value = notifyDelay, onValueChange = { notifyDelay = it }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                colors = fieldColors(accent, muted, text), modifier = Modifier.width(110.dp),
            )
            Text("Useful if you're at the computer and about to type.",
                 style = TextStyle(color = muted, fontSize = 10.sp))
            Spacer(Modifier.height(8.dp))
            CheckRow("Permission prompts", permPrompts, accent, text) { permPrompts = it }
            CheckRow("Question prompts", questionPrompts, accent, text) { questionPrompts = it }
            Text("Approve permission requests and answer AskUserQuestion prompts from " +
                 "the phone. Requires the companion to run with --remote-control.",
                 style = TextStyle(color = muted, fontSize = 10.sp))
            Spacer(Modifier.height(8.dp))
            CheckRow("Auto-open on request", autoOpen, accent, text) { autoOpen = it }
            Text("Open the app automatically when a request arrives, instead of " +
                 "tapping the notification. Needs the \"draw over other apps\" permission.",
                 style = TextStyle(color = muted, fontSize = 10.sp))
        }

        // ── Conversation ──────────────────────────────────────────────────
        SettingsCard(card, muted, label = "Conversation") {
            Text("Number of recent lines to show in the Conversation tab:",
                 style = TextStyle(color = muted, fontSize = 11.sp))
            Spacer(Modifier.height(4.dp))
            OutlinedTextField(
                value = convLines, onValueChange = { convLines = it }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                colors = fieldColors(accent, muted, text), modifier = Modifier.width(110.dp),
            )
        }

        // ── Wi-Fi SSID filter ─────────────────────────────────────────────
        SettingsCard(card, muted, label = "Wi-Fi SSID filter") {
            Text(
                "Service pauses (no connection, no notifications) while not on a Wi-Fi network " +
                "in this list — including on mobile data or VPN. It resumes automatically. " +
                "Leave empty to run on any network.",
                style = TextStyle(color = muted, fontSize = 11.sp),
            )
            Spacer(Modifier.height(4.dp))
            OutlinedTextField(
                value = allowedSsids, onValueChange = { allowedSsids = it },
                placeholder = { Text("HomeWiFi, OfficeWiFi", color = muted, fontSize = 13.sp) },
                singleLine = false,
                label = { Text("Allowed SSIDs (comma-separated)", color = muted, fontSize = 11.sp) },
                colors = fieldColors(accent, muted, text), modifier = Modifier.fillMaxWidth(),
            )
            Text("Requires 'Nearby devices' permission on Android 12+.",
                 style = TextStyle(color = muted, fontSize = 10.sp))
        }

        // ── Buttons ───────────────────────────────────────────────────────
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp, Alignment.End),
        ) {
            if (onClose != null) {
                OutlinedButton(
                    onClick = onClose,
                    colors  = ButtonDefaults.outlinedButtonColors(contentColor = muted),
                ) { Text("Cancel") }
            }
            Button(
                onClick = { save() },
                colors  = ButtonDefaults.buttonColors(containerColor = accent),
            ) { Text(stringResource(R.string.save), color = Color.Black) }
        }
    }
}

@Composable
internal fun CheckRow(label: String, checked: Boolean, accent: Color, text: Color, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().clickable { onChange(!checked) },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = onChange,
                 colors = CheckboxDefaults.colors(checkedColor = accent))
        Spacer(Modifier.width(8.dp))
        Text(label, style = TextStyle(color = text, fontSize = 13.sp))
    }
}

@Composable
internal fun SettingsCard(card: Color, muted: Color, label: String, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier = Modifier.fillMaxWidth().background(card, shape = MaterialTheme.shapes.medium).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(label.uppercase(), style = TextStyle(color = muted, fontSize = 10.sp, letterSpacing = 1.sp))
        content()
    }
}

@Composable
internal fun fieldColors(accent: Color, muted: Color, text: Color) =
    OutlinedTextFieldDefaults.colors(
        focusedBorderColor   = accent,
        unfocusedBorderColor = muted,
        cursorColor          = accent,
        focusedTextColor     = text,
        unfocusedTextColor   = text,
    )

// ── Conversation (read-only feed) ─────────────────────────────────────────────

/**
 * Shows the last N lines of the active session's conversation, mirrored to
 * STATE_PREFS by the service from the companion's `conversation` feed. Read-only:
 * injecting into a live interactive `claude` session isn't supported (see the
 * Phase-0 spike), so there is no send box.
 */
@Composable
fun ConversationScreen() {
    val context  = LocalContext.current
    val state    = context.getSharedPreferences(CodelightService.STATE_PREFS, 0)
    val settings = context.getSharedPreferences(CodelightService.SETTINGS_PREFS, 0)
    val maxLines = settings.getInt(CodelightService.KEY_CONV_LINES, 50)

    var lines by remember { mutableStateOf(loadConversation(state)) }
    DisposableEffect(Unit) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { p, k ->
            if (k == CodelightService.KEY_CONVERSATION) lines = loadConversation(p)
        }
        state.registerOnSharedPreferenceChangeListener(listener)
        onDispose { state.unregisterOnSharedPreferenceChangeListener(listener) }
    }

    val text  = Palette.text
    val muted = Palette.muted
    val shown = lines.takeLast(maxLines)
    val scroll = rememberScrollState()

    // Keep the newest content in view as the feed grows. Drive off maxValue
    // (which updates after the new content is laid out) rather than the message
    // count — otherwise we'd scroll to the previous bottom before the new tool
    // output has been measured and stop short of it.
    LaunchedEffect(Unit) {
        snapshotFlow { scroll.maxValue }.collect { scroll.scrollTo(it) }
    }

    if (shown.isEmpty()) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
            Text("No conversation yet. It appears here once Claude Code is active on the companion.",
                 style = TextStyle(color = muted, fontSize = 13.sp))
        }
        return
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(scroll).padding(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        shown.forEach { (role, body) ->
            val label = when (role) {
                "user"   -> "you"
                "tool"   -> "tool"
                "output" -> "output"
                else     -> "claude"
            }
            val labelColor = when (role) {
                "user"   -> Palette.accent
                "tool"   -> Color(0xFFCC9944)
                else     -> muted
            }
            val bgColor = when (role) {
                "user"           -> Color(0xFF16303A)
                "tool", "output" -> Color(0xFF181818)
                else             -> Palette.card
            }
            val mono = role == "tool" || role == "output"
            Column(
                Modifier.fillMaxWidth().background(bgColor, RoundedCornerShape(8.dp)).padding(10.dp),
            ) {
                Text(label, style = TextStyle(color = labelColor, fontSize = 10.sp, letterSpacing = 1.sp))
                Spacer(Modifier.height(3.dp))
                if (mono) {
                    Text(body, style = TextStyle(color = if (role == "output") muted else text,
                                                 fontSize = 12.sp, fontFamily = FontFamily.Monospace))
                } else {
                    // user / claude prose may contain markdown — render it.
                    MarkdownText(body, Palette.accent, text)
                }
            }
        }
    }
}

/**
 * Tiny, dependency-free markdown renderer covering the cases that show up in
 * Claude's replies: #/##/### headings, **bold**, `inline code`, [text](url)
 * links, - / * bullets (with nesting indent), and ``` fenced code blocks.
 * Anything else falls through as plain text. Fenced blocks render as their own
 * boxed Composable (light background filling the width); everything else is one
 * AnnotatedString. Deliberately not a full CommonMark parser.
 */
private val MD_INLINE = Regex("""\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)]\(([^)]+)\)""")
private val MD_BULLET = Regex("""^(\s*)[-*] (.*)""")

// Code stands out via the amber "tool" colour on a subtly darker background,
// not a stark white box.
private val CODE_BG = Color(0xFF262626)
private val CODE_FG = Color(0xFFCC9944)

private data class MdSeg(val text: String, val isCode: Boolean)

/** Split text on ``` fences into alternating prose / code segments. */
private fun splitFences(text: String): List<MdSeg> {
    val segs = mutableListOf<MdSeg>()
    var inCode = false
    val cur = StringBuilder()
    for (line in text.split("\n")) {
        if (line.trimStart().startsWith("```")) {
            segs.add(MdSeg(cur.toString().removeSuffix("\n"), inCode))
            cur.clear()
            inCode = !inCode
        } else {
            cur.append(line).append("\n")
        }
    }
    segs.add(MdSeg(cur.toString().removeSuffix("\n"), inCode))
    return segs.filter { it.text.isNotBlank() }
}

@Composable
private fun MarkdownText(text: String, accent: Color, baseColor: Color) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(5.dp)) {
        splitFences(text).forEach { seg ->
            if (seg.isCode) {
                Box(Modifier.fillMaxWidth().background(CODE_BG, RoundedCornerShape(6.dp)).padding(10.dp)) {
                    Text(seg.text, style = TextStyle(
                        color = CODE_FG, fontFamily = FontFamily.Monospace, fontSize = 12.sp))
                }
            } else {
                MarkdownProse(seg.text, accent, baseColor)
            }
        }
    }
}

/**
 * Block-level markdown: each line becomes its own element. Bullets render as a
 * Row (fixed marker column + weighted text column) so wrapped lines hang-indent
 * under the text, not back at the margin.
 */
@Composable
private fun MarkdownProse(text: String, accent: Color, baseColor: Color) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(3.dp)) {
        text.split("\n").forEach { raw ->
            val bullet = MD_BULLET.find(raw)
            val headingPad = Modifier.padding(top = 4.dp, bottom = 2.dp)
            when {
                raw.startsWith("### ") -> Text(inlineMd(raw.substring(4), accent), modifier = headingPad,
                    style = TextStyle(color = baseColor, fontSize = 14.sp, fontWeight = FontWeight.Bold))
                raw.startsWith("## ") -> Text(inlineMd(raw.substring(3), accent), modifier = headingPad,
                    style = TextStyle(color = baseColor, fontSize = 15.sp, fontWeight = FontWeight.Bold))
                raw.startsWith("# ") -> Text(inlineMd(raw.substring(2), accent), modifier = headingPad,
                    style = TextStyle(color = baseColor, fontSize = 16.sp, fontWeight = FontWeight.Bold))
                bullet != null -> {
                    val level = bullet.groupValues[1].replace("\t", "  ").length / 2
                    Row(Modifier.fillMaxWidth().padding(start = (8 + level * 16).dp)) {
                        Text("•", style = TextStyle(color = baseColor, fontSize = 13.sp))
                        Spacer(Modifier.width(8.dp))
                        Text(inlineMd(bullet.groupValues[2], accent), modifier = Modifier.weight(1f),
                             style = TextStyle(color = baseColor, fontSize = 13.sp))
                    }
                }
                raw.isBlank() -> {}   // paragraph gap handled by the arrangement
                else -> Text(inlineMd(raw, accent),
                             style = TextStyle(color = baseColor, fontSize = 13.sp))
            }
        }
    }
}

private fun inlineMd(s: String, accent: Color): AnnotatedString =
    buildAnnotatedString { appendInline(s, accent) }

private fun AnnotatedString.Builder.appendInline(s: String, accent: Color) {
    var last = 0
    for (m in MD_INLINE.findAll(s)) {
        if (m.range.first > last) append(s.substring(last, m.range.first))
        when {
            m.groupValues[1].isNotEmpty() ->
                withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(m.groupValues[1]) }
            m.groupValues[2].isNotEmpty() ->
                withStyle(SpanStyle(fontFamily = FontFamily.Monospace, background = CODE_BG, color = CODE_FG)) {
                    // hair spaces give a small pad without the full-space gap
                    append("\u200A" + m.groupValues[2] + "\u200A") }
            m.groupValues[3].isNotEmpty() ->
                withStyle(SpanStyle(color = accent)) { append(m.groupValues[3]) }
        }
        last = m.range.last + 1
    }
    if (last < s.length) append(s.substring(last))
}

private fun loadConversation(prefs: SharedPreferences): List<Pair<String, String>> {
    return try {
        val arr = JSONArray(prefs.getString(CodelightService.KEY_CONVERSATION, "[]") ?: "[]")
        (0 until arr.length()).mapNotNull {
            val o = arr.optJSONObject(it) ?: return@mapNotNull null
            o.optString("role", "assistant") to o.optString("text", "")
        }.filter { it.second.isNotBlank() }
    } catch (_: Exception) { emptyList() }
}

// ── Request (permission / question) ────────────────────────────────────────────

/**
 * Renders the active forwarded request from the pending snapshot. Used both by
 * the standalone [RequestActivity] (auto-open / lock screen) and the Request tab.
 * [onDone] is invoked after answering; in a tab it can be a no-op.
 */
@Composable
fun RequestScreen(requestedId: String?, onDone: () -> Unit) {
    val context = LocalContext.current
    val state = context.getSharedPreferences(CodelightService.STATE_PREFS, Context.MODE_PRIVATE)

    var pending by remember { mutableStateOf(loadPending(state)) }
    DisposableEffect(Unit) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { p, k ->
            if (k == CodelightService.KEY_PENDING_REQUESTS) pending = loadPending(p)
        }
        state.registerOnSharedPreferenceChangeListener(listener)
        onDispose { state.unregisterOnSharedPreferenceChangeListener(listener) }
    }

    val current = pending.firstOrNull { it.optString("id") == requestedId } ?: pending.firstOrNull()

    // Keepalive: extend the daemon deadline every 20 s while a request is shown.
    DisposableEffect(current?.optString("id")) {
        val id = current?.optString("id")
        if (id.isNullOrEmpty()) { onDispose { } }
        else {
            val t = kotlin.concurrent.timer(period = 20_000L, initialDelay = 20_000L) {
                context.startService(Intent(context, CodelightService::class.java)
                    .setAction(CodelightService.ACTION_EXTEND)
                    .putExtra(CodelightService.EXTRA_REQUEST_ID, id))
            }
            onDispose { t.cancel() }
        }
    }

    val card  = Palette.card
    val text  = Palette.text
    val muted = Palette.muted

    if (current == null) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
            Text("No active request.", style = TextStyle(color = muted, fontSize = 13.sp))
        }
        LaunchedEffect(Unit) { onDone() }
        return
    }

    Box(Modifier.fillMaxSize().padding(20.dp)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
            if (current.optString("kind") == "permission")
                PermissionContent(current, card, text, muted, onDone)
            else
                QuestionContent(current, card, text, muted, onDone)
        }
    }
}

@Composable
private fun PermissionContent(req: JSONObject, card: Color, text: Color, muted: Color, onDone: () -> Unit) {
    val context = LocalContext.current
    val id = req.optString("id")
    val tool = req.optString("tool_name", "tool")
    val ti = req.optJSONObject("tool_input")
    val detail = req.optString("summary", tool)

    // Show the most meaningful field for the tool rather than the raw JSON blob:
    // a plan (ExitPlanMode), a command (Bash), a path (Edit/Read/Write)…
    val body = when {
        ti == null                     -> detail
        ti.optString("plan").isNotEmpty()      -> ti.optString("plan")
        ti.optString("command").isNotEmpty()   -> ti.optString("command")
        ti.optString("file_path").isNotEmpty() -> ti.optString("file_path")
        else                           -> ti.toString(2)
    }

    Text("Claude Code asks", style = TextStyle(color = text, fontSize = 18.sp, fontWeight = FontWeight.Bold))
    Spacer(Modifier.height(4.dp))
    Text("Allow $tool?", style = TextStyle(color = muted, fontSize = 13.sp))
    Spacer(Modifier.height(12.dp))
    Column(Modifier.fillMaxWidth().background(card, RoundedCornerShape(8.dp)).padding(12.dp)) {
        Text(body,
             style = TextStyle(color = Color(0xFFC8C8C8), fontFamily = FontFamily.Monospace, fontSize = 12.sp))
    }
    Spacer(Modifier.height(16.dp))
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Button(onClick = { respondPermission(context, id, "allow"); onDone() },
               colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF238636)),
               modifier = Modifier.weight(1f)) { Text("Allow") }
        Button(onClick = { respondPermission(context, id, "deny"); onDone() },
               colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6E2B2B)),
               modifier = Modifier.weight(1f)) { Text("Deny") }
    }
}

@Composable
private fun QuestionContent(req: JSONObject, card: Color, text: Color, muted: Color, onDone: () -> Unit) {
    val context = LocalContext.current
    val id = req.optString("id")
    val questions = req.optJSONArray("questions")

    Text("Claude asks", style = TextStyle(color = text, fontSize = 18.sp, fontWeight = FontWeight.Bold))
    Spacer(Modifier.height(12.dp))

    val selected = remember { List(questions?.length() ?: 0) { mutableStateListOf<String>() } }
    val other    = remember { List(questions?.length() ?: 0) { mutableStateOf("") } }

    for (qi in 0 until (questions?.length() ?: 0)) {
        val q = questions!!.optJSONObject(qi) ?: continue
        val multi = q.optBoolean("multiSelect", false)
        val opts = q.optJSONArray("options")

        Column(Modifier.fillMaxWidth().background(card, RoundedCornerShape(8.dp)).padding(12.dp)) {
            q.optString("header").takeIf { it.isNotEmpty() }?.let {
                Text(it, style = TextStyle(color = muted, fontSize = 11.sp))
            }
            Text(q.optString("question"), style = TextStyle(color = text, fontSize = 14.sp))
            Spacer(Modifier.height(8.dp))
            for (oi in 0 until (opts?.length() ?: 0)) {
                val opt = opts!!.optJSONObject(oi) ?: continue
                val label = opt.optString("label")
                val desc  = opt.optString("description")
                val isSel = selected[qi].contains(label)
                Row(Modifier.fillMaxWidth()
                        .padding(vertical = 3.dp)
                        .border(1.dp, if (isSel) Color(0xFF00C800) else Color(0xFF444444), RoundedCornerShape(6.dp))
                        .background(if (isSel) Color(0x2600C800) else Color.Transparent, RoundedCornerShape(6.dp))
                        .clickable {
                            if (multi) {
                                if (isSel) selected[qi].remove(label) else selected[qi].add(label)
                            } else {
                                selected[qi].clear(); selected[qi].add(label)
                            }
                        }
                        .padding(10.dp)) {
                    Text(if (desc.isNotEmpty()) "$label — $desc" else label,
                         style = TextStyle(color = text, fontSize = 13.sp))
                }
            }
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(
                value = other[qi].value,
                onValueChange = { other[qi].value = it },
                placeholder = { Text("Other…", color = muted, fontSize = 13.sp) },
                singleLine = false,
                keyboardOptions = KeyboardOptions.Default,
                colors = OutlinedTextFieldDefaults.colors(
                    focusedTextColor = text, unfocusedTextColor = text,
                    focusedBorderColor = Color(0xFF44CCAA), unfocusedBorderColor = muted),
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.height(12.dp))
    }

    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Button(onClick = {
                    val answers = JSONObject()
                    var complete = true
                    for (qi in 0 until (questions?.length() ?: 0)) {
                        val q = questions!!.optJSONObject(qi) ?: continue
                        val parts = selected[qi].toMutableList()
                        other[qi].value.trim().takeIf { it.isNotEmpty() }?.let { parts.add(it) }
                        if (parts.isEmpty()) { complete = false; break }
                        answers.put(q.optString("question"), parts.joinToString(", "))
                    }
                    if (complete) { respondQuestion(context, id, answers.toString()); onDone() }
                },
               colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF238636)),
               modifier = Modifier.weight(1f)) { Text("Submit") }
        OutlinedButton(onClick = { respondQuestion(context, id, null); onDone() }) { Text("Skip") }
    }
}

private fun respondPermission(ctx: Context, id: String, decision: String) {
    ctx.startService(Intent(ctx, CodelightService::class.java)
        .setAction(CodelightService.ACTION_PERMISSION_RESPONSE)
        .putExtra(CodelightService.EXTRA_REQUEST_ID, id)
        .putExtra(CodelightService.EXTRA_DECISION, decision))
}

private fun respondQuestion(ctx: Context, id: String, answersJson: String?) {
    ctx.startService(Intent(ctx, CodelightService::class.java)
        .setAction(CodelightService.ACTION_QUESTION_RESPONSE)
        .putExtra(CodelightService.EXTRA_REQUEST_ID, id)
        .putExtra(CodelightService.EXTRA_ANSWERS, answersJson))
}

internal fun loadPending(prefs: SharedPreferences): List<JSONObject> {
    return try {
        val all = JSONObject(prefs.getString(CodelightService.KEY_PENDING_REQUESTS, "{}") ?: "{}")
        all.keys().asSequence().map { all.getJSONObject(it) }.toList()
    } catch (_: Exception) { emptyList() }
}
