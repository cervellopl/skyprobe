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
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers
import java.io.File
import net.skyprobe.app.net.CompareResult
import net.skyprobe.app.net.Health
import net.skyprobe.app.net.JobResult
import net.skyprobe.app.net.JobSummary
import net.skyprobe.app.net.NearbyImage
import net.skyprobe.app.net.SkyProbeApi
import net.skyprobe.app.net.Upload

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
    // photometry, in units of the measured star width (FWHM); blank means "from the seeing"
    val aperture: String = "",
    val annulusIn: String = "",
    val annulusOut: String = "",
    val snrMin: String = "",           // minimum SNR for a new-object candidate
    val survey: String = "dss2",       // survey the close-up is blinked against
    val photometry: Boolean = true,
    val transients: Boolean = true,
)

data class PickedFile(val uri: Uri, val name: String)

/** One file's progress through a multi-image upload (several images picked or shared at once). */
data class QueueItem(
    val name: String,
    val jobId: String? = null,
    val status: String = "queued",     // queued | uploading | running | done | failed
    val progress: Int = 0,
    val error: String? = null,
)

data class UiState(
    val settings: Settings = Settings(),
    val health: Health? = null,
    val healthError: String? = null,
    val picked: List<PickedFile> = emptyList(),
    val queue: List<QueueItem> = emptyList(),   // non-empty while a multi-image upload is running
    val uploadProgress: Float = 0f,
    val busy: Boolean = false,
    val error: String? = null,
    val job: JobResult? = null,
    val history: List<JobSummary> = emptyList(),
    val duplicate: Upload? = null,     // the picture is already on the server; ask what to do
)

class AppViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = app.getSharedPreferences("skyprobe", Context.MODE_PRIVATE)
    private val _state = MutableStateFlow(UiState(settings = load()))
    val state: StateFlow<UiState> = _state.asStateFlow()
    private var pollJob: Job? = null
    private var lastObsTime: String? = null

    var api = SkyProbeApi(_state.value.settings.server, _state.value.settings.token)
        private set

    init {
        refreshHealth()
        // A foreground service (AnalysisService) keeps this process alive while work is in
        // progress, so the screen turning off or the user switching apps does not let Android
        // kill the upload/polling coroutines below - see AnalysisService's own doc comment.
        viewModelScope.launch {
            state.map { it.busy to progressText(it) }.distinctUntilChanged().collect { (busy, text) ->
                val ctx = getApplication<Application>()
                if (busy) AnalysisService.start(ctx, text) else AnalysisService.stop(ctx)
            }
        }
    }

    /** What the persistent notification shows while [AnalysisService] is running. */
    private fun progressText(s: UiState): String = when {
        s.queue.isNotEmpty() -> {
            val finished = s.queue.count { it.status == "done" || it.status == "failed" }
            val current = s.queue.getOrNull(finished.coerceAtMost(s.queue.size - 1))
            "Analysing image ${(finished + 1).coerceAtMost(s.queue.size)} of ${s.queue.size}" +
                (current?.takeIf { it.status != "done" && it.status != "failed" }?.let { ": ${it.name}" } ?: "")
        }
        s.job != null && s.job.running -> "${s.job.stage.replaceFirstChar { it.uppercase() }} (${s.job.progress}%)"
        s.uploadProgress > 0f -> "Uploading ${(s.uploadProgress * 100).toInt()}%"
        else -> "Working…"
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
        aperture = prefs.getString("aperture", "") ?: "",
        annulusIn = prefs.getString("annulusIn", "") ?: "",
        annulusOut = prefs.getString("annulusOut", "") ?: "",
        snrMin = prefs.getString("snrMin", "") ?: "",
        survey = prefs.getString("survey", "dss2") ?: "dss2",
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
            putString("aperture", s.aperture); putString("annulusIn", s.annulusIn)
            putString("annulusOut", s.annulusOut); putString("snrMin", s.snrMin)
            putString("survey", s.survey)
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
     * Takes one or more images from the picker or from another app ("Share"/"Open with").
     *
     * The permission on a shared content:// URI only lasts until this task finishes, and
     * gallery apps hand out one-shot URIs, so every picture is copied into our cache first.
     * That also means the upload survives a rotation or a trip to the background.
     */
    fun importImages(uris: List<Uri>) {
        if (uris.isEmpty()) return
        pollJob?.cancel()
        val ctx = getApplication<Application>()
        val names = uris.map { runCatching { api.displayName(ctx.contentResolver, it) }.getOrDefault("image.jpg") }
        _state.update {
            it.copy(
                picked = uris.zip(names) { u, n -> PickedFile(u, n) },
                job = null, busy = false, error = null, uploadProgress = 0f, queue = emptyList(),
            )
        }
        viewModelScope.launch {
            val copies = withContext(Dispatchers.IO) {
                val dir = File(ctx.cacheDir, "incoming").apply { mkdirs() }
                dir.listFiles()?.forEach { it.delete() }
                uris.zip(names).mapIndexedNotNull { i, (uri, name) ->
                    runCatching {
                        val out = File(dir, "${i}_${name.replace(Regex("[^A-Za-z0-9._-]"), "_")}")
                        ctx.contentResolver.openInputStream(uri)?.use { input ->
                            out.outputStream().use { input.copyTo(it, 256 * 1024) }
                        } ?: error("cannot open the shared file")
                        PickedFile(Uri.fromFile(out), name)
                    }.getOrNull()
                }
            }
            _state.update {
                if (copies.isEmpty()) it.copy(error = "Could not read the shared image" + if (uris.size > 1) "s" else "")
                else it.copy(picked = copies)
            }
        }
    }

    private fun buildOpts(s: Settings, obsTime: String?) = buildMap {
        put("device", s.device)
        put("photometry", s.photometry.toString())
        put("transients", s.transients.toString())
        if (s.apiKey.isNotBlank()) put("api_key", s.apiKey)
        if (s.obscode.isNotBlank()) put("obscode", s.obscode)
        if (s.lat.isNotBlank()) put("lat", s.lat)
        if (s.lon.isNotBlank()) put("lon", s.lon)
        if (!obsTime.isNullOrBlank()) put("obs_time", obsTime)
        if (s.aperture.isNotBlank()) put("aperture", s.aperture)
        if (s.annulusIn.isNotBlank()) put("annulus_in", s.annulusIn)
        if (s.annulusOut.isNotBlank()) put("annulus_out", s.annulusOut)
        if (s.snrMin.isNotBlank()) put("snr_min", s.snrMin)
    }

    fun analyse(obsTime: String? = null, allowDuplicate: Boolean = false) {
        val st = _state.value
        lastObsTime = obsTime
        if (st.settings.server.isBlank()) {
            _state.update { it.copy(error = "Set the server address in settings first") }
            return
        }
        if (st.picked.size > 1) { analyseQueue(obsTime); return }
        val uri = st.picked.firstOrNull()?.uri ?: return
        _state.update { it.copy(busy = true, error = null, job = null, uploadProgress = 0f, duplicate = null) }
        viewModelScope.launch {
            val opts = buildOpts(st.settings, obsTime)
            runCatching {
                api.submit(getApplication<Application>().contentResolver, uri, opts, allowDuplicate) { p ->
                    _state.update { it.copy(uploadProgress = p) }
                }
            }.onSuccess { up ->
                if (up.duplicate) _state.update { it.copy(busy = false, duplicate = up) } else watch(up.id)
            }.onFailure { e -> _state.update { it.copy(busy = false, error = e.message ?: "upload failed") } }
        }
    }

    /**
     * Several images picked or shared at once: uploaded one at a time (the server itself works
     * one job at a time by default) and tracked in [UiState.queue] instead of the single-job
     * progress bar. A picture the server already holds is just opened, like a normal duplicate
     * would be - there is no per-file dialog to interrupt a batch.
     */
    private fun analyseQueue(obsTime: String?) = viewModelScope.launch {
        val st = _state.value
        val ctx = getApplication<Application>()
        val opts = buildOpts(st.settings, obsTime)
        _state.update { it.copy(busy = true, error = null, job = null, queue = st.picked.map { p -> QueueItem(p.name) }) }
        fun updateItem(i: Int, block: (QueueItem) -> QueueItem) =
            _state.update { s -> s.copy(queue = s.queue.mapIndexed { j, q -> if (j == i) block(q) else q }) }
        var lastDoneId: String? = null
        for ((i, pf) in st.picked.withIndex()) {
            updateItem(i) { it.copy(status = "uploading") }
            val up = runCatching { api.submit(ctx.contentResolver, pf.uri, opts, allowDuplicate = false) {} }
                .onFailure { e -> updateItem(i) { it.copy(status = "failed", error = e.message ?: "upload failed") } }
                .getOrNull() ?: continue
            updateItem(i) { it.copy(status = "running", jobId = up.id) }
            var done = false
            while (!done) {
                val r = runCatching { api.job(up.id) }.getOrNull()
                if (r == null) { delay(2000); continue }
                updateItem(i) {
                    it.copy(progress = r.progress, status = if (r.failed) "failed" else if (r.done) "done" else "running",
                            error = if (r.failed) r.error else null)
                }
                if (r.failed) done = true
                else if (r.done) { done = true; lastDoneId = up.id }
                else delay(1500)
            }
        }
        _state.update { it.copy(busy = false) }
        lastDoneId?.let { watch(it) }
    }

    /** The user chose to analyse a picture the server already has. */
    fun analyseAnyway() = analyse(lastObsTime, allowDuplicate = true)

    fun openDuplicate() {
        val id = _state.value.duplicate?.id ?: return
        _state.update { it.copy(duplicate = null) }
        watch(id)
    }

    fun dismissDuplicate() = _state.update { it.copy(duplicate = null) }

    /** Runs the pipeline again on the copy the server kept - for a job that broke or was interrupted. */
    fun rerun(id: String) = viewModelScope.launch {
        pollJob?.cancel()
        _state.update { it.copy(busy = true, error = null, job = null, uploadProgress = 0f) }
        runCatching { api.rerun(id) }
            .onSuccess { watch(id) }
            .onFailure { e -> _state.update { it.copy(busy = false, error = e.message ?: "could not re-analyse") } }
    }

    fun deleteJob(id: String, onDone: () -> Unit = {}) = viewModelScope.launch {
        runCatching { api.deleteJob(id) }
            .onSuccess {
                if (_state.value.job?.id == id) clearJob()
                _state.update { st -> st.copy(history = st.history.filterNot { it.id == id }) }
                onDone()
            }
            .onFailure { e -> _state.update { it.copy(error = e.message ?: "could not delete") } }
    }

    fun watch(id: String) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            var misses = 0
            while (true) {
                val r = try {
                    api.job(id)
                } catch (e: Exception) {
                    // a vanished job (deleted, pruned) would otherwise be polled for ever
                    if (++misses >= 5) {
                        _state.update { it.copy(busy = false, error = e.message ?: "the server does not answer") }
                        break
                    }
                    null
                }
                if (r == null) {
                    delay(3000)
                    continue
                }
                misses = 0
                _state.update { it.copy(job = r, busy = r.running, error = if (r.failed) r.error else null) }
                if (!r.running) break
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
        _state.update { it.copy(job = null, busy = false, error = null, uploadProgress = 0f, duplicate = null) }
    }

    suspend fun dbStats() = api.dbStats()

    suspend fun dbStars(query: String, repeatedOnly: Boolean) =
        api.dbStars(query, minPoints = if (repeatedOnly) 2 else 1)

    suspend fun dbStar(name: String) = api.dbStar(name)

    suspend fun dbRestore(uri: Uri, merge: Boolean) =
        api.dbRestore(getApplication<Application>().contentResolver, uri, merge)

    suspend fun aavsoReport(id: String): String = api.text(api.fileUrl(id, "aavso.txt"))

    suspend fun nearby(jobId: String): List<NearbyImage> = api.nearby(jobId)

    suspend fun compare(jobIds: List<String>): CompareResult = api.compare(jobIds)

    suspend fun stack(jobIds: List<String>): Upload = api.stack(jobIds)

    suspend fun vsnetReport(id: String, includeLimits: Boolean) = with(_state.value.settings) {
        api.vsnet(id, observer, site, instrument, includeLimits, force = forceVsnet)
    }
}
