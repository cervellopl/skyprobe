"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private mode */ } },
};
const COLORS = { unidentified: "#ff5a5f", known: "#ffd23f", variable: "#5cd0ff", limit: "#8b97b0", minor: "#ffa94d", minorMiss: "#9c7a55" };
let job = null, pollTimer = null, view = { z: 1, x: 0, y: 0 }, selected = null;
let display = { brightness: Number(store.get("sp_bright")) || 1, contrast: Number(store.get("sp_contrast")) || 1 };

// ---------------------------------------------------------------- server status
fetch("/api/health").then((r) => r.json()).then((h) => {
  const s = $("#serverStatus");
  const solvers = [h.local_solver && "local", h.remote_solver_key_configured && "nova"].filter(Boolean);
  s.textContent = solvers.length ? `solver: ${solvers.join(" + ")}` : "no solver – add API key";
  s.classList.toggle("ok", solvers.length > 0);
  $("#file").accept = h.formats.map((f) => "." + f).join(",");
}).catch(() => { $("#serverStatus").textContent = "offline"; });

// ---------------------------------------------------------------- form
for (const id of ["apikey", "obscode", "observer", "site", "instrument"]) { const v = store.get("sp_" + id); if (v) $("#" + id).value = v; }
for (const k of ["lat", "lon"]) { const v = store.get("sp_" + k); if (v) $(`[name=${k}]`).value = v; }

const drop = $("#drop"), fileInput = $("#file");
function setFile(f) {
  if (!f) return;
  const dt = new DataTransfer(); dt.items.add(f); fileInput.files = dt.files;
  $("#fileName").textContent = `${f.name} · ${(f.size / 1048576).toFixed(1)} MB`;
  $("#btnSubmit").disabled = false;
}
fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

$("#btnGeo").addEventListener("click", () => {
  navigator.geolocation?.getCurrentPosition((p) => {
    $("[name=lat]").value = p.coords.latitude.toFixed(4); $("[name=lon]").value = p.coords.longitude.toFixed(4);
    store.set("sp_lat", $("[name=lat]").value); store.set("sp_lon", $("[name=lon]").value);
  }, (err) => alert("Location unavailable: " + err.message));
});

$("#form").addEventListener("submit", (e) => {
  e.preventDefault();
  const fd = new FormData($("#form"));
  for (const k of ["photometry", "transients"]) fd.set(k, $(`[name=${k}]`).checked ? "true" : "false");
  const t = fd.get("obs_time"); if (t) fd.set("obs_time", t.length === 16 ? t + ":00" : t);
  for (const [k, v] of [...fd.entries()]) if (v === "" && k !== "file") fd.delete(k);
  for (const id of ["apikey", "obscode", "observer", "site", "instrument"]) store.set("sp_" + id, $("#" + id).value);
  upload(fd);
});

function upload(fd) {
  showProgress("Uploading…", 0, "");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (e) => e.lengthComputable && showProgress("Uploading…", Math.round((e.loaded / e.total) * 100), "");
  xhr.onload = () => {
    let r = {}; try { r = JSON.parse(xhr.responseText); } catch { /* ignore */ }
    if (xhr.status >= 300) { showProgress("Upload failed", 0, r.detail || xhr.statusText, true); return; }
    location.hash = "job=" + r.id;
    poll(r.id);
  };
  xhr.onerror = () => showProgress("Upload failed", 0, "network error", true);
  xhr.send(fd);
}

function showProgress(stage, pct, log, err) {
  $("#progressCard").classList.remove("hidden");
  $("#stage").textContent = stage; $("#stage").classList.toggle("error", !!err);
  $("#pct").textContent = pct ? pct + "%" : ""; $("#barFill").style.width = (pct || 0) + "%";
  $("#log").textContent = log;
  $("#log").scrollTop = 1e9;
}

async function poll(id) {
  clearTimeout(pollTimer);
  let r;
  try { r = await (await fetch(`/api/jobs/${id}`)).json(); } catch { pollTimer = setTimeout(() => poll(id), 3000); return; }
  if (r.detail) { showProgress("Unknown job", 0, r.detail, true); return; }
  const log = (r.log || []).join("\n");
  if (r.status === "failed") { showProgress("Failed: " + (r.error || ""), 0, log, true); return; }
  if (r.status !== "done") { showProgress(cap(r.stage || r.status) + "…", r.progress, log); pollTimer = setTimeout(() => poll(id), 1500); return; }
  $("#progressCard").classList.add("hidden");
  render(r);
}
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------------------------------------------------------------- results
function render(r) {
  job = r; selected = null;
  $("#results").classList.remove("hidden");
  const w = r.warnings || [];
  $("#warnings").classList.toggle("hidden", !w.length);
  $("#warnings").innerHTML = w.map((x) => `<div>⚠ ${esc(x)}</div>`).join("");
  renderSolution(r); renderCalib(r); renderCandidates(r); renderVariables(r); renderMinor(r);
  $("#fullLog").textContent = (r.log || []).join("\n") + "\n\nmeta: " + JSON.stringify(r.meta, null, 1);
  const base = `/api/jobs/${r.id}/`;
  $("#dlCand").href = base + "candidates.csv"; $("#dlPhot").href = base + "photometry.csv"; $("#dlAavso").href = base + "aavso.txt";
  $("#dlAavso").classList.toggle("hidden", !r.time);
  $("#btnVsnet").classList.toggle("hidden", !r.time);
  const img = $("#img");
  $("#selectedCard").classList.add("hidden");
  img.onload = () => { $("#overlay").setAttribute("viewBox", `0 0 ${img.naturalWidth} ${img.naturalHeight}`);
    applyDisplay();
    $("#overlay").setAttribute("width", img.naturalWidth); $("#overlay").setAttribute("height", img.naturalHeight); fit(); drawOverlay(); };
  img.src = base + "preview.jpg";
  $("#results").scrollIntoView({ behavior: "smooth" });
}

function renderSolution(r) {
  const s = r.solution;
  if (!s) { $("#solutionCard").innerHTML = "<h2>Astrometry</h2><p class='empty'>not solved</p>"; return; }
  const f = r.file || {}, t = r.time;
  const fov = s.fov_w_deg < 2 ? `${fmt(s.fov_w_deg * 60, 1)}′ × ${fmt(s.fov_h_deg * 60, 1)}′` : `${fmt(s.fov_w_deg, 1)}° × ${fmt(s.fov_h_deg, 1)}°`;
  $("#solutionCard").innerHTML = `<h2>Astrometry</h2>
    <dl class="kv">
      <dt>Centre</dt><dd>${esc(s.ra_hms)} &nbsp;${esc(s.dec_dms)}</dd>
      <dt>RA / Dec</dt><dd>${fmt(s.ra, 5)}° / ${fmt(s.dec, 5)}°</dd>
      <dt>Pixel scale</dt><dd>${fmt(s.pixel_scale, 3)}″/px</dd>
      <dt>Field</dt><dd>${fov}</dd>
      <dt>Rotation</dt><dd>${fmt(s.rotation_deg, 1)}° (${esc(s.parity)})</dd>
      <dt>Galactic</dt><dd>l ${fmt(s.gal_l, 2)}°, b ${fmt(s.gal_b, 2)}°</dd>
      <dt>Image</dt><dd>${esc(f.width)}×${esc(f.height)} ${esc((f.format || "").toUpperCase())}${f.linear ? "" : " (non-linear)"}</dd>
      <dt>Stars / FWHM</dt><dd>${r.detections?.count} / ${fmt(r.detections?.fwhm_px, 1)} px (${fmt(r.detections?.fwhm_arcsec, 1)}″)</dd>
      <dt>Time (UTC)</dt><dd>${t ? esc(t.utc_mid.replace("T", " ").slice(0, 19)) + ` <span class="muted small">${esc(t.source)}</span>` : "–"}</dd>
      <dt>Solver</dt><dd>${esc(s.solver)} <span class="muted small">${fmt(s.solve_seconds, 1)} s</span></dd>
    </dl>
    <div class="links">
      <a class="btn small" href="/api/jobs/${r.id}/wcs.fits">WCS FITS</a>
      <a class="btn small" href="/api/jobs/${r.id}/annotated.jpg" target="_blank">Annotated JPEG</a>
      <a class="btn small" target="_blank" rel="noopener" href="${aladin(s.ra, s.dec, Math.max(s.fov_w_deg, s.fov_h_deg) * 1.2)}">Aladin</a>
    </div>`;
}

function renderCalib(r) {
  const c = r.calibration;
  const v = r.variables || [], cands = r.candidates || [];
  const unid = cands.filter((x) => x.status === "unidentified").length;
  $("#calibCard").innerHTML = `<h2>Summary</h2>
    <div class="stat" style="margin:10px 0 14px">
      <div><b style="color:var(--red)">${unid}</b><span class="muted small">unidentified</span></div>
      <div><b>${v.filter((x) => !x.upper_limit).length}</b><span class="muted small">variables measured</span></div>
      <div><b>${(r.minor_bodies || []).length}</b><span class="muted small">minor bodies</span></div>
    </div>
    ${c ? `<dl class="kv">
      <dt>Band</dt><dd>${esc(c.band)} <span class="muted small">(${esc(c.catalog)})</span></dd>
      <dt>Comparison stars</dt><dd>${c.n_comps} of ${c.n_gaia_in_field}</dd>
      <dt>Zero point</dt><dd>${fmt(c.zero_point, 3)} ± ${fmt(c.rms, 3)} mag</dd>
      <dt>Colour term</dt><dd>${fmt(c.color_term, 3)}</dd>
      <dt>Limiting mag (5σ)</dt><dd>${fmt(c.limit_mag_5sigma, 1)}</dd>
      <dt>Aperture</dt><dd>${fmt(c.aperture_px, 1)} px</dd>
    </dl>` : `<p class="empty">No photometric calibration.</p>`}`;
}

function sky(o) { return `${fmt(o.ra, 5)} ${o.dec >= 0 ? "+" : ""}${fmt(o.dec, 5)}`; }
function aladin(ra, dec, fov = 0.25) { return `https://aladin.cds.unistra.fr/AladinLite/?target=${ra.toFixed(5)}%20${dec.toFixed(5)}&fov=${fov.toFixed(3)}&survey=P%2FDSS2%2Fcolor`; }
function tns(o) { return `https://www.wis-tns.org/search?ra=${o.ra.toFixed(5)}&decl=${o.dec.toFixed(5)}&radius=30&coords_unit=arcsec`; }

function table(el, cols, rows, onClick, empty) {
  if (!rows.length) { el.innerHTML = `<p class="empty">${empty}</p>`; return; }
  let sortKey = null, dir = 1;
  const draw = () => {
    const data = sortKey === null ? rows : [...rows].sort((a, b) => {
      const x = cols[sortKey].sort(a), y = cols[sortKey].sort(b);
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
    el.innerHTML = `<div class="tbl"><table><thead><tr>${cols.map((c, i) => `<th data-i="${i}">${c.h}${sortKey === i ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
      <tbody>${data.map((r) => `<tr data-k="${esc(r._key)}">${cols.map((c) => `<td class="${c.cls || ""}">${c.v(r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    el.querySelectorAll("th").forEach((th) => th.onclick = () => { const i = +th.dataset.i; if (!cols[i].sort) return; dir = sortKey === i ? -dir : 1; sortKey = i; draw(); });
    el.querySelectorAll("tbody tr").forEach((tr) => tr.onclick = (e) => { if (e.target.closest("a")) return; onClick(rows.find((r) => r._key === tr.dataset.k)); });
  };
  draw();
}

function renderCandidates(r) {
  const rows = (r.candidates || []).map((c, i) => ({ ...c, _key: "c" + i, _type: "candidates" }));
  const badge = (c) => c.status === "unidentified" ? `<span class="badge b-red">new?</span>` : `<span class="badge b-yellow">known</span>`;
  table($("#candidates"), [
    { h: "", v: badge, sort: (c) => c.status },
    { h: "Object", v: (c) => esc(c.label), cls: "wrap" },
    { h: "Mag", v: (c) => fmt(c.mag, 2), sort: (c) => c.mag },
    { h: "SNR", v: (c) => fmt(c.snr, 0), sort: (c) => c.snr },
    { h: "FWHM", v: (c) => fmt(c.fwhm_px, 1), sort: (c) => c.fwhm_px },
    { h: "RA Dec", v: sky },
    { h: "Check", v: (c) => `<a href="${aladin(c.ra, c.dec)}" target="_blank" rel="noopener">Aladin</a> · <a href="${tns(c)}" target="_blank" rel="noopener">TNS</a>` },
  ], rows, (c) => focusOn(c), r.transients === false ? "Search disabled." :
    (r.options?.transients === "false" ? "Search disabled." : "No new objects found above the catalogue limit."));
}

function renderVariables(r) {
  const all = (r.variables || []).map((v, i) => ({ ...v, _key: "v" + i, _type: "variables" }));
  const range = (v) => v.max == null ? "–" : v.min_is_amplitude ? `${fmt(v.max, 2)} (ampl. ${fmt(v.min, 2)})` : `${fmt(v.max, 2)}–${fmt(v.min, 2)}`;
  const draw = () => {
    const q = $("#varFilter").value.trim().toLowerCase();
    const rows = q ? all.filter((v) => v.name.toLowerCase().includes(q) || v.type.toLowerCase().includes(q)) : all;
    table($("#variables"), [
      { h: "Star", v: (v) => `<a href="${esc(v.vsx_url)}" target="_blank" rel="noopener">${esc(v.name)}</a>`, sort: (v) => v.name },
      { h: "Type", v: (v) => esc(v.type), sort: (v) => v.type },
      { h: "Mag", v: (v) => (v.upper_limit ? "&lt;" : "") + fmt(v.mag, 3), sort: (v) => v.mag },
      { h: "Err", v: (v) => fmt(v.err, 3), sort: (v) => v.err ?? 9 },
      { h: "VSX range", v: range, sort: (v) => v.max ?? 99 },
      { h: "Period (d)", v: (v) => v.period ? fmt(v.period, v.period < 10 ? 5 : 1) : "–", sort: (v) => v.period ?? 1e9 },
      { h: "Airmass", v: (v) => fmt(v.airmass, 2), sort: (v) => v.airmass ?? 99 },
      { h: "Check star", v: (v) => v.check ? `${fmt(v.check.measured, 2)} <span class="muted small">(cat ${fmt(v.check.catalog, 2)})</span>` : "–" },
      { h: "Flags", v: (v) => v.flags.map((f) => `<span class="badge ${f === "fainter than" ? "b-gray" : "b-yellow"}">${esc(f)}</span>`).join(" ") },
    ], rows, (v) => focusOn(v), "No catalogued variables in the field.");
  };
  $("#varFilter").oninput = draw;
  draw();
}

function renderMinor(r) {
  const rows = (r.minor_bodies || []).map((m, i) => ({ ...m, _key: "m" + i, _type: "minor" }));
  table($("#minor"), [
    { h: "Name", v: (m) => esc(m.name), sort: (m) => m.name },
    { h: "Class", v: (m) => esc(m.class), sort: (m) => m.class },
    { h: "Pred. V", v: (m) => fmt(m.vmag, 1), sort: (m) => m.vmag ?? 99 },
    { h: "Detected", v: (m) => m.detected ? `<span class="badge b-cyan">yes</span>` : `<span class="badge b-gray">no</span>`, sort: (m) => !m.detected },
    { h: "Measured", v: (m) => fmt(m.measured_mag, 2) },
    { h: "Motion (″/h)", v: (m) => fmt(Math.hypot(m.rate_ra_arcsec_h, m.rate_dec_arcsec_h), 1) },
    { h: "RA Dec", v: sky },
  ], rows, (m) => focusOn(m), job?.time ? "No known asteroids or comets brighter than the image limit." : "Needs the observation time.");
}

// ---------------------------------------------------------------- viewer (zoom / pan / overlay)
const viewer = $("#viewer"), wrap = $("#stageWrap");
function apply() { wrap.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`; drawOverlay(); }
function fit() {
  const img = $("#img"); if (!img.naturalWidth) return;
  const z = Math.min(viewer.clientWidth / img.naturalWidth, viewer.clientHeight / img.naturalHeight);
  view = { z, x: (viewer.clientWidth - img.naturalWidth * z) / 2, y: (viewer.clientHeight - img.naturalHeight * z) / 2 };
  apply();
}
$("#zoomFit").onclick = fit;
window.addEventListener("resize", () => job && fit());
function zoomAt(cx, cy, f) {
  const nz = Math.min(Math.max(view.z * f, 0.05), 20);
  view.x = cx - ((cx - view.x) * nz) / view.z; view.y = cy - ((cy - view.y) * nz) / view.z; view.z = nz; apply();
}
viewer.addEventListener("wheel", (e) => { e.preventDefault(); const b = viewer.getBoundingClientRect(); zoomAt(e.clientX - b.left, e.clientY - b.top, Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
const pointers = new Map(); let lastDist = 0, moved = false;
viewer.addEventListener("pointerdown", (e) => { viewer.setPointerCapture(e.pointerId); pointers.set(e.pointerId, e); moved = false; viewer.classList.add("dragging"); });
viewer.addEventListener("pointermove", (e) => {
  if (!pointers.has(e.pointerId)) return;
  const prev = pointers.get(e.pointerId); pointers.set(e.pointerId, e);
  if (pointers.size === 1) {
    const dx = e.clientX - prev.clientX, dy = e.clientY - prev.clientY;
    if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;
    view.x += dx; view.y += dy; apply();
  } else if (pointers.size === 2) {
    const [a, b] = [...pointers.values()]; const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    const r = viewer.getBoundingClientRect();
    if (lastDist) zoomAt((a.clientX + b.clientX) / 2 - r.left, (a.clientY + b.clientY) / 2 - r.top, d / lastDist);
    lastDist = d; moved = true;
  }
});
const up = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) lastDist = 0; if (!pointers.size) viewer.classList.remove("dragging"); };
viewer.addEventListener("pointerup", up); viewer.addEventListener("pointercancel", up);
document.querySelectorAll("[data-layer]").forEach((c) => c.addEventListener("change", drawOverlay));

function markers() {
  if (!job) return [];
  const on = (k) => $(`[data-layer=${k}]`).checked;
  const out = [];
  if (on("variables")) (job.variables || []).forEach((v, i) => out.push({ o: v, key: "v" + i, shape: "circle", col: v.upper_limit ? COLORS.limit : COLORS.variable, label: v.name }));
  if (on("minor")) (job.minor_bodies || []).forEach((m, i) => out.push({ o: m, key: "m" + i, shape: "square", col: m.detected ? COLORS.minor : COLORS.minorMiss, label: m.name }));
  if (on("candidates")) (job.candidates || []).forEach((c, i) => out.push({ o: c, key: "c" + i, shape: "ring", col: c.status === "unidentified" ? COLORS.unidentified : COLORS.known, label: c.status === "unidentified" ? `? ${fmt(c.mag, 1)}` : c.known?.name }));
  return out;
}

function drawOverlay() {
  if (!job?.preview) return;
  const s = job.preview.scale, svg = $("#overlay"), r = 9 / view.z, labels = $("[data-layer=labels]").checked;
  svg.innerHTML = markers().map((m) => {
    const x = m.o.x * s, y = m.o.y * s, sel = selected === m.key ? " sel" : "";
    let shape;
    if (m.shape === "square") shape = `<rect class="m${sel}" data-k="${m.key}" x="${x - r}" y="${y - r}" width="${2 * r}" height="${2 * r}" stroke="${m.col}"/>`;
    else if (m.shape === "ring") shape = `<circle class="m${sel}" data-k="${m.key}" cx="${x}" cy="${y}" r="${1.6 * r}" stroke="${m.col}" stroke-dasharray="${r * 0.8} ${r * 0.4}"/>`;
    else shape = `<circle class="m${sel}" data-k="${m.key}" cx="${x}" cy="${y}" r="${r}" stroke="${m.col}"/>`;
    const lab = (labels || sel) && m.label ? `<text x="${x + 1.8 * r}" y="${y - 1.2 * r}" fill="${m.col}" style="font-size:${12 / view.z}px;stroke-width:${3 / view.z}px">${esc(m.label)}</text>` : "";
    return shape + lab;
  }).join("");
  svg.querySelectorAll(".m").forEach((el) => {
    el.addEventListener("pointerenter", (e) => showTip(el.dataset.k, e));
    el.addEventListener("pointerleave", () => $("#tip").classList.add("hidden"));
    el.addEventListener("click", () => { if (!moved) select(el.dataset.k); });
  });
}

function lookup(key) {
  const i = +key.slice(1);
  return key[0] === "v" ? job.variables[i] : key[0] === "m" ? job.minor_bodies[i] : job.candidates[i];
}
function describe(key) {
  const o = lookup(key);
  if (key[0] === "v") return `<b>${esc(o.name)}</b> <span class="muted">${esc(o.type)}</span><br>${o.upper_limit ? "fainter than " : ""}${fmt(o.mag, 3)}${o.err ? " ± " + fmt(o.err, 3) : ""} ${esc(job.calibration?.band || "")}`;
  if (key[0] === "m") return `<b>${esc(o.name)}</b><br>${esc(o.class)} · pred. V ${fmt(o.vmag, 1)} · ${o.detected ? "detected " + fmt(o.measured_mag, 1) : "not detected"}`;
  return `<b>${esc(o.label)}</b><br>mag ${fmt(o.mag, 2)} · SNR ${fmt(o.snr, 0)} · FWHM ${fmt(o.fwhm_px, 1)} px<br>${sky(o)}`;
}
function showTip(key, e) {
  const tip = $("#tip"), b = viewer.getBoundingClientRect();
  tip.innerHTML = describe(key); tip.classList.remove("hidden");
  tip.style.left = Math.min(e.clientX - b.left + 14, b.width - 290) + "px"; tip.style.top = e.clientY - b.top + 14 + "px";
}
function applyDisplay() {
  $("#img").style.filter = `brightness(${display.brightness}) contrast(${display.contrast})`;
  $("#bright").value = display.brightness;
  $("#contrast").value = display.contrast;
  store.set("sp_bright", String(display.brightness));
  store.set("sp_contrast", String(display.contrast));
  if (selected) showSelected(selected);      // keep the close-up in step with the viewer
}
$("#bright")?.addEventListener("input", (e) => { display.brightness = +e.target.value; applyDisplay(); });
$("#contrast")?.addEventListener("input", (e) => { display.contrast = +e.target.value; applyDisplay(); });
$("#bcReset")?.addEventListener("click", () => { display = { brightness: 1, contrast: 1 }; applyDisplay(); });

// close-up of whatever is selected, cut from the preview on the server
function showSelected(key) {
  const o = lookup(key), card = $("#selectedCard");
  if (!o) { card.classList.add("hidden"); return; }
  const url = `/api/jobs/${job.id}/cutout.jpg?x=${o.x.toFixed(1)}&y=${o.y.toFixed(1)}` +
    `&size=90&zoom=4&brightness=${display.brightness}&contrast=${display.contrast}`;
  const title = key[0] === "v" ? o.name : key[0] === "m" ? o.name : o.label;
  const facts = key[0] === "v"
    ? [["Type", o.type], ["Magnitude", (o.upper_limit ? "fainter than " : "") + fmt(o.mag, 3)],
       ["Error", fmt(o.err, 3)], ["VSX range", o.max == null ? "–" : `${fmt(o.max, 2)}–${fmt(o.min, 2)}`],
       ["Position", sky(o)]]
    : key[0] === "m"
      ? [["Class", o.class], ["Predicted V", fmt(o.vmag, 1)], ["Detected", o.detected ? "yes" : "no"],
         ["Measured", fmt(o.measured_mag, 2)], ["Position", sky(o)]]
      : [["Magnitude", fmt(o.mag, 2)], ["SNR", fmt(o.snr, 0)], ["FWHM", fmt(o.fwhm_px, 1) + " px"],
         ["Identified", o.known ? `${o.known.name} (${o.known.catalog})` : "no"], ["Position", sky(o)]];
  card.classList.remove("hidden");
  card.innerHTML = `<div class="tableHead"><h2>Close-up</h2><span class="grow"></span>
      <button class="ghost small" id="selClose">×</button></div>
    <img src="${url}" alt="close-up of ${esc(title)}">
    <div class="facts"><div><span>Object</span><span>${esc(title)}</span></div>
      ${facts.map(([k, v]) => `<div><span>${esc(k)}</span><span>${esc(v ?? "–")}</span></div>`).join("")}</div>
    <div class="links"><a class="btn small" target="_blank" rel="noopener" href="${aladin(o.ra, o.dec)}">Aladin</a>
      ${key[0] === "v" ? `<a class="btn small" target="_blank" rel="noopener" href="${esc(o.vsx_url)}">VSX</a>` : ""}</div>`;
  $("#selClose").onclick = () => { selected = null; card.classList.add("hidden"); drawOverlay(); };
}

function select(key) {
  selected = key; drawOverlay(); showSelected(key);
  document.querySelectorAll("tbody tr.sel").forEach((tr) => tr.classList.remove("sel"));
  const tr = document.querySelector(`tr[data-k="${key}"]`);
  if (tr) { tr.classList.add("sel"); tr.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}
function focusOn(o) {
  const key = o._key; const s = job.preview.scale;
  view.z = Math.max(view.z, 2.5);
  view.x = viewer.clientWidth / 2 - o.x * s * view.z; view.y = viewer.clientHeight / 2 - o.y * s * view.z;
  selected = key; apply(); select(key);
  viewer.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------------------------------------------------------- history
$("#btnHistory").onclick = async () => {
  const list = await (await fetch("/api/jobs")).json();
  $("#historyCard").classList.remove("hidden");
  $("#history").innerHTML = list.length ? `<div class="tbl"><table><thead><tr><th>File</th><th>Status</th><th>Centre</th><th>Variables</th><th>New?</th><th>Date</th></tr></thead><tbody>${list.map((j) => `
    <tr data-id="${esc(j.id)}"><td>${esc(j.filename)}</td><td>${esc(j.status)}</td><td>${j.ra != null ? fmt(j.ra, 3) + " " + fmt(j.dec, 3) : "–"}</td>
    <td>${j.n_variables}</td><td>${j.n_unidentified ? `<span class="badge b-red">${j.n_unidentified}</span>` : "0"}</td><td>${new Date(j.created * 1000).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<p class="empty">No analyses yet.</p>`;
  $("#history").querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => { $("#historyCard").classList.add("hidden"); location.hash = "job=" + tr.dataset.id; poll(tr.dataset.id); });
  $("#historyCard").scrollIntoView({ behavior: "smooth" });
};
$("#closeHistory").onclick = () => $("#historyCard").classList.add("hidden");

const m = location.hash.match(/job=([a-f0-9]{16})/);
if (m) poll(m[1]);

// ---------------------------------------------------------------- vsnet-obs
async function loadVsnet() {
  const p = new URLSearchParams({
    observer: $("#observer").value || "", site: $("#site").value || "",
    instrument: $("#instrument").value || "", include_limits: $("#vsnetLimits").checked ? "true" : "false",
    force: $("#vsnetForce").checked ? "true" : "false",
  });
  const r = await fetch(`/api/jobs/${job.id}/vsnet?${p}`);
  const v = await r.json();
  if (r.status === 409 && v.detail?.blocked) {       // non-linear image: nothing to send
    const d = v.detail;
    $("#vsnetInfo").textContent = `${d.n_observations} observations would be reported.`;
    $("#vsnetBlocked").classList.remove("hidden");
    $("#vsnetBlocked").textContent = `${d.reason} Tick "force" above to compose it anyway.`;
    $("#vsnetBody").value = d.preview + "\n…";
    $("#vsnetMail").removeAttribute("href");
    $("#vsnetMail").classList.add("disabled");
    return;
  }
  if (!r.ok) { $("#vsnetInfo").textContent = (v.detail && v.detail.reason) || v.detail || "could not compose the report"; return; }
  $("#vsnetMail").classList.remove("disabled");
  const sel = v.selection || {};
  $("#vsnetInfo").textContent = `${v.n_observations} observations of ${sel.total} measured ` +
    `(dropped: ${sel.dropped_survey_id || 0} survey IDs, ${sel.dropped_flagged || 0} flagged, ` +
    `${sel.dropped_error || 0} too noisy, ${sel.dropped_limits || 0} limits` +
    (sel.over_limit ? `, ${sel.over_limit} over the 50-line cap` : "") + ")";
  $("#vsnetBlocked").classList.toggle("hidden", !v.blocked);
  $("#vsnetBlocked").textContent = v.blocked_reason || "";
  $("#vsnetBody").value = v.body;
  $("#vsnetMail").href = `mailto:${v.to}?subject=${encodeURIComponent(v.subject)}&body=${encodeURIComponent(v.body)}`;
  $("#vsnetTxt").onclick = () => {
    const url = URL.createObjectURL(new Blob([v.body], { type: "text/plain" }));
    const a = document.createElement("a"); a.href = url; a.download = `vsnet_${job.id}.txt`; a.click();
    URL.revokeObjectURL(url);
  };
}
$("#btnVsnet")?.addEventListener("click", async () => { $("#vsnetDlg").showModal(); await loadVsnet(); });
$("#vsnetLimits")?.addEventListener("change", loadVsnet);
$("#vsnetForce")?.addEventListener("change", () => { store.set("sp_vsnetforce", $("#vsnetForce").checked ? "1" : ""); loadVsnet(); });
if (store.get("sp_vsnetforce")) { const f = $("#vsnetForce"); if (f) f.checked = true; }
$("#vsnetCopy")?.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#vsnetBody").value); $("#vsnetCopy").textContent = "Copied"; }
  catch { $("#vsnetBody").select(); }
  setTimeout(() => ($("#vsnetCopy").textContent = "Copy"), 1500);
});

// ---------------------------------------------------------------- measurement archive
const jdToDate = (jd) => new Date((jd - 2440587.5) * 86400000).toISOString().slice(0, 16).replace("T", " ");

async function openDb() {
  $("#dbCard").classList.remove("hidden");
  $("#dbCurve").classList.add("hidden");
  const s = await (await fetch("/api/db/stats")).json();
  $("#dbStats").innerHTML = [
    [s.images, "images"], [s.measurements, "measurements"], [s.stars, "stars"],
    [s.nights, "nights"], [s.unidentified_candidates, "unidentified"],
    [(s.db_bytes / 1048576).toFixed(1) + " MB", "database"],
  ].map(([v, k]) => `<div><b>${esc(v)}</b><span class="muted small">${k}</span></div>`).join("");
  await listStars();
  $("#dbCard").scrollIntoView({ behavior: "smooth" });
}

async function listStars() {
  const q = encodeURIComponent($("#dbSearch").value.trim());
  const min = $("#dbRepeat").checked ? 2 : 1;
  const rows = await (await fetch(`/api/db/stars?q=${q}&min_points=${min}&limit=200`)).json();
  const el = $("#dbStars");
  if (!rows.length) { el.innerHTML = `<p class="empty">Nothing in the archive matches.</p>`; return; }
  el.innerHTML = `<div class="tbl"><table><thead><tr><th>Star</th><th>Type</th><th>Points</th><th>Limits</th>
    <th>Brightest</th><th>Faintest</th><th>Range</th><th>Last seen</th></tr></thead><tbody>${rows.map((r) => `
    <tr data-name="${esc(r.name)}"><td>${esc(r.name)}</td><td>${esc(r.type || "")}</td><td>${r.points}</td>
    <td class="muted">${r.limits || 0}</td>
    <td>${fmt(r.brightest, 2)}</td><td>${fmt(r.faintest, 2)}</td>
    <td>${r.amplitude > 0.05 ? fmt(r.amplitude, 2) + " mag" : "–"}</td>
    <td>${r.last_jd ? jdToDate(r.last_jd) : "–"}</td></tr>`).join("")}</tbody></table></div>`;
  el.querySelectorAll("tr[data-name]").forEach((tr) => tr.onclick = () => showCurve(tr.dataset.name));
}

async function showCurve(name) {
  const r = await (await fetch(`/api/db/star/${encodeURIComponent(name)}`)).json();
  if (r.detail) return;
  $("#dbCurve").classList.remove("hidden");
  $("#dbStars").classList.add("hidden");
  $("#dbCurveName").textContent = name;
  $("#dbCurveCsv").href = `/api/db/star/${encodeURIComponent(name)}?csv_format=true`;
  plotCurve(r.points);
  $("#dbPoints").innerHTML = `<div class="tbl"><table><thead><tr><th>Date (UT)</th><th>JD</th><th>Mag</th>
    <th>Err</th><th>Band</th><th>Camera</th><th>Flags</th><th>Image</th></tr></thead><tbody>${r.points.map((p) => `
    <tr><td>${p.jd ? jdToDate(p.jd) : "–"}</td><td>${fmt(p.jd, 4)}</td>
    <td>${p.upper_limit ? "&lt;" : ""}${fmt(p.mag, 3)}</td><td>${fmt(p.err, 3)}</td><td>${esc(p.band || "")}</td>
    <td>${esc(p.camera || "")}</td>
    <td>${p.nonlinear ? '<span class="badge b-yellow">non-linear</span> ' : ""}${esc(p.flags || "")}</td>
    <td><a href="#job=${esc(p.job_id)}">open</a></td></tr>`).join("")}</tbody></table></div>`;
}

function plotCurve(points) {
  const svg = $("#dbPlot"), W = 720, H = 260, pad = { l: 46, r: 12, t: 12, b: 28 };
  const good = points.filter((p) => p.jd != null && p.mag != null);
  if (good.length < 1) { svg.innerHTML = `<text x="20" y="30" class="label">no dated measurements</text>`; return; }
  const jds = good.map((p) => p.jd), mags = good.map((p) => p.mag);
  const jd0 = Math.min(...jds), jd1 = Math.max(...jds);
  const spanX = (jd1 - jd0) || 1;
  const m0 = Math.min(...mags), m1 = Math.max(...mags);
  const padY = Math.max((m1 - m0) * 0.15, 0.05);
  const lo = m0 - padY, hi = m1 + padY;                    // magnitudes: brighter is up
  const x = (jd) => pad.l + ((jd - jd0) / spanX) * (W - pad.l - pad.r);
  const y = (m) => pad.t + ((m - lo) / (hi - lo)) * (H - pad.t - pad.b);
  const ticks = [lo, (lo + hi) / 2, hi].map((m) =>
    `<text class="tick" x="6" y="${y(m) + 3}">${m.toFixed(2)}</text>
     <line class="axis" x1="${pad.l}" y1="${y(m)}" x2="${W - pad.r}" y2="${y(m)}" opacity=".35"/>`).join("");
  const dates = [jd0, jd1].map((jd, i) =>
    `<text class="tick" x="${x(jd)}" y="${H - 8}" text-anchor="${i ? "end" : "start"}">${jdToDate(jd).slice(0, 10)}</text>`).join("");
  const marks = good.map((p) => {
    const cx = x(p.jd), cy = y(p.mag);
    const bar = p.err ? `<line class="bar" x1="${cx}" y1="${y(p.mag - p.err)}" x2="${cx}" y2="${y(p.mag + p.err)}"/>` : "";
    return bar + (p.upper_limit
      ? `<path class="pt limit" d="M${cx - 4},${cy} l8,0 M${cx},${cy} l0,8 M${cx - 3},${cy + 5} l3,3 l3,-3"><title>${fmt(p.mag, 2)} limit</title></path>`
      : `<circle class="pt" cx="${cx}" cy="${cy}" r="3.5"><title>${jdToDate(p.jd)} — ${fmt(p.mag, 3)} ${esc(p.band || "")}</title></circle>`);
  }).join("");
  svg.innerHTML = `${ticks}${dates}
    <line class="axis" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${H - pad.b}"/>
    <line class="axis" x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${H - pad.b}"/>${marks}`;
}

$("#btnDb")?.addEventListener("click", openDb);
$("#closeDb")?.addEventListener("click", () => $("#dbCard").classList.add("hidden"));
$("#dbSearch")?.addEventListener("input", () => { $("#dbStars").classList.remove("hidden"); $("#dbCurve").classList.add("hidden"); listStars(); });
$("#dbRepeat")?.addEventListener("change", listStars);
$("#dbCurveClose")?.addEventListener("click", () => { $("#dbCurve").classList.add("hidden"); $("#dbStars").classList.remove("hidden"); });

// ---------------------------------------------------------------- backup / restore
$("#dbRestoreBtn")?.addEventListener("click", () => $("#dbRestoreFile").click());
$("#dbRestoreFile")?.addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const merge = confirm(`Restore from ${f.name}?\n\nOK = merge into the current archive (keeps both).\n` +
                        `Cancel = replace it (a safety copy is written first).`);
  if (!merge && !confirm("Replace the whole archive? The current one is copied to the backups folder first.")) {
    e.target.value = ""; return;
  }
  const fd = new FormData(); fd.append("file", f);
  $("#dbRestoreInfo").textContent = "restoring…";
  const r = await fetch(`/api/db/restore?merge=${merge}&confirm=true`, { method: "POST", body: fd });
  const v = await r.json();
  e.target.value = "";
  if (!r.ok) { $("#dbRestoreInfo").textContent = v.detail || "restore failed"; return; }
  $("#dbRestoreInfo").textContent =
    `${v.mode}: ${v.before.images} → ${v.after.images} images, safety copy ${v.safety_copy.split("/").pop()}`;
  openDb();
});
