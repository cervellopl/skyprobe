# SkyProbe

Web app + REST API + Android client that take a single sky image from a **phone** or a
**ZWO Seestar** (JPEG, HEIC, camera RAW or FITS) and:

1. **solve the image parameters and astrometry** with astrometry.net — WCS, field centre,
   pixel scale, field of view, rotation, parity (locally with `solve-field`, or through
   nova.astrometry.net);
2. **search the field for objects that are not in the catalogues** — candidate novae,
   supernovae, comets and uncatalogued moving objects, with known asteroids/comets
   (IMCCE SkyBoT), known variables (AAVSO VSX) and galaxies (HyperLEDA) identified and
   labelled instead of reported as new;
3. **do photometry of every catalogued variable star in the field** — aperture photometry
   calibrated against a Gaia DR3 / Tycho-2 ensemble, exportable as an
   **AAVSO Extended Format** report;
4. all of it from an **Android app** (or the browser UI, or `curl`).

```
mc3j/
├── backend/          FastAPI service + web UI  (Python)
│   ├── app/          imageio, astrometry, detect, catalogs, photometry, transients, pipeline, main
│   ├── static/       single-page web UI (no build step)
│   ├── tests/        offline unit tests + synthetic-image generators + CLI runner
│   └── scripts/      astrometry.net index-file installer
├── android/          Jetpack Compose client (Kotlin)
└── docker-compose.yml
```

---

## Quick start

### Server

```bash
sudo apt install astrometry.net astrometry-data-tycho2-{07,08,09,10-19}-littleendian
cd backend && ./run.sh 5678       # http://localhost:5678
```

or with Docker:

```bash
docker compose up --build         # http://localhost:8765
```

Index files decide which field sizes can be solved locally. `astrometry-data-tycho2-*`
(or `backend/scripts/install_indexes.sh phone|telephoto|seestar|deep`) covers roughly
20′ to 30°. If you would rather not host index files at all, set
`ASTROMETRY_API_KEY` (from your nova.astrometry.net profile) and everything is solved
remotely.

### Analysing an image

* Browser: open the server, drop a file, press **Analyse**.
* CLI: `curl -F file=@image.fits -F obscode=ABC http://localhost:8765/api/jobs`
* Without a server: `backend/tests/run_local.py IMAGE [key=value ...]` runs the whole
  pipeline in-process and prints the result.

### Android app

`android/` is a complete Gradle project (Compose, OkHttp, Coil):

```bash
cd android && gradle wrapper --gradle-version 8.11.1 && ./gradlew assembleDebug
```

The included GitHub Action (`.github/workflows/android.yml` → `skyprobe-debug-apk`) builds
it on x86-64 runners. It also builds **on arm64** (Raspberry Pi) — the SDK's Java tools are
architecture-independent, and the one x86-64 binary, `aapt2`, runs under qemu:

```bash
sudo apt install qemu-user-static binfmt-support
# minimal amd64 sysroot for aapt2 (libc6, libstdc++6, zlib1g, libgcc-s1 .debs, extracted)
export QEMU_LD_PREFIX=/path/to/x86root
./gradlew assembleDebug          # ~33 min for a cold first build on a Pi 5
```

Then set the server URL in the app's settings (⚙). The app also registers as a share
target, so you can send a photo to SkyProbe straight from the gallery.

---

## What the pipeline does

| Stage | Detail |
|---|---|
| **Load** | JPEG/PNG/HEIC via Pillow (+pillow-heif), camera RAW via rawpy/LibRaw (**DNG** — both the ordinary Bayer kind and the already-demosaiced "linear DNG" that Apple ProRAW and some Android computational-RAW pipelines write — plus CR2/CR3, NEF, ARW, ORF, RW2, RAF…), FITS via astropy. Colour data is reduced to the **green channel at full resolution** (AAVSO *TG* band): Bayer frames are interpolated, not binned, so pixel geometry — and therefore the WCS — stays valid. JPEG/HEIC are linearised with the inverse sRGB curve and flagged as approximate. EXIF/FITS give exposure, time, focal length, pixel size and GPS. |
| **Detect** | SEP (SExtractor) background mesh + extraction, half-light radii → FWHM, saturation and edge flags. Lit **foreground** (buildings, trees, the ground in a landscape astrophoto) is found from block texture plus connected-component analysis and excluded from solving, photometry and the transient search. |
| **Solve** | Fields wider than ~30° are solved on the **central part of the frame** first, because a phone lens is nowhere near the gnomonic projection a solver assumes; the solution is then extended to the whole frame by matching catalogue stars and fitting a sigma-clipped **SIP distortion polynomial** (on a real 86°×65° Galaxy S25 frame this brings the residual from ~5 px down to ~1 px). The *source list* (not the image) is sent to `solve-field` / nova, so a 50 MB RAW solves as fast as a JPEG and the WCS applies to full-resolution pixels. Hints: pixel scale from EXIF 35 mm-equivalent focal length, FITS `FOCALLEN`/`XPIXSZ`, or a device preset (Seestar S50 2.39″/px, S30 3.99″/px); RA/Dec from the FITS header. Each attempt falls back to a blinder one. A WCS already present in a FITS header is verified against Gaia before it is trusted. |
| **Catalogues** | Gaia DR3 for fields ≲2.5° radius, Tycho-2 for wide phone fields (VizieR truncates huge Gaia cones), AAVSO VSX for variables, IMCCE SkyBoT for minor bodies at the exposure time (tiled in parallel for wide fields), HyperLEDA for galaxies. The magnitude limit is chosen from the pixel scale, field area and galactic latitude, and everything is cached on disk. |
| **Photometry** | Aperture photometry (r ≈ 1.4 FWHM, local sky annulus). Comparison stars: isolated, unsaturated, non-variable catalogue stars transformed to Johnson *V* with the published Gaia DR3 polynomials. A sigma-clipped **zero point + colour term** is fitted globally; a **local correction** from the ~25 nearest comparison stars absorbs vignetting and differential extinction across wide fields. Each variable is measured by forced photometry at its VSX position (upper limit if SNR < 5), with a check star and airmass, and exported in AAVSO Extended Format. |
| **New objects** | Every detection is matched against the reference catalogue. Leftovers are screened for hot pixels, cosmic rays, satellite trails, edges, saturation halos and blends, and must be brighter than the catalogue depth (otherwise "not in the catalogue" is meaningless). Survivors are classified: *known minor planet/comet* (SkyBoT), *known variable* (VSX), *galaxy* (HyperLEDA), *unidentified diffuse* (possible comet, re-measured with a coma-sized aperture), *unidentified star-like* (possible nova/supernova), or *brightening* — a catalogued star ≥1.5 mag brighter than predicted, i.e. a possible outburst. |

### Accuracy, and what this cannot do

* On a synthetic Seestar-like frame built from real Gaia stars, the pipeline solves in
  ~1.5 s, calibrates to **σ ≈ 0.006 mag** (FITS) / **0.027 mag** (the same frame as an
  8-bit JPEG), and recovers an injected nova (12.00 → 11.96), an outburst (12.50 → 12.46)
  and a diffuse comet (11.00 → 11.04) with no false positives. On a synthetic 85°×64°
  phone frame the scatter is ≈0.18 mag — realistic for tone-mapped 8-bit phone images.
* Phone JPEG/HEIC photometry is **approximate**: tone mapping, noise reduction, lens
  vignetting and 8-bit quantisation all bite. RAW or FITS is much better; shoot RAW when
  the magnitudes matter.
* A single image cannot prove a transient. Everything in the "new objects" list is a
  *candidate*: check it against the linked Aladin/TNS pages and a second exposure before
  reporting anything. Unknown fast movers, internal reflections, dust spots and processing
  artefacts all look like new stars.
* Minor-body identification needs the exposure time. Phone EXIF without a timezone offset
  is assumed to be UTC — pass `obs_time` or `utc_offset` if that is wrong.
* Airmass (and thus a complete AAVSO record) needs `lat`/`lon`, taken from EXIF GPS when
  present.
* AAVSO reports use a Gaia/Tycho-transformed **ensemble**, not AAVSO chart comparison
  stars — good for a quick look, but check the sequence before submitting to the AID.
* **Computational phone RAW is not necessarily linear.** A Galaxy S25 Ultra night-mode DNG
  measured here shows a compressed response: bright stars read more than a magnitude too
  faint, regardless of aperture. The calibration fits that response empirically (a slope
  plus a binned response curve, never extrapolated past the comparison stars), flags
  measurements outside the calibrated range, and **disables the AAVSO export** for such
  files. Treat those magnitudes as indicative only.
* The new-object search is **skipped** when the reference catalogue is shallower than the
  image — on a wide phone frame the image can reach mag 11 while Tycho-2 stops near 8, and
  at 80″/px most "sources" are unresolved blends of faint stars, so everything would look
  like a discovery. Known asteroids and comets are still listed.

---

## API

Interactive docs (OpenAPI) at `/docs`. Set `API_TOKEN` to require `X-API-Key` on every call.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | version, available solvers, accepted formats, device presets |
| `POST` | `/api/jobs` | multipart upload, returns `{"id": …}` (202) |
| `GET` | `/api/jobs` | recent jobs |
| `GET` | `/api/jobs/{id}` | status + full result (poll until `status` is `done`/`failed`) |
| `DELETE` | `/api/jobs/{id}` | delete a finished job |
| `GET` | `/api/jobs/{id}/preview.jpg` | stretched preview |
| `GET` | `/api/jobs/{id}/annotated.jpg` | preview with markers drawn |
| `GET` | `/api/jobs/{id}/wcs.fits`, `/solution.wcs` | the astrometric solution |
| `GET` | `/api/jobs/{id}/photometry.csv`, `/candidates.csv` | tables |
| `GET` | `/api/jobs/{id}/aavso.txt` | AAVSO Extended Format report |
| `GET` | `/api/jobs/{id}/report.pdf` | printable PDF report (image, astrometry, calibration, tables) |
| `GET` | `/api/jobs/{id}/vsnet` | composed vsnet-obs posting (`?plain=true` for text) |

### `POST /api/jobs` form fields

`file` (required) · `device` (`auto`, `phone`, `seestar_s50`, `seestar_s30`) · `scale`,
`scale_low`, `scale_high` (″/px) · `ra`, `dec`, `radius` (deg) · `solver`
(`auto|local|remote`) · `api_key` (nova key) · `obs_time` (ISO UTC) · `utc_offset` (h) ·
`lat`, `lon` · `band` (`TG`, `CV`, `V`, …) · `mag_limit` · `obscode` ·
`photometry`, `transients` (`true|false`) · `use_header_wcs` · `detect_sigma`.

### Result shape

```jsonc
{
  "id": "…", "status": "done", "progress": 100, "log": ["…"], "warnings": ["…"],
  "file": {"format": "fits", "width": 1080, "height": 1920, "band": "CV", "linear": true},
  "time": {"utc_mid": "2025-09-15T21:35:00", "jd_mid": 2460934.399, "source": "fits"},
  "solution": {"ra": 29.818, "dec": 8.087, "ra_hms": "01:59:16.4", "pixel_scale": 2.390,
               "fov_w_deg": 0.717, "rotation_deg": 197.0, "parity": "normal", "solver": "…"},
  "calibration": {"band": "CV", "n_comps": 177, "zero_point": 24.999, "color_term": -0.005,
                  "rms": 0.017, "limit_mag_5sigma": 17.4, "catalog": "Gaia DR3 -> Johnson V"},
  "variables":  [{"name": "SS Cyg", "type": "UGSS", "mag": 11.969, "err": 0.003,
                  "upper_limit": false, "airmass": null, "check": {…}, "flags": []}],
  "candidates": [{"status": "unidentified", "kind": "new_star", "label": "…",
                  "ra": 30.058, "dec": 8.321, "mag": 12.0, "snr": 344, "fwhm_px": 3.1}],
  "minor_bodies": [{"name": "(151) Abundantia", "vmag": 13.6, "detected": true,
                    "measured_mag": 13.0, "rate_ra_arcsec_h": -17.3}]
}
```

### Reporting to VSNET

`/api/jobs/{id}/vsnet` composes a posting for the
[vsnet-obs](http://www.kusastro.kyoto-u.ac.jp/vsnet/) mailing list in the
[documented format](http://www.kusastro.kyoto-u.ac.jp/vsnet/etc/format.html)
(`CYGSS 20000101.345 11.83V Xyz` — constellation-first name, UT date, magnitude with a
filter letter, `>` for a limit, a trailing colon for an uncertain value), wrapped in a
configurable intro and footer (`VSNET_INTRO`, `VSNET_FOOTER`, `VSNET_ADDRESS`).

It is deliberately **composed, not sent**: the list expects the message to come from the
address the observer subscribed with, so the web UI and the app hand it to the mail client.
Two guards keep the list usable: only named variables with small errors are included
(auto-generated survey identifiers, flagged and too-noisy measurements are dropped, and the
line count is capped — a wide phone frame yields ~2000 measurements), and a posting from an
image whose camera response was found to be non-linear is **refused** (HTTP 409, with the
reason and a short preview but no ready-to-send text) unless `force=true` is passed - a
setting in the app and a checkbox in the web UI.

## Configuration (environment)

`DATA_DIR` · `WORKERS` (parallel jobs) · `MAX_UPLOAD_MB` · `KEEP_JOBS` · `API_TOKEN` ·
`ASTROMETRY_API_KEY` · `ASTROMETRY_URL` · `SOLVER` · `SOLVE_FIELD` (binary path) ·
`VSNET_ADDRESS` · `VSNET_INTRO` · `VSNET_FOOTER` ·
`SOLVE_TIMEOUT` · `CATALOG_CACHE` · `VIZIER_SERVER` · `GAIA_MAX_RADIUS`.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
../.venv/bin/python -m pytest tests/test_units.py -q      # offline
python tests/make_synthetic.py /tmp/syn [asteroid]        # Gaia-based synthetic Seestar frame
python tests/make_phone.py /tmp/syn 26                    # synthetic wide-field phone JPEG
python tests/make_dng.py /tmp/syn [linear]                # synthetic phone DNG (Bayer or linear)
python tests/run_local.py /tmp/syn/seestar_sscyg.fits     # full pipeline, no server
```

## Credits

astrometry.net (Lang, Hogg, Mierle, Blanton & Roweis) · Gaia DR3 and Tycho-2, AAVSO VSX
and HyperLEDA via VizieR (CDS, Strasbourg) · IMCCE SkyBoT · SEP/SExtractor · astropy,
photutils, rawpy, Pillow.
