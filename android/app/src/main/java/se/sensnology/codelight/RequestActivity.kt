package se.sensnology.codelight

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.sp

/**
 * Standalone surface for answering a Claude Code request forwarded by the
 * companion, used for auto-open and the lock screen (it needs setShowWhenLocked
 * and its own task). The Request tab in [MainActivity] renders the same
 * [RequestScreen]; both share the composables in Ui.kt. This wraps it in the
 * same "codelight" top bar so it's branded and clears the status bar.
 */
class RequestActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Show over the lock screen and wake it, so auto-open works when locked too.
        if (android.os.Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        val requestedId = intent.getStringExtra(CodelightService.EXTRA_REQUEST_ID)
        setContent { FramedRequest(requestedId) { finish() } }
    }

    @OptIn(ExperimentalMaterial3Api::class)
    @Composable
    private fun FramedRequest(requestedId: String?, onDone: () -> Unit) {
        Scaffold(
            containerColor = Palette.bg,
            topBar = {
                CenterAlignedTopAppBar(
                    title = {
                        Text("codelight", style = TextStyle(
                            color = Palette.text, fontSize = 20.sp, fontFamily = FontFamily.Monospace))
                    },
                    colors = TopAppBarDefaults.centerAlignedTopAppBarColors(containerColor = Palette.bg),
                )
            },
        ) { inner ->
            Box(Modifier.fillMaxSize().padding(inner).background(Palette.bg)) {
                RequestScreen(requestedId, onDone)
            }
        }
    }
}
