package net.skyprobe.app.net

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import okio.source
import java.io.IOException
import java.util.concurrent.TimeUnit

// ---------------------------------------------------------------- models (server JSON)

@Serializable
data class Health(
    val status: String = "",
    val version: String = "",
    @SerialName("local_solver") val localSolver: Boolean = false,
    @SerialName("remote_solver_key_configured") val remoteKey: Boolean = false,
    @SerialName("auth_required") val authRequired: Boolean = false,
    @SerialName("max_upload_mb") val maxUploadMb: Int = 0,
    val formats: List<String> = emptyList(),
)

@Serializable
data class Solution(
    val ra: Double = 0.0, val dec: Double = 0.0,
    @SerialName("ra_hms") val raHms: String = "", @SerialName("dec_dms") val decDms: String = "",
    @SerialName("pixel_scale") val pixelScale: Double = 0.0,
    @SerialName("fov_w_deg") val fovW: Double = 0.0, @SerialName("fov_h_deg") val fovH: Double = 0.0,
    @SerialName("rotation_deg") val rotation: Double = 0.0, val parity: String = "",
    @SerialName("gal_l") val galL: Double = 0.0, @SerialName("gal_b") val galB: Double = 0.0,
    val solver: String = "", @SerialName("solve_seconds") val solveSeconds: Double = 0.0,
)

@Serializable
data class Calibration(
    val band: String = "", val catalog: String = "",
    @SerialName("n_comps") val nComps: Int = 0,
    @SerialName("zero_point") val zeroPoint: Double = 0.0,
    @SerialName("color_term") val colorTerm: Double = 0.0,
    val rms: Double? = null,
    @SerialName("limit_mag_5sigma") val limitMag: Double? = null,
    @SerialName("aperture_px") val aperturePx: Double = 0.0,
)

@Serializable
data class CheckStar(val name: String = "", val measured: Double? = null, val catalog: Double? = null)

@Serializable
data class Variable(
    val name: String = "", val type: String = "", val oid: Long = 0,
    val max: Double? = null, val min: Double? = null,
    @SerialName("min_is_amplitude") val minIsAmplitude: Boolean = false,
    val period: Double? = null, val ra: Double = 0.0, val dec: Double = 0.0,
    val x: Double = 0.0, val y: Double = 0.0,
    val mag: Double = 0.0, val err: Double? = null,
    @SerialName("upper_limit") val upperLimit: Boolean = false,
    val snr: Double = 0.0, val airmass: Double? = null,
    val flags: List<String> = emptyList(),
    val check: CheckStar? = null,
    @SerialName("vsx_url") val vsxUrl: String = "",
)

@Serializable
data class KnownMatch(val catalog: String = "", val name: String = "", @SerialName("class") val cls: String = "")

@Serializable
data class Candidate(
    val kind: String = "", val status: String = "", val label: String = "",
    val ra: Double = 0.0, val dec: Double = 0.0, val x: Double = 0.0, val y: Double = 0.0,
    val mag: Double = 0.0, val snr: Double = 0.0,
    @SerialName("fwhm_px") val fwhmPx: Double = 0.0,
    @SerialName("delta_mag") val deltaMag: Double? = null,
    val known: KnownMatch? = null,
)

@Serializable
data class MinorBody(
    val name: String = "", @SerialName("class") val cls: String = "",
    @SerialName("is_comet") val isComet: Boolean = false,
    val ra: Double = 0.0, val dec: Double = 0.0, val x: Double = 0.0, val y: Double = 0.0,
    val vmag: Double? = null, val detected: Boolean = false,
    @SerialName("measured_mag") val measuredMag: Double? = null,
    @SerialName("rate_ra_arcsec_h") val rateRa: Double = 0.0,
    @SerialName("rate_dec_arcsec_h") val rateDec: Double = 0.0,
)

@Serializable
data class ObsTime(@SerialName("utc_mid") val utcMid: String = "", @SerialName("jd_mid") val jdMid: Double = 0.0, val source: String = "")

@Serializable
data class FileInfo(val name: String = "", val format: String = "", val width: Int = 0, val height: Int = 0, val band: String = "")

@Serializable
data class Detections(val count: Int = 0, @SerialName("fwhm_px") val fwhmPx: Double? = null, @SerialName("fwhm_arcsec") val fwhmArcsec: Double? = null)

@Serializable
data class JobResult(
    val id: String = "", val status: String = "", val stage: String = "", val progress: Int = 0,
    val filename: String = "", val error: String? = null, val log: List<String> = emptyList(),
    val warnings: List<String> = emptyList(),
    val file: FileInfo? = null, val time: ObsTime? = null,
    val detections: Detections? = null,
    val solution: Solution? = null, val calibration: Calibration? = null,
    val variables: List<Variable> = emptyList(),
    val candidates: List<Candidate> = emptyList(),
    @SerialName("minor_bodies") val minorBodies: List<MinorBody> = emptyList(),
) {
    val done get() = status == "done"
    val failed get() = status == "failed"
    val unidentified get() = candidates.count { it.status == "unidentified" }
}

@Serializable
data class JobSummary(
    val id: String = "", val filename: String? = null, val status: String = "", val created: Double = 0.0,
    val ra: Double? = null, val dec: Double? = null,
    @SerialName("n_variables") val nVariables: Int = 0,
    @SerialName("n_unidentified") val nUnidentified: Int = 0,
)

@Serializable
data class VsnetSelection(
    val total: Int = 0,
    @SerialName("dropped_survey_id") val droppedSurveyId: Int = 0,
    @SerialName("dropped_flagged") val droppedFlagged: Int = 0,
    @SerialName("dropped_error") val droppedError: Int = 0,
    @SerialName("dropped_limits") val droppedLimits: Int = 0,
    val selected: Int = 0,
    @SerialName("over_limit") val overLimit: Int = 0,
)

@Serializable
data class VsnetReport(
    val to: String = "", val subject: String = "", val body: String = "",
    @SerialName("n_observations") val nObservations: Int = 0,
    val blocked: Boolean = false,
    @SerialName("blocked_reason") val blockedReason: String? = null,
    val selection: VsnetSelection = VsnetSelection(),
)

@Serializable
private data class CreateResponse(val id: String = "")

// ---------------------------------------------------------------- client

class SkyProbeApi(baseUrl: String, private val token: String? = null) {
    val base: String = baseUrl.trim().trimEnd('/').let { if (it.startsWith("http")) it else "http://$it" }

    private val client = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.MINUTES)
        .readTimeout(2, TimeUnit.MINUTES)
        .build()

    private val json = Json { ignoreUnknownKeys = true; isLenient = true; coerceInputValues = true }

    fun fileUrl(jobId: String, name: String) = "$base/api/jobs/$jobId/$name"

    private fun req(url: String) = Request.Builder().url(url).apply { token?.takeIf { it.isNotBlank() }?.let { header("X-API-Key", it) } }

    private suspend fun <T> get(url: String, parse: (String) -> T): T = withContext(Dispatchers.IO) {
        client.newCall(req(url).build()).execute().use { r ->
            val body = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw IOException(errorMessage(body, r.code))
            parse(body)
        }
    }

    private fun errorMessage(body: String, code: Int): String = try {
        Json.parseToJsonElement(body).let { el ->
            el.toString().let { s -> Regex("\"detail\"\\s*:\\s*\"([^\"]*)\"").find(s)?.groupValues?.get(1) ?: "HTTP $code" }
        }
    } catch (e: Exception) { "HTTP $code" }

    suspend fun health(): Health = get("$base/api/health") { json.decodeFromString(it) }

    suspend fun job(id: String): JobResult = get("$base/api/jobs/$id") { json.decodeFromString(it) }

    suspend fun jobs(): List<JobSummary> = get("$base/api/jobs?limit=25") { json.decodeFromString(it) }

    suspend fun text(url: String): String = get(url) { it }

    /** Composes (never sends) a vsnet-obs posting for this job. */
    suspend fun vsnet(jobId: String, observer: String, site: String, instrument: String,
                      includeLimits: Boolean = false, limit: Int = 50): VsnetReport {
        fun enc(v: String) = java.net.URLEncoder.encode(v, "UTF-8")
        val url = "$base/api/jobs/$jobId/vsnet?observer=${enc(observer)}&site=${enc(site)}" +
            "&instrument=${enc(instrument)}&include_limits=$includeLimits&limit=$limit"
        return get(url) { json.decodeFromString(it) }
    }

    /** Uploads the picked image and returns the new job id. */
    suspend fun submit(
        resolver: ContentResolver,
        uri: Uri,
        options: Map<String, String>,
        onProgress: (Float) -> Unit = {},
    ): String = withContext(Dispatchers.IO) {
        val name = displayName(resolver, uri)
        val total = sizeOf(resolver, uri)
        val fileBody = object : RequestBody() {
            override fun contentType() = "application/octet-stream".toMediaType()
            override fun contentLength() = total
            override fun writeTo(sink: BufferedSink) {
                resolver.openInputStream(uri)?.use { input ->
                    input.source().use { src ->
                        var written = 0L
                        val chunk = 256L * 1024
                        while (true) {
                            val n = src.read(sink.buffer, chunk)
                            if (n == -1L) break
                            written += n
                            sink.flush()
                            if (total > 0) onProgress(written.toFloat() / total)
                        }
                    }
                } ?: throw IOException("Cannot read the selected file")
            }
        }
        val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("file", name, fileBody)
        options.filterValues { it.isNotBlank() }.forEach { (k, v) -> builder.addFormDataPart(k, v) }
        client.newCall(req("$base/api/jobs").post(builder.build()).build()).execute().use { r ->
            val body = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw IOException(errorMessage(body, r.code))
            json.decodeFromString<CreateResponse>(body).id
        }
    }

    private fun sizeOf(resolver: ContentResolver, uri: Uri): Long =
        resolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.SIZE)
            if (i >= 0 && c.moveToFirst() && !c.isNull(i)) c.getLong(i) else -1L
        } ?: -1L

    /** The server picks the decoder from the extension, so make sure the name has a sensible one. */
    fun displayName(resolver: ContentResolver, uri: Uri): String {
        val raw = resolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (i >= 0 && c.moveToFirst()) c.getString(i) else null
        } ?: uri.lastPathSegment?.substringAfterLast('/')
        val name = raw?.takeIf { it.isNotBlank() } ?: "upload"
        if (name.substringAfterLast('.', "").length in 2..5) return name
        val ext = when (resolver.getType(uri)) {
            "image/jpeg" -> "jpg"
            "image/png" -> "png"
            "image/heic", "image/heif" -> "heic"
            "image/x-adobe-dng" -> "dng"
            "image/tiff" -> "tif"
            "image/webp" -> "webp"
            else -> "jpg"
        }
        return "$name.$ext"
    }
}
