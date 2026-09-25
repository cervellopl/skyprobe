package net.skyprobe.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
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
                // Without this, an upload/analysis still survives the screen turning off (the
                // foreground service still runs), but its notification stays invisible on
                // Android 13+ until the user grants it from system settings.
                val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
                LaunchedEffect(Unit) {
                    if (Build.VERSION.SDK_INT >= 33 &&
                        ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.POST_NOTIFICATIONS)
                            != PackageManager.PERMISSION_GRANTED
                    ) {
                        askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
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
     * (ACTION_SEND / ACTION_SEND_MULTIPLE, one or several images at once) and "Open with"
     * (ACTION_VIEW, always one).
     */
    private fun handleIncoming(intent: Intent?) {
        if (intent == null) return
        if (intent.type?.startsWith("text/") == true) return
        val uris = when (intent.action) {
            Intent.ACTION_SEND -> intent.stream(Intent.EXTRA_STREAM)?.let { listOf(it) } ?: emptyList()
            Intent.ACTION_SEND_MULTIPLE -> intent.streams()
            Intent.ACTION_VIEW -> intent.data?.let { listOf(it) } ?: emptyList()
            else -> emptyList()
        }
        if (uris.isEmpty()) return
        vm.importImages(uris)
        // consume it, so a configuration change does not re-import the same picture(s)
        intent.action = null
    }

    private fun Intent.stream(key: String): Uri? =
        if (Build.VERSION.SDK_INT >= 33) getParcelableExtra(key, Uri::class.java)
        else @Suppress("DEPRECATION") getParcelableExtra(key)

    private fun Intent.streams(): List<Uri> =
        if (Build.VERSION.SDK_INT >= 33) getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java).orEmpty()
        else @Suppress("DEPRECATION") getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
}
