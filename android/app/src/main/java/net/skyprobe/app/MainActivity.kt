package net.skyprobe.app

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import net.skyprobe.app.ui.AppScaffold
import net.skyprobe.app.ui.SkyProbeTheme

class MainActivity : ComponentActivity() {
    private val vm: AppViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleIncoming(intent)
        setContent {
            SkyProbeTheme {
                val state by vm.state.collectAsStateWithLifecycle()
                Surface(modifier = Modifier) {
                    AppScaffold(state = state, vm = vm)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIncoming(intent)
    }

    /**
     * Images arriving from another app: "Share" from any gallery or file manager
     * (ACTION_SEND / ACTION_SEND_MULTIPLE) and "Open with" (ACTION_VIEW).
     */
    private fun handleIncoming(intent: Intent?) {
        if (intent == null) return
        if (intent.type?.startsWith("text/") == true) return
        val uri = when (intent.action) {
            Intent.ACTION_SEND -> intent.stream(Intent.EXTRA_STREAM)
            Intent.ACTION_SEND_MULTIPLE -> intent.streams().firstOrNull()
            Intent.ACTION_VIEW -> intent.data
            else -> null
        } ?: return
        val extra = if (intent.action == Intent.ACTION_SEND_MULTIPLE) intent.streams().size else 1
        vm.importImage(uri, sharedBatch = extra)
        // consume it, so a configuration change does not re-import the same picture
        intent.action = null
    }

    private fun Intent.stream(key: String): Uri? =
        if (Build.VERSION.SDK_INT >= 33) getParcelableExtra(key, Uri::class.java)
        else @Suppress("DEPRECATION") getParcelableExtra(key)

    private fun Intent.streams(): List<Uri> =
        if (Build.VERSION.SDK_INT >= 33) getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java).orEmpty()
        else @Suppress("DEPRECATION") getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
}
