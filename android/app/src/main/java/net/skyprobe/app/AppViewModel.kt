package net.skyprobe.app

import android.app.Application
import android.content.Context
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers
import java.io.File
import net.skyprobe.app.net.Health
import net.skyprobe.app.net.JobResult
import net.skyprobe.app.net.JobSummary
import net.skyprobe.app.net.SkyProbeApi

const val NOT_CONFIGURED = "not configured"

data class Settings(
    val server: String = "",          // set by the user: http://<host>:5678
    val token: String = "",
    val apiKey: String = "",
    val obscode: String = "",
    val observer: String = "",        // VSNET postings are signed with a name, not a code
    val site: String = "",
    val instrument: String = "",
    val device: String = "auto",
    val lat: String = "",
    val lon: String = "",
    val forceVsnet: Boolean = false,   // allow postings from images with a non-linear response
    val photometry: Boolean = true,
    val transients: Boolean = true,
)

data class UiState(
    val settings: Settings = Settings(),
    val health: Health? = null,
    val healthError: String? = null,
    val pickedUri: Uri? = null,
    val pickedName: String = "",
    val uploadProgress: Float = 0f,
    val busy: Boolean = false,
    val error: String? = null,
    val job: JobResult? = null,
    val history: List<JobSummary> = emptyList(),
)

class AppViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = app.getSharedPreferences("skyprobe", Context.MODE_PRIVATE)
    private val _state = MutableStateFlow(UiState(settings = load()))
    val state: StateFlow<UiState> = _state.asStateFlow()
    private var pollJob: Job? = null

    var api = SkyProbeApi(_state.value.settings.server, _state.value.settings.token)
        private set

    init {
        refreshHealth()
    }

    private fun load() = Settings(
        server = prefs.getString("server", null) ?: Settings().server,
        token = prefs.getString("token", "") ?: "",
        apiKey = prefs.getString("apiKey", "") ?: "",
        obscode = prefs.getString("obscode", "") ?: "",
        observer = prefs.getString("observer", "") ?: "",
        site = prefs.getString("site", "") ?: "",
        instrument = prefs.getString("instrument", "") ?: "",
        device = prefs.getString("device", "auto") ?: "auto",
        lat = prefs.getString("lat", "") ?: "",
        lon = prefs.getString("lon", "") ?: "",
        forceVsnet = prefs.getBoolean("forceVsnet", false),
        photometry = prefs.getBoolean("photometry", true),
        transients = prefs.getBoolean("transients", true),
    )

    fun update(block: (Settings) -> Settings) {
        val s = block(_state.value.settings)
        prefs.edit().apply {
            putString("server", s.server); putString("token", s.token); putString("apiKey", s.apiKey)
            putString("obscode", s.obscode); putString("device", s.device)
            putString("observer", s.observer); putString("site", s.site); putString("instrument", s.instrument)
            putString("lat", s.lat); putString("lon", s.lon)
            putBoolean("photometry", s.photometry); putBoolean("transients", s.transients)
            putBoolean("forceVsnet", s.forceVsnet)
        }.apply()
        val serverChanged = s.server != _state.value.settings.server || s.token != _state.value.settings.token
        _state.update { it.copy(settings = s) }
        if (serverChanged) {
            api = SkyProbeApi(s.server, s.token)
            refreshHealth()
        }
    }

    fun refreshHealth() = viewModelScope.launch {
        if (_state.value.settings.server.isBlank()) {
            _state.update { it.copy(health = null, healthError = NOT_CONFIGURED) }
            return@launch
        }
        _state.update { it.copy(health = null, healthError = null) }
        runCatching { api.health() }
            .onSuccess { h -> _state.update { it.copy(health = h, healthError = null) } }
            .onFailure { e -> _state.update { it.copy(healthError = e.message ?: "unreachable") } }
    }

    /**
     * Takes an image from the picker or from another app ("Share"/"Open with").
     *
     * The permission on a shared content:// URI only lasts until this task finishes, and
     * gallery apps hand out one-shot URIs, so the picture is copied into our cache first.
     * That also means the upload survives a rotation or a trip to the background.
     */
    fun importImage(uri: Uri?, sharedBatch: Int = 1) {
        if (uri == null) return
        pollJob?.cancel()
        val ctx = getApplication<Application>()
        val name = runCatching { api.displayName(ctx.contentResolver, uri) }.getOrDefault("image.jpg")
        _state.update {
            it.copy(pickedUri = uri, pickedName = name, job = null, busy = false, error = null, uploadProgress = 0f)
        }
        viewModelScope.launch {
            val local = withContext(Dispatchers.IO) {
                runCatching {
                    val dir = File(ctx.cacheDir, "incoming").apply { mkdirs() }
                    dir.listFiles()?.forEach { it.delete() }
                    val out = File(dir, name.replace(Regex("[^A-Za-z0-9._-]"), "_"))
                    ctx.contentResolver.openInputStream(uri)?.use { input ->
                        out.outputStream().use { input.copyTo(it, 256 * 1024) }
                    } ?: error("cannot open the shared file")
                    out
                }.getOrNull()
            }
            _state.update {
                if (local == null || local.length() == 0L) {
                    it.copy(error = "Could not read the shared image")
                } else {
                    it.copy(
                        pickedUri = Uri.fromFile(local), pickedName = name,
                        error = if (sharedBatch > 1) "Only the first of $sharedBatch images was taken" else it.error,
                    )
                }
            }
        }
    }

    fun analyse(obsTime: String? = null) {
        val st = _state.value
        val uri = st.pickedUri ?: return
        if (st.settings.server.isBlank()) {
            _state.update { it.copy(error = "Set the server address in settings first") }
            return
        }
        _state.update { it.copy(busy = true, error = null, job = null, uploadProgress = 0f) }
        viewModelScope.launch {
            val s = st.settings
            val opts = buildMap {
                put("device", s.device)
                put("photometry", s.photometry.toString())
                put("transients", s.transients.toString())
                if (s.apiKey.isNotBlank()) put("api_key", s.apiKey)
                if (s.obscode.isNotBlank()) put("obscode", s.obscode)
                if (s.lat.isNotBlank()) put("lat", s.lat)
                if (s.lon.isNotBlank()) put("lon", s.lon)
                if (!obsTime.isNullOrBlank()) put("obs_time", obsTime)
            }
            runCatching {
                api.submit(getApplication<Application>().contentResolver, uri, opts) { p ->
                    _state.update { it.copy(uploadProgress = p) }
                }
            }.onSuccess { id -> watch(id) }
                .onFailure { e -> _state.update { it.copy(busy = false, error = e.message ?: "upload failed") } }
        }
    }

    fun watch(id: String) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            while (true) {
                val r = try {
                    api.job(id)
                } catch (e: Exception) {
                    null
                }
                if (r == null) {
                    delay(3000)
                    continue
                }
                _state.update { it.copy(job = r, busy = !r.done && !r.failed, error = if (r.failed) r.error else null) }
                if (r.done || r.failed) break
                delay(2000)
            }
        }
    }

    fun loadHistory() = viewModelScope.launch {
        runCatching { api.jobs() }.onSuccess { h -> _state.update { it.copy(history = h) } }
            .onFailure { e -> _state.update { it.copy(error = e.message) } }
    }

    fun clearJob() {
        pollJob?.cancel()
        _state.update { it.copy(job = null, busy = false, error = null, uploadProgress = 0f) }
    }

    suspend fun aavsoReport(id: String): String = api.text(api.fileUrl(id, "aavso.txt"))

    suspend fun vsnetReport(id: String, includeLimits: Boolean) = with(_state.value.settings) {
        api.vsnet(id, observer, site, instrument, includeLimits, force = forceVsnet)
    }
}
