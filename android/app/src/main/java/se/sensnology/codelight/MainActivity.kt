package se.sensnology.codelight

import android.Manifest
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat

/**
 * Single hosting activity. With remote-control armed on the companion it shows a
 * bottom tab bar (Conversation / Request / Settings); otherwise it is just the
 * Settings screen. "codelight" stays centered at the top. Also the launcher and
 * the target of the widget/notification taps.
 */
class MainActivity : ComponentActivity() {

    private val requestPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* no-op */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startService(Intent(this, CodelightService::class.java))
        maybeRequestPermissions()
        val initialTab = intent?.getStringExtra(CodelightService.EXTRA_TAB)
        setContent { MainScreen(initialTab) }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val tab = intent.getStringExtra(CodelightService.EXTRA_TAB)
        setContent { MainScreen(tab) }
    }

    private fun maybeRequestPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.NEARBY_WIFI_DEVICES)
                != PackageManager.PERMISSION_GRANTED
        ) {
            @Suppress("NewApi")
            requestPermission.launch(Manifest.permission.NEARBY_WIFI_DEVICES)
        }
    }
}

private enum class Tab(val key: String, val title: String) {
    CONVERSATION("conversation", "Conversation"),
    REQUEST("request", "Request"),
    SETTINGS("settings", "Settings"),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MainScreen(initialTab: String?) {
    val context = LocalContext.current
    val state = context.getSharedPreferences(CodelightService.STATE_PREFS, 0)

    var remoteControl by remember { mutableStateOf(state.getBoolean(CodelightService.KEY_REMOTE_CONTROL, false)) }
    var hasRequest    by remember { mutableStateOf(loadPending(state).isNotEmpty()) }
    DisposableEffect(Unit) {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { p, k ->
            when (k) {
                CodelightService.KEY_REMOTE_CONTROL -> remoteControl = p.getBoolean(k, false)
                CodelightService.KEY_PENDING_REQUESTS -> hasRequest = loadPending(p).isNotEmpty()
            }
        }
        state.registerOnSharedPreferenceChangeListener(listener)
        onDispose { state.unregisterOnSharedPreferenceChangeListener(listener) }
    }

    var current by remember {
        mutableStateOf(
            when (initialTab) {
                Tab.REQUEST.key      -> Tab.REQUEST
                Tab.CONVERSATION.key -> Tab.CONVERSATION
                Tab.SETTINGS.key     -> Tab.SETTINGS
                else -> Tab.SETTINGS
            }
        )
    }

    // Jump to the Request tab when a request arrives while remote-control is on.
    LaunchedEffect(hasRequest, remoteControl) {
        if (remoteControl && hasRequest) current = Tab.REQUEST
    }

    val visibleTabs = if (remoteControl) {
        buildList {
            add(Tab.CONVERSATION)
            if (hasRequest) add(Tab.REQUEST)
            add(Tab.SETTINGS)
        }
    } else listOf(Tab.SETTINGS)

    // Never leave the selection on a tab that is no longer visible.
    if (current !in visibleTabs) current = visibleTabs.first()

    Scaffold(
        containerColor = Palette.bg,
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Text("codelight", style = TextStyle(
                        color = Palette.text, fontSize = 20.sp, fontFamily = FontFamily.Monospace))
                },
                colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
                    containerColor = Palette.bg),
            )
        },
        bottomBar = {
            if (remoteControl) {
                NavigationBar(containerColor = Palette.card) {
                    visibleTabs.forEach { tab ->
                        NavigationBarItem(
                            selected = current == tab,
                            onClick = { current = tab },
                            icon = {},
                            label = { Text(tab.title, fontSize = 12.sp) },
                            colors = NavigationBarItemDefaults.colors(
                                selectedTextColor   = Palette.accent,
                                unselectedTextColor = Palette.muted,
                                indicatorColor      = Palette.card,
                            ),
                        )
                    }
                }
            }
        },
    ) { inner ->
        Box(Modifier.fillMaxSize().padding(inner).background(Palette.bg)) {
            when (current) {
                Tab.CONVERSATION -> ConversationScreen()
                Tab.REQUEST      -> RequestScreen(null) { /* stay in the tab after answering */ }
                Tab.SETTINGS     -> SettingsScreen(onClose = null)
            }
        }
    }
}
