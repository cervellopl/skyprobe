package net.skyprobe.app.ui

import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import kotlinx.coroutines.launch
import net.skyprobe.app.AppViewModel
import net.skyprobe.app.NOT_CONFIGURED
import net.skyprobe.app.Phone
import net.skyprobe.app.UiState
import net.skyprobe.app.net.Candidate
import net.skyprobe.app.net.JobResult
import net.skyprobe.app.net.MinorBody
import net.skyprobe.app.net.Variable
import kotlin.math.abs

private fun f(v: Double?, d: Int = 2): String = if (v == null || v.isNaN()) "–" else String.format("%.${d}f", v)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppScaffold(state: UiState, vm: AppViewModel) {
    var showSettings by remember { mutableStateOf(false) }
    var showHistory by remember { mutableStateOf(false) }
    val snack = remember { SnackbarHostState() }
    LaunchedEffect(state.error) { state.error?.let { snack.showSnackbar(it) } }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("SkyProbe") },
                navigationIcon = {
                    if (state.job != null) IconButton(onClick = { vm.clearJob() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                    }
                },
                actions = {
                    IconButton(onClick = { showHistory = true; vm.loadHistory() }) { Icon(Icons.Default.History, "History") }
                    IconButton(onClick = { showSettings = true }) { Icon(Icons.Default.Settings, "Settings") }
                },
            )
        },
        snackbarHost = { SnackbarHost(snack) },
    ) { pad ->
        Box(Modifier.padding(pad)) {
            val job = state.job
            if (job != null && job.done) ResultScreen(job, vm) else HomeScreen(state, vm)
        }
    }
    if (showSettings) SettingsDialog(state, vm) { showSettings = false }
    if (showHistory) HistoryDialog(state, vm) { showHistory = false }
}

@Composable
private fun HomeScreen(state: UiState, vm: AppViewModel) {
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { vm.importImage(it) }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        ServerStatus(state, vm)

        ElevatedCard {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Analyse a sky image", style = MaterialTheme.typography.titleMedium)
                Text(
                    "Phone or Seestar frame (JPEG, HEIC, RAW or FITS). The server plate-solves it with " +
                        "astrometry.net, measures every catalogued variable star in the field and looks for " +
                        "objects that are not in the catalogues.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedButton(onClick = { picker.launch("*/*") }, modifier = Modifier.fillMaxWidth()) {
                    Icon(Icons.Default.Image, null); Spacer(Modifier.width(8.dp))
                    Text(if (state.pickedName.isBlank()) "Choose image" else state.pickedName, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                DevicePicker(state, vm)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(state.settings.photometry, { b -> vm.update { it.copy(photometry = b) } })
                    Text("Variable-star photometry", Modifier.weight(1f))
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(state.settings.transients, { b -> vm.update { it.copy(transients = b) } })
                    Text("Search for new objects", Modifier.weight(1f))
                }
                Button(
                    onClick = { vm.analyse() },
                    enabled = state.pickedUri != null && !state.busy,
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Analyse") }
            }
        }

        if (state.busy || state.job != null) {
            val job = state.job
            ElevatedCard {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    val stage = when {
                        job == null && state.uploadProgress > 0f -> "Uploading ${(state.uploadProgress * 100).toInt()} %"
                        job == null -> "Uploading…"
                        job.failed -> "Failed"
                        else -> job.stage.replaceFirstChar { it.uppercase() }
                    }
                    Text(stage, fontWeight = FontWeight.SemiBold)
                    val p = if (job == null) state.uploadProgress * 0.2f else job.progress / 100f
                    LinearProgressIndicator(progress = { p.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
                    job?.error?.let { Text(it, color = Danger, style = MaterialTheme.typography.bodySmall) }
                    job?.log?.takeLast(6)?.let { lines ->
                        Text(lines.joinToString("\n"), fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

@Composable
private fun ServerStatus(state: UiState, vm: AppViewModel) {
    val h = state.health
    val unset = state.healthError == NOT_CONFIGURED
    val text = when {
        h != null -> "Server ${h.version} · solver: " +
            listOfNotNull("local".takeIf { h.localSolver }, "nova".takeIf { h.remoteKey })
                .ifEmpty { listOf("none – add an astrometry.net key") }.joinToString(" + ")
        unset -> "No server yet — open settings (⚙) and enter the address of your SkyProbe server"
        state.healthError != null -> "Server unreachable: ${state.healthError}"
        else -> "Connecting…"
    }
    Surface(
        // "not configured yet" is a nudge, not a failure
        color = if (h != null || unset) MaterialTheme.colorScheme.surfaceVariant
                else MaterialTheme.colorScheme.errorContainer,
        shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth(),
    ) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(text, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
            if (!unset) TextButton(onClick = { vm.refreshHealth() }) { Text("Retry") }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DevicePicker(state: UiState, vm: AppViewModel) {
    val options = listOf(
        "auto" to "Auto-detect", "phone" to "Phone camera",
        "seestar_s50" to "ZWO Seestar S50", "seestar_s30" to "ZWO Seestar S30",
    )
    var open by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = open, onExpandedChange = { open = it }) {
        OutlinedTextField(
            value = options.firstOrNull { it.first == state.settings.device }?.second ?: "Auto-detect",
            onValueChange = {}, readOnly = true, label = { Text("Device") },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(open) },
            modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryNotEditable).fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            options.forEach { (k, label) ->
                DropdownMenuItem(text = { Text(label) }, onClick = {
                    vm.update { it.copy(device = k) }; open = false
                })
            }
        }
    }
}

// ---------------------------------------------------------------- results

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ResultScreen(job: JobResult, vm: AppViewModel) {
    var tab by remember { mutableIntStateOf(0) }
    var query by remember { mutableStateOf("") }
    var imageTall by remember { mutableStateOf(false) }
    val uri = LocalUriHandler.current
    val tabs = listOf("New objects (${job.unidentified})", "Variables (${job.variables.size})",
        "Minor bodies (${job.minorBodies.size})", "Details")
    val variables = job.variables.filter {
        query.isBlank() || it.name.contains(query, true) || it.type.contains(query, true)
    }

    // One scrolling list for the whole screen: the preview and the summary scroll away
    // instead of permanently occupying the space the data needs, and the tabs stay on top.
    LazyColumn(Modifier.fillMaxSize()) {
        item(key = "image") { AnnotatedImage(vm.api.fileUrl(job.id, "annotated.jpg"), imageTall) { imageTall = !imageTall } }
        item(key = "summary") { SummaryRow(job) }
        stickyHeader(key = "tabs") {
            Surface(tonalElevation = 3.dp, shadowElevation = 3.dp) {
                ScrollableTabRow(selectedTabIndex = tab, edgePadding = 8.dp) {
                    tabs.forEachIndexed { i, t ->
                        Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t, maxLines = 1) })
                    }
                }
            }
        }
        when (tab) {
            0 -> candidateItems(job.candidates, uri)
            1 -> {
                item(key = "vartools") { VariableToolbar(job, vm, query) { query = it } }
                variableItems(variables, job.calibration?.band ?: "", uri)
            }
            2 -> minorBodyItems(job.minorBodies, job.time != null)
            else -> detailItems(job)
        }
        item(key = "tail") { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun AnnotatedImage(url: String, tall: Boolean, onToggleHeight: () -> Unit) {
    var scale by remember { mutableFloatStateOf(1f) }
    var ox by remember { mutableFloatStateOf(0f) }
    var oy by remember { mutableFloatStateOf(0f) }
    Box(
        Modifier.fillMaxWidth().height(if (tall) 460.dp else 200.dp).background(Color.Black)
            .pointerInput(Unit) {
                detectTransformGestures { _, pan, zoom, _ ->
                    scale = (scale * zoom).coerceIn(1f, 12f)
                    ox += pan.x; oy += pan.y
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        AsyncImage(
            model = url, contentDescription = "Annotated image",
            modifier = Modifier.fillMaxSize().graphicsLayer(scaleX = scale, scaleY = scale, translationX = ox, translationY = oy),
        )
        Row(Modifier.align(Alignment.TopEnd)) {
            if (abs(scale - 1f) > 0.01f) {
                TextButton(onClick = { scale = 1f; ox = 0f; oy = 0f }) { Text("Reset") }
            }
            TextButton(onClick = onToggleHeight) { Text(if (tall) "Smaller" else "Bigger") }
        }
    }
}

@Composable
private fun SummaryRow(job: JobResult) {
    val s = job.solution
    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
        Text(
            s?.let { "${it.raHms}  ${it.decDms}  ·  ${f(it.pixelScale, 2)}″/px  ·  " +
                if (it.fovW < 2) "${f(it.fovW * 60, 1)}′ × ${f(it.fovH * 60, 1)}′" else "${f(it.fovW, 1)}° × ${f(it.fovH, 1)}°" }
                ?: "not solved",
            style = MaterialTheme.typography.titleSmall,
        )
        val c = job.calibration
        Text(
            buildString {
                append(job.file?.let { "${it.format.uppercase()} ${it.width}×${it.height}" } ?: "")
                c?.let { append("  ·  ${it.band} zp ${f(it.zeroPoint, 2)} ± ${f(it.rms, 3)}  ·  limit ${f(it.limitMag, 1)}") }
                job.time?.let { append("  ·  JD ${f(it.jdMid, 4)}") }
            },
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        // one line each: the full text lives in the Details tab
        if (job.warnings.isNotEmpty()) {
            Text("⚠ ${job.warnings.first()}", style = MaterialTheme.typography.bodySmall, color = Warn,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        if (job.warnings.size > 1) {
            Text("⚠ +${job.warnings.size - 1} more (see Details)",
                style = MaterialTheme.typography.bodySmall, color = Warn)
        }
    }
}

private val CARD = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)

private fun LazyListScope.candidateItems(items: List<Candidate>, uri: UriHandler) {
    if (items.isEmpty()) {
        item { Empty("Nothing unexpected above the catalogue limit.") }
        return
    }
    items(items, key = { "c" + it.ra + it.dec }) { c ->
        ElevatedCard(CARD) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Badge(if (c.status == "unidentified") "NEW?" else "KNOWN",
                        if (c.status == "unidentified") Danger else Warn)
                    Spacer(Modifier.width(8.dp))
                    Text("mag ${f(c.mag)}", fontWeight = FontWeight.SemiBold)
                }
                Text(c.label, style = MaterialTheme.typography.bodyMedium)
                Text("RA ${f(c.ra, 5)}°  Dec ${f(c.dec, 5)}°  ·  SNR ${f(c.snr, 0)}  ·  FWHM ${f(c.fwhmPx, 1)} px",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Row {
                    TextButton(onClick = {
                        uri.openUri("https://aladin.cds.unistra.fr/AladinLite/?target=${f(c.ra, 5)}%20${f(c.dec, 5)}&fov=0.25&survey=P%2FDSS2%2Fcolor")
                    }) { Text("Aladin") }
                    TextButton(onClick = {
                        uri.openUri("https://www.wis-tns.org/search?ra=${f(c.ra, 5)}&decl=${f(c.dec, 5)}&radius=30&coords_unit=arcsec")
                    }) { Text("TNS") }
                }
            }
        }
    }
}

@Composable
private fun VariableToolbar(job: JobResult, vm: AppViewModel, query: String, onQuery: (String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(query, onQuery, label = { Text("Filter") }, singleLine = true, modifier = Modifier.weight(1f))
        Spacer(Modifier.width(8.dp))
        if (job.time != null) {
            TextButton(onClick = {
                scope.launch {
                    runCatching { vm.aavsoReport(job.id) }.onSuccess { text ->
                        val send = Intent(Intent.ACTION_SEND).apply {
                            type = "text/plain"
                            putExtra(Intent.EXTRA_SUBJECT, "AAVSO report ${job.filename}")
                            putExtra(Intent.EXTRA_TEXT, text)
                        }
                        ctx.startActivity(Intent.createChooser(send, "Share AAVSO report"))
                    }
                }
            }) { Text("AAVSO") }
        }
    }
}

private fun LazyListScope.variableItems(items: List<Variable>, band: String, uri: UriHandler) {
    if (items.isEmpty()) {
        item { Empty("No catalogued variables measured.") }
        return
    }
    items(items, key = { "v" + it.oid + it.ra }) { v -> VariableCard(v, band) { uri.openUri(v.vsxUrl) } }
}

@Composable
private fun VariableCard(v: Variable, band: String, onOpen: () -> Unit) {
    ElevatedCard(CARD) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(v.name, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    (if (v.upperLimit) "< " else "") + f(v.mag, 3) + (v.err?.let { " ± ${f(it, 3)}" } ?: "") + " $band",
                    color = if (v.upperLimit) MaterialTheme.colorScheme.onSurfaceVariant else Cyan,
                    fontWeight = FontWeight.SemiBold,
                )
            }
            Text(
                buildString {
                    append(v.type)
                    v.max?.let {
                        append("  ·  VSX ")
                        append(if (v.minIsAmplitude) "${f(it, 2)} (ampl ${f(v.min, 2)})" else "${f(it, 2)}–${f(v.min, 2)}")
                    }
                    v.period?.let { append("  ·  P ${f(it, if (it < 10) 4 else 1)} d") }
                    v.airmass?.let { append("  ·  X ${f(it, 2)}") }
                },
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (v.flags.isNotEmpty()) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) { v.flags.forEach { Badge(it.uppercase(), Warn) } }
            }
            v.check?.let {
                Text("check ${it.name}: ${f(it.measured, 2)} (cat ${f(it.catalog, 2)})",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextButton(onClick = onOpen, contentPadding = PaddingValues(0.dp)) { Text("VSX page") }
        }
    }
}

private fun LazyListScope.minorBodyItems(items: List<MinorBody>, haveTime: Boolean) {
    if (items.isEmpty()) {
        item {
            Empty(if (haveTime) "No known asteroids or comets brighter than the image limit."
                  else "Needs the observation time of the image.")
        }
        return
    }
    items(items, key = { "m" + it.name }) { m ->
        ElevatedCard(CARD) {
            Column(Modifier.padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(m.name, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    if (m.detected) Badge("DETECTED ${f(m.measuredMag, 1)}", Amber) else Badge("NOT SEEN", Color.Gray)
                }
                Text("${m.cls}  ·  predicted V ${f(m.vmag, 1)}  ·  motion ${f(kotlin.math.hypot(m.rateRa, m.rateDec), 1)}″/h",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

private fun LazyListScope.detailItems(job: JobResult) {
    item {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            job.solution?.let {
                Row2("Centre", "${it.raHms} ${it.decDms}")
                Row2("RA / Dec", "${f(it.ra, 5)}° / ${f(it.dec, 5)}°")
                Row2("Pixel scale", "${f(it.pixelScale, 3)}″/px")
                Row2("Rotation", "${f(it.rotation, 1)}° (${it.parity})")
                Row2("Galactic", "l ${f(it.galL, 2)}°, b ${f(it.galB, 2)}°")
                Row2("Solver", "${it.solver} (${f(it.solveSeconds, 1)} s)")
            }
            job.detections?.let { Row2("Stars / FWHM", "${it.count} / ${f(it.fwhmPx, 1)} px (${f(it.fwhmArcsec, 1)}″)") }
            job.calibration?.let {
                Row2("Band / catalogue", "${it.band} · ${it.catalog}")
                Row2("Comparison stars", it.nComps.toString())
                Row2("Zero point", "${f(it.zeroPoint, 3)} ± ${f(it.rms, 3)}")
                Row2("Colour term", f(it.colorTerm, 3))
                Row2("Limiting mag (5σ)", f(it.limitMag, 1))
            }
            job.time?.let { Row2("Time (UTC)", "${it.utcMid}  (${it.source})") }
        }
    }
    if (job.warnings.isNotEmpty()) {
        item {
            Column(Modifier.padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Warnings", fontWeight = FontWeight.SemiBold)
                job.warnings.forEach {
                    Text("⚠ $it", style = MaterialTheme.typography.bodySmall, color = Warn)
                }
            }
        }
    }
    item {
        Column(Modifier.padding(16.dp)) {
            Text("Log", fontWeight = FontWeight.SemiBold)
            Text(job.log.joinToString("\n"), fontFamily = FontFamily.Monospace,
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun Row2(k: String, v: String) = Row(Modifier.fillMaxWidth()) {
    Text(k, Modifier.width(150.dp), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Text(v, style = MaterialTheme.typography.bodySmall)
}

@Composable
private fun Badge(text: String, color: Color) = Surface(color = color.copy(alpha = 0.18f), shape = RoundedCornerShape(99.dp)) {
    Text(text, Modifier.padding(horizontal = 8.dp, vertical = 2.dp), color = color, style = MaterialTheme.typography.labelSmall)
}

@Composable
private fun Empty(text: String) = Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.TopCenter) {
    Text(text, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

// ---------------------------------------------------------------- dialogs

@Composable
private fun SettingsDialog(state: UiState, vm: AppViewModel, onClose: () -> Unit) {
    var s by remember { mutableStateOf(state.settings) }
    AlertDialog(
        onDismissRequest = onClose,
        confirmButton = { TextButton(onClick = { vm.update { s }; onClose() }) { Text("Save") } },
        dismissButton = { TextButton(onClick = onClose) { Text("Cancel") } },
        title = { Text("Settings") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(s.server, { s = s.copy(server = it) }, label = { Text("Server URL") },
                    placeholder = { Text("http://<host or IP>:5678") }, singleLine = true,
                    supportingText = { Text("The machine running the SkyProbe backend, reachable from this phone") })
                OutlinedTextField(s.token, { s = s.copy(token = it) }, label = { Text("Server API token (optional)") }, singleLine = true)
                OutlinedTextField(s.apiKey, { s = s.copy(apiKey = it) }, label = { Text("astrometry.net key (optional)") }, singleLine = true)
                OutlinedTextField(s.obscode, { s = s.copy(obscode = it) }, label = { Text("AAVSO observer code") }, singleLine = true)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(s.lat, { s = s.copy(lat = it) }, label = { Text("Latitude") }, singleLine = true, modifier = Modifier.weight(1f))
                    OutlinedTextField(s.lon, { s = s.copy(lon = it) }, label = { Text("Longitude") }, singleLine = true, modifier = Modifier.weight(1f))
                }
                LocationRow(onLocation = { lat, lon -> s = s.copy(lat = lat, lon = lon) })
                Text("Latitude/longitude are only used to compute the airmass. Images that carry GPS "
                        + "coordinates in their EXIF are handled without this.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        },
    )
}

/** "Use my location" - asks for the permission the first time and fills in the two fields. */
@Composable
private fun LocationRow(onLocation: (String, String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    fun fetch() {
        busy = true
        status = if (Phone.locationEnabled(ctx)) "Waiting for a fix…" else "Location is switched off"
        scope.launch {
            val loc = Phone.location(ctx)
            busy = false
            if (loc == null) {
                status = if (Phone.locationEnabled(ctx)) "No fix - try again outdoors"
                         else "Turn location on in Android settings"
            } else {
                onLocation(String.format("%.5f", loc.latitude), String.format("%.5f", loc.longitude))
                val age = (System.currentTimeMillis() - loc.time) / 60000
                status = "±${loc.accuracy.toInt()} m" + if (age > 1) ", $age min old" else ", just now"
            }
        }
    }

    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted.values.any { it }) fetch() else status = "Permission denied - enter the position by hand"
    }

    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedButton(
            onClick = { if (Phone.hasPermission(ctx)) fetch() else ask.launch(Phone.PERMISSIONS) },
            enabled = !busy,
        ) {
            Icon(Icons.Default.MyLocation, null, Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
            Text("Use my location")
        }
        if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
        status?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun HistoryDialog(state: UiState, vm: AppViewModel, onClose: () -> Unit) {
    AlertDialog(
        onDismissRequest = onClose,
        confirmButton = { TextButton(onClick = onClose) { Text("Close") } },
        title = { Text("Recent analyses") },
        text = {
            if (state.history.isEmpty()) Text("Nothing yet.") else
                LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    items(state.history) { j ->
                        ListItem(
                            headlineContent = { Text(j.filename ?: j.id, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                            supportingContent = {
                                Text("${j.status} · ${j.nVariables} variables · ${j.nUnidentified} unidentified",
                                    style = MaterialTheme.typography.bodySmall)
                            },
                            modifier = Modifier.clip(RoundedCornerShape(8.dp)).clickable { vm.watch(j.id); onClose() },
                        )
                    }
                }
        },
    )
}
