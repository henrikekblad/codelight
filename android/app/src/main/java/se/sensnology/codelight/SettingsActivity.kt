package se.sensnology.codelight

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

class SettingsActivity : ComponentActivity() {

    private val requestNotifPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* no-op */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startService(Intent(this, CodelightService::class.java))
        maybeRequestNotificationPermission()
        setContent { SettingsScreen() }
    }

    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}

@Composable
private fun SettingsScreen() {
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

    val bg     = Color(0xFF121212)
    val card   = Color(0xFF1E1E1E)
    val accent = Color(0xFF44CCAA)
    val text   = Color(0xFFEEEEEE)
    val muted  = Color(0xFF888888)

    fun save() {
        val port = portStr.toIntOrNull() ?: 0
        settings.edit()
            .putString(CodelightService.KEY_SECRET,           secret.trim())
            .putString(CodelightService.KEY_HOST,             host.trim())
            .putInt(CodelightService.KEY_PORT,                port)
            .putBoolean(CodelightService.KEY_NOTIFY_ON_IDLE,    notifyIdle)
            .putBoolean(CodelightService.KEY_NOTIFY_ON_WAITING, notifyWaiting)
            .putInt(CodelightService.KEY_NOTIFY_DELAY_SECS,     notifyDelay.toIntOrNull() ?: 30)
            .apply {
                if (selectedName != null) putString(CodelightService.KEY_SELECTED_NAME, selectedName)
                else remove(CodelightService.KEY_SELECTED_NAME)
            }
            .apply()
        context.stopService(Intent(context, CodelightService::class.java))
        context.startService(Intent(context, CodelightService::class.java))
        (context as? Activity)?.finish()
    }

    Box(
        modifier = Modifier.fillMaxSize().background(bg).padding(24.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(20.dp),
        ) {
            // ── Header ────────────────────────────────────────────────────────
            Text("codelight", style = TextStyle(
                color = text, fontSize = 22.sp, fontFamily = FontFamily.Monospace))

            // ── Widget hint ───────────────────────────────────────────────────
            Text(
                "Add the codelight widget to your home screen to see live status.",
                style = TextStyle(color = muted, fontSize = 13.sp),
            )

            Spacer(Modifier.height(4.dp))

            // ── Connection (status + discovered + manual host) ────────────────
            SettingsCard(card, muted, label = "Connection") {
                // Status badge
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        if (connected) "●" else "○",
                        style = TextStyle(
                            color    = if (connected) accent else muted,
                            fontSize = 14.sp,
                        ),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        if (connected && connectedHost.isNotEmpty()) "Connected  $connectedHost:$connectedPort"
                        else if (connected) "Connected"
                        else "Not connected",
                        style = TextStyle(
                            color    = if (connected) accent else muted,
                            fontSize = 13.sp,
                        ),
                    )
                }

                HorizontalDivider(color = muted.copy(alpha = 0.2f),
                                  modifier = Modifier.padding(vertical = 4.dp))

                // Discovered
                Text("DISCOVERED", style = TextStyle(color = muted, fontSize = 10.sp,
                     letterSpacing = 1.sp))
                if (discovered.isEmpty()) {
                    Text("No daemons found yet. Make sure codelight.py is running.",
                         style = TextStyle(color = muted, fontSize = 12.sp))
                } else {
                    discovered.forEach { svc ->
                        val isSelected = selectedName == svc.name
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
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
                                Text("${svc.host}:${svc.port}",
                                     style = TextStyle(color = muted, fontSize = 11.sp))
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

                // Manual host
                Text("MANUAL HOST", style = TextStyle(color = muted, fontSize = 10.sp,
                     letterSpacing = 1.sp))
                Text("Leave empty to use mDNS auto-discovery.",
                     style = TextStyle(color = muted, fontSize = 11.sp))
                Spacer(Modifier.height(4.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(
                        value         = host,
                        onValueChange = { host = it },
                        placeholder   = { Text("192.168.x.y", color = muted, fontSize = 13.sp) },
                        singleLine    = true,
                        label         = { Text("IP", color = muted, fontSize = 11.sp) },
                        colors        = fieldColors(accent, muted, text),
                        modifier      = Modifier.weight(1f),
                    )
                    OutlinedTextField(
                        value         = portStr,
                        onValueChange = { portStr = it },
                        placeholder   = { Text("8765", color = muted, fontSize = 13.sp) },
                        singleLine    = true,
                        label         = { Text("Port", color = muted, fontSize = 11.sp) },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        colors        = fieldColors(accent, muted, text),
                        modifier      = Modifier.width(90.dp),
                    )
                }
            }

            // ── Password ──────────────────────────────────────────────────────
            SettingsCard(card, muted, label = "Password") {
                OutlinedTextField(
                    value         = secret,
                    onValueChange = { secret = it },
                    placeholder   = { Text(stringResource(R.string.pref_secret_hint),
                                          color = muted, fontSize = 13.sp) },
                    singleLine    = true,
                    visualTransformation = if (showPw) VisualTransformation.None
                                          else PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    trailingIcon  = {
                        TextButton(onClick = { showPw = !showPw }) {
                            Text(if (showPw) "hide" else "show", color = accent, fontSize = 12.sp)
                        }
                    },
                    colors   = fieldColors(accent, muted, text),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            // ── Notifications ─────────────────────────────────────────────────
            SettingsCard(card, muted, label = "Notifications") {
                Text("Notify when Claude Code changes to:",
                     style = TextStyle(color = muted, fontSize = 11.sp))
                Spacer(Modifier.height(4.dp))
                CheckRow("IDLE",    notifyIdle,    accent, text) { notifyIdle    = it }
                CheckRow("WAITING", notifyWaiting, accent, text) { notifyWaiting = it }
                Spacer(Modifier.height(8.dp))
                Text("Delay before notifying (seconds):",
                     style = TextStyle(color = muted, fontSize = 11.sp))
                Spacer(Modifier.height(4.dp))
                OutlinedTextField(
                    value         = notifyDelay,
                    onValueChange = { notifyDelay = it },
                    singleLine    = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    colors        = fieldColors(accent, muted, text),
                    modifier      = Modifier.width(110.dp),
                )
                Text("Useful if you're at the computer and about to type.",
                     style = TextStyle(color = muted, fontSize = 10.sp))
            }

            // ── Buttons ───────────────────────────────────────────────────────
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp, Alignment.End),
            ) {
                OutlinedButton(
                    onClick = { (context as? Activity)?.finish() },
                    colors  = ButtonDefaults.outlinedButtonColors(contentColor = muted),
                ) {
                    Text("Cancel")
                }
                Button(
                    onClick = { save() },
                    colors  = ButtonDefaults.buttonColors(containerColor = accent),
                ) {
                    Text(stringResource(R.string.save), color = Color.Black)
                }
            }
        }
    }
}

@Composable
private fun CheckRow(
    label:    String,
    checked:  Boolean,
    accent:   Color,
    text:     Color,
    onChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().clickable { onChange(!checked) },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(
            checked = checked,
            onCheckedChange = onChange,
            colors = CheckboxDefaults.colors(checkedColor = accent),
        )
        Spacer(Modifier.width(8.dp))
        Text(label, style = TextStyle(color = text, fontSize = 13.sp))
    }
}

@Composable
private fun SettingsCard(
    card:    Color,
    muted:   Color,
    label:   String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(card, shape = MaterialTheme.shapes.medium)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(label.uppercase(), style = TextStyle(color = muted, fontSize = 10.sp,
             letterSpacing = 1.sp))
        content()
    }
}

@Composable
private fun fieldColors(accent: Color, muted: Color, text: Color) =
    OutlinedTextFieldDefaults.colors(
        focusedBorderColor   = accent,
        unfocusedBorderColor = muted,
        cursorColor          = accent,
        focusedTextColor     = text,
        unfocusedTextColor   = text,
    )
