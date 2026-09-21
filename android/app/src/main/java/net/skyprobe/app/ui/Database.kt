package net.skyprobe.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import kotlinx.coroutines.launch
import net.skyprobe.app.AppViewModel
import net.skyprobe.app.net.CurvePoint
import net.skyprobe.app.net.DbStats
import net.skyprobe.app.net.StarSummary
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

private val UTC = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }

fun jdToText(jd: Double?): String =
    if (jd == null) "–" else UTC.format(Date(((jd - 2440587.5) * 86400000.0).toLong()))

/**
 * The measurement archive: every analysed image contributes its stars, so a single
 * frame becomes a point on a light curve.
 */
@Composable
fun DatabaseScreen(vm: AppViewModel, onClose: () -> Unit) {
    var stats by remember { mutableStateOf<DbStats?>(null) }
    var stars by remember { mutableStateOf<List<StarSummary>>(emptyList()) }
    var query by remember { mutableStateOf("") }
    var repeatedOnly by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var selected by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) { runCatching { vm.dbStats() }.onSuccess { stats = it }.onFailure { error = it.message } }
    LaunchedEffect(query, repeatedOnly) {
        loading = true
        runCatching { vm.dbStars(query, repeatedOnly) }
            .onSuccess { stars = it; error = null }
            .onFailure { error = it.message }
        loading = false
    }

    val star = selected
    if (star != null) {
        LightCurveScreen(vm, star) { selected = null }
        return
    }

    Column(Modifier.fillMaxSize()) {
        TopRow("Measurement archive", onClose)
        stats?.let { s ->
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Stat(s.images.toString(), "images")
                Stat(s.measurements.toString(), "measurements")
                Stat(s.stars.toString(), "stars")
                Stat(s.nights.toString(), "nights")
                Stat(String.format(Locale.US, "%.1f MB", s.dbBytes / 1048576.0), "database")
            }
        }
        BackupRow(vm)
        Row(Modifier.padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(query, { query = it }, label = { Text("Search a star") },
                singleLine = true, modifier = Modifier.weight(1f))
        }
        Row(Modifier.padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Checkbox(repeatedOnly, { repeatedOnly = it })
            Text("only stars measured more than once", style = MaterialTheme.typography.bodySmall)
        }
        when {
            error != null -> Box(Modifier.fillMaxSize().padding(24.dp)) { Text(error!!, color = Danger) }
            loading && stars.isEmpty() -> Box(Modifier.fillMaxSize().padding(24.dp)) { Text("Loading…") }
            stars.isEmpty() -> Box(Modifier.fillMaxSize().padding(24.dp)) {
                Text("Nothing in the archive matches.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            else -> LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(stars, key = { it.name }) { s ->
                    ElevatedCard(Modifier.fillMaxWidth().clickable { selected = s.name }) {
                        Column(Modifier.padding(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(s.name, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f),
                                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Badge("${s.points} pt", if (s.points > 1) Cyan else Color.Gray)
                            }
                            Text(buildString {
                                s.type?.takeIf { it.isNotBlank() }?.let { append(it).append("  ·  ") }
                                append("${f(s.brightest, 2)} – ${f(s.faintest, 2)} mag")
                                if ((s.amplitude ?: 0.0) > 0.05) append("  ·  range ${f(s.amplitude, 2)}")
                                append("  ·  last ${jdToText(s.lastJd)}")
                            }, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
        }
    }
}

/** Download a copy of the archive, or put one back. */
@Composable
private fun BackupRow(vm: AppViewModel) {
    val uriHandler = LocalUriHandler.current
    val scope = rememberCoroutineScope()
    var pending by remember { mutableStateOf<Uri?>(null) }
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { pending = it }

    fun run(uri: Uri, merge: Boolean) {
        pending = null
        busy = true
        status = if (merge) "Merging…" else "Restoring…"
        scope.launch {
            runCatching { vm.dbRestore(uri, merge) }
                .onSuccess {
                    status = "${it.mode}: ${it.before.images} → ${it.after.images} images, " +
                        "safety copy ${it.safetyCopyName}"
                }
                .onFailure { status = it.message ?: "restore failed" }
            busy = false
        }
    }

    Column(Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedButton(onClick = { uriHandler.openUri(vm.api.backupUrl()) }, enabled = !busy,
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
                Text("Download backup", style = MaterialTheme.typography.labelLarge)
            }
            OutlinedButton(onClick = { picker.launch("*/*") }, enabled = !busy,
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
                Text("Restore…", style = MaterialTheme.typography.labelLarge)
            }
            if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
        }
        status?.let {
            Text(it, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }

    pending?.let { uri ->
        AlertDialog(
            onDismissRequest = { pending = null },
            title = { Text("Restore the archive") },
            text = {
                Text("Merge keeps what is here and adds the images that are missing.\n\n" +
                     "Replace swaps the archive for the backup - a safety copy of the current one " +
                     "is written on the server first.")
            },
            confirmButton = { TextButton(onClick = { run(uri, merge = true) }) { Text("Merge") } },
            dismissButton = {
                Row {
                    TextButton(onClick = { run(uri, merge = false) }) { Text("Replace", color = Danger) }
                    TextButton(onClick = { pending = null }) { Text("Cancel") }
                }
            },
        )
    }
}

@Composable
private fun LightCurveScreen(vm: AppViewModel, name: String, onBack: () -> Unit) {
    var points by remember { mutableStateOf<List<CurvePoint>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(name) {
        runCatching { vm.dbStar(name) }.onSuccess { points = it.points }.onFailure { error = it.message }
    }
    Column(Modifier.fillMaxSize()) {
        TopRow(name, onBack)
        if (error != null) {
            Box(Modifier.fillMaxSize().padding(24.dp)) { Text(error!!, color = Danger) }
            return@Column
        }
        LightCurve(points, Modifier.fillMaxWidth().height(220.dp).padding(horizontal = 12.dp, vertical = 8.dp))
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items(points, key = { it.jobId + it.jd }) { p ->
                Surface(color = MaterialTheme.colorScheme.surfaceVariant, shape = RoundedCornerShape(8.dp)) {
                    Column(Modifier.fillMaxWidth().padding(10.dp)) {
                        Row {
                            Text(jdToText(p.jd), Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                            Text((if (p.upperLimit == 1) "< " else "") + f(p.mag, 3) +
                                (p.err?.let { " ± ${f(it, 3)}" } ?: "") + " " + (p.band ?: ""),
                                fontWeight = FontWeight.SemiBold,
                                color = if (p.upperLimit == 1) MaterialTheme.colorScheme.onSurfaceVariant else Cyan)
                        }
                        Text(listOfNotNull(p.camera, p.filename, p.flags?.takeIf { it.isNotBlank() },
                            "non-linear".takeIf { p.nonlinear == 1 }).joinToString("  ·  "),
                            style = MaterialTheme.typography.labelSmall,
                            color = if (p.nonlinear == 1) Warn else MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

/** Magnitudes run the astronomer's way: brighter is higher. */
@Composable
private fun LightCurve(points: List<CurvePoint>, modifier: Modifier = Modifier) {
    val dated = points.filter { it.jd != null && it.mag != null }
    val grid = MaterialTheme.colorScheme.outlineVariant
    val dot = Cyan
    val limitColour = MaterialTheme.colorScheme.onSurfaceVariant
    if (dated.isEmpty()) {
        Box(modifier, contentAlignment = Alignment.Center) { Text("No dated measurements yet") }
        return
    }
    Canvas(modifier.background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(8.dp))) {
        val padL = 8f; val padR = 8f; val padT = 12f; val padB = 12f
        val jd0 = dated.minOf { it.jd!! }; val jd1 = dated.maxOf { it.jd!! }
        val spanX = (jd1 - jd0).takeIf { it > 0 } ?: 1.0
        val m0 = dated.minOf { it.mag!! }; val m1 = dated.maxOf { it.mag!! }
        val padY = ((m1 - m0) * 0.15).coerceAtLeast(0.05)
        val lo = m0 - padY; val hi = m1 + padY
        fun px(jd: Double) = padL + ((jd - jd0) / spanX * (size.width - padL - padR)).toFloat()
        fun py(m: Double) = padT + ((m - lo) / (hi - lo) * (size.height - padT - padB)).toFloat()

        listOf(lo, (lo + hi) / 2, hi).forEach { m ->
            drawLine(grid, Offset(padL, py(m)), Offset(size.width - padR, py(m)), strokeWidth = 1f)
        }
        dated.forEach { p ->
            val x = px(p.jd!!); val y = py(p.mag!!)
            p.err?.takeIf { it > 0 }?.let {
                drawLine(dot.copy(alpha = .5f), Offset(x, py(p.mag - it)), Offset(x, py(p.mag + it)), strokeWidth = 2f)
            }
            if (p.upperLimit == 1) drawCircle(limitColour, 5f, Offset(x, y), style = Stroke(width = 2f))
            else drawCircle(dot, 6f, Offset(x, y))
        }
    }
}

@Composable
private fun TopRow(title: String, onBack: () -> Unit) = Row(
    Modifier.fillMaxWidth().padding(start = 4.dp, end = 12.dp, top = 4.dp),
    verticalAlignment = Alignment.CenterVertically,
) {
    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
    Text(title, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
private fun Stat(value: String, label: String) = Column(horizontalAlignment = Alignment.CenterHorizontally) {
    Text(value, fontWeight = FontWeight.SemiBold)
    Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}
