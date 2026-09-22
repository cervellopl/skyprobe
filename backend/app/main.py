"""SkyProbe - REST API + web UI.

POST /api/jobs            upload an image (multipart), returns job id
GET  /api/jobs            recent jobs
GET  /api/jobs/{id}       status + full results (poll until status is done/failed)
GET  /api/jobs/{id}/preview.jpg | annotated.jpg | wcs.fits | solution.wcs | aavso.txt | photometry.csv | candidates.csv
GET  /api/jobs/{id}/cutout.jpg?x=&y=  close-up of one object
GET  /api/jobs/{id}/report.pdf    printable PDF report
GET  /api/jobs/{id}/vsnet         composed vsnet-obs posting (JSON, or text with ?plain=true)
DELETE /api/jobs/{id}
GET  /api/health          capabilities of this server
GET  /api/db/stats | /api/db/stars | /api/db/star/{name} | /api/db/near | /api/db/images
GET  /api/db/backup       download a consistent copy;  POST /api/db/restore  puts one back
POST /api/db/backfill     fold the jobs on disk into the database
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import astrometry, db, pipeline, report as report_mod, vsnet
from .imageio import DEVICE_PRESETS, FITS_EXT, JPEG_EXT, RAW_EXT

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
JOBS_DIR = DATA / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 300))
API_TOKEN = os.environ.get("API_TOKEN", "")  # optional shared secret for public deployments
KEEP_JOBS = int(os.environ.get("KEEP_JOBS", 200))
ALLOWED_EXT = RAW_EXT | FITS_EXT | JPEG_EXT

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", 1)))


class Job:
    lock = threading.Lock()

    def __init__(self, job_id: str, filename: str = "", input_path: str = "", opts: dict | None = None):
        self.id = job_id
        self.dir = JOBS_DIR / job_id
        self.filename = filename
        self.input_path = input_path
        self.result = {"id": job_id, "status": "queued", "stage": "queued", "progress": 0, "log": [],
                       "created": time.time(), "filename": filename,
                       "options": {k: v for k, v in (opts or {}).items() if k != "api_key"}}

    def set_stage(self, stage, progress):
        self.result.update({"stage": stage, "progress": progress, "status": "running"})
        self.save()

    def log(self, msg):
        self.result["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.save()

    def save(self):
        with self.lock:
            tmp = self.dir / "result.json.tmp"
            tmp.write_text(json.dumps(self.result, default=_json_default))
            tmp.replace(self.dir / "result.json")


def _json_default(o):
    import numpy as np

    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _run_job(job: Job, opts: dict):
    try:
        pipeline.run(job, opts, job.dir, job.log)
        job.result.update({"status": "done", "stage": "done", "progress": 100})
        try:
            stored = db.ingest(job.result, job.input_path)
            job.log(f"stored in the database: {stored.get('measurements', 0)} measurements")
        except Exception as e:      # a database problem must not lose the analysis
            job.log(f"WARNING could not store in the database: {e}")
    except Exception as e:
        job.result.update({"status": "failed", "stage": "failed", "error": str(e)})
        job.log("ERROR " + "".join(traceback.format_exception_only(type(e), e)).strip())
        traceback.print_exc()
    finally:
        job.result["finished"] = time.time()
        job.save()
        _prune()


def _prune():
    dirs = sorted((d for d in JOBS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_JOBS:]:
        shutil.rmtree(d, ignore_errors=True)


def _load(job_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise HTTPException(404, "unknown job")
    p = JOBS_DIR / job_id / "result.json"
    if not p.exists():
        raise HTTPException(404, "unknown job")
    for _ in range(3):
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            time.sleep(0.05)
    raise HTTPException(503, "busy")


def auth(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    if not API_TOKEN:
        return
    token = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token or "", API_TOKEN):
        raise HTTPException(401, "invalid or missing API token")


app = FastAPI(title="SkyProbe", version=VERSION,
              description="Plate solving (astrometry.net), transient search and variable-star photometry "
                          "for phone and Seestar images.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION, "local_solver": astrometry.local_available(),
            "remote_solver_key_configured": bool(os.environ.get("ASTROMETRY_API_KEY")),
            "auth_required": bool(API_TOKEN), "max_upload_mb": MAX_UPLOAD_MB,
            "formats": sorted(e.lstrip(".") for e in ALLOWED_EXT),
            "devices": {"auto": "Auto-detect", "phone": "Phone camera",
                        **{k: v["label"] for k, v in DEVICE_PRESETS.items()}}}


@app.post("/api/jobs", status_code=202, dependencies=[Depends(auth)])
async def create_job(
    file: UploadFile = File(...),
    device: str = Form("auto"),
    scale: str = Form(""), scale_low: str = Form(""), scale_high: str = Form(""),
    ra: str = Form(""), dec: str = Form(""), radius: str = Form(""),
    solver: str = Form("auto"), api_key: str = Form(""),
    obs_time: str = Form(""), utc_offset: str = Form(""),
    lat: str = Form(""), lon: str = Form(""),
    photometry: str = Form("true"), transients: str = Form("true"),
    band: str = Form(""), mag_limit: str = Form(""), obscode: str = Form(""),
    use_header_wcs: str = Form("true"), detect_sigma: str = Form(""),
):
    opts = {k: v for k, v in dict(device=device, scale=scale, scale_low=scale_low, scale_high=scale_high, ra=ra,
                                  dec=dec, radius=radius, solver=solver, api_key=api_key, obs_time=obs_time,
                                  utc_offset=utc_offset, lat=lat, lon=lon, photometry=photometry,
                                  transients=transients, band=band, mag_limit=mag_limit, obscode=obscode,
                                  use_header_wcs=use_header_wcs, detect_sigma=detect_sigma).items()
            if v not in ("", None)}
    return await _submit(file, opts)


async def _submit(file: UploadFile, opts: dict) -> dict:
    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        # some share sheets hand over a file without a usable name
        guess = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/heif": ".heic",
                 "image/tiff": ".tif", "image/webp": ".webp", "image/x-adobe-dng": ".dng",
                 "application/fits": ".fits", "image/fits": ".fits"}.get((file.content_type or "").lower())
        if guess:
            ext = guess
            name = name + guess if "." not in name else Path(name).stem + guess
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(ALLOWED_EXT))}")
    job_id = secrets.token_hex(8)
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True)
    dest = jdir / ("input" + ext)
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                shutil.rmtree(jdir, ignore_errors=True)
                raise HTTPException(413, f"File larger than {MAX_UPLOAD_MB} MB")
            f.write(chunk)
    job = Job(job_id, name, str(dest), opts)
    job.result["size_bytes"] = size
    job.save()
    executor.submit(_run_job, job, opts)
    return {"id": job_id, "status": "queued", "url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs", dependencies=[Depends(auth)])
def list_jobs(limit: int = 30):
    out = []
    dirs = sorted((d for d in JOBS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[:limit]:
        try:
            r = json.loads((d / "result.json").read_text())
        except Exception:
            continue
        s = r.get("solution") or {}
        out.append({"id": r["id"], "filename": r.get("filename"), "status": r.get("status"),
                    "stage": r.get("stage"), "created": r.get("created"), "ra": s.get("ra"), "dec": s.get("dec"),
                    "object": (r.get("meta") or {}).get("object"),
                    "n_variables": len(r.get("variables", [])),
                    "n_unidentified": sum(1 for c in r.get("candidates", []) if c.get("status") == "unidentified")})
    return out


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str):
    return JSONResponse(_load(job_id))


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(auth)])
def delete_job(job_id: str):
    r = _load(job_id)
    if r.get("status") in ("queued", "running"):
        raise HTTPException(409, "job still running")
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    db.forget(job_id)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/report.pdf", dependencies=[Depends(auth)])
def job_report(job_id: str, refresh: bool = False):
    """A printable summary of the analysis; built on first request and then cached."""
    r = _load(job_id)
    if r.get("status") != "done":
        raise HTTPException(409, "the analysis is not finished")
    path = JOBS_DIR / job_id / "report.pdf"
    if refresh or not path.exists():
        report_mod.build_pdf(r, JOBS_DIR / job_id, path)
    return FileResponse(path, media_type="application/pdf", filename=f"skyprobe_{job_id}.pdf")


@app.get("/api/jobs/{job_id}/vsnet", dependencies=[Depends(auth)])
def job_vsnet(job_id: str, observer: str = "", site: str = "", instrument: str = "",
              limit: int = 50, named_only: bool = True, include_limits: bool = False,
              max_error: float = 0.2, intro: str = "", footer: str = "", subject: str = "",
              plain: bool = False, force: bool = False):
    """Composes a vsnet-obs posting. It is never sent from here: the list expects the
    message to come from the observer's own (subscribed) address.

    A posting built from an image with a non-linear camera response is refused with 409
    unless force=true: those magnitudes are not good enough for a public list."""
    r = _load(job_id)
    if r.get("status") != "done":
        raise HTTPException(409, "the analysis is not finished")
    try:
        rep = vsnet.build_report(r, observer=observer, site=site, instrument=instrument,
                                 include_limits=include_limits, named_only=named_only,
                                 max_error=max_error, limit=limit, intro=intro or None,
                                 footer=footer or None, subject=subject or None, version=VERSION)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if rep["blocked"] and not force:
        # withhold the ready-to-send text, but show enough to explain the refusal
        preview = "\n".join(rep["body"].splitlines()[:12])
        raise HTTPException(409, {"blocked": True, "reason": rep["blocked_reason"],
                                  "n_observations": rep["n_observations"],
                                  "selection": rep["selection"], "preview": preview,
                                  "hint": "repeat the request with force=true to compose it anyway"})
    if plain:
        return PlainTextResponse(rep["body"])
    return rep


@app.get("/api/jobs/{job_id}/cutout.jpg", dependencies=[Depends(auth)])
def job_cutout(job_id: str, x: float, y: float, size: int = 80, zoom: int = 4, mark: bool = True,
               brightness: float = 1.0, contrast: float = 1.0):
    """A close-up of one object, cut from the preview around a full-resolution pixel position.

    Cut from the preview rather than the original: the original may be a 50 MB raw file, and
    for judging whether something looks like a star the stretched preview is what you want.
    """
    from PIL import Image, ImageDraw, ImageEnhance

    r = _load(job_id)
    path = JOBS_DIR / job_id / "preview.jpg"
    if not path.exists():
        raise HTTPException(404, "no preview for this job")
    scale = float((r.get("preview") or {}).get("scale") or 1.0)
    size = max(16, min(size, 400))
    zoom = max(1, min(zoom, 12))

    with Image.open(path) as im:
        im = im.convert("RGB")
        cx, cy = x * scale, y * scale
        half = size / 2
        box = (int(round(cx - half)), int(round(cy - half)), int(round(cx + half)), int(round(cy + half)))
        crop = im.crop(box)          # PIL pads out-of-bounds areas with black
    if abs(brightness - 1.0) > 0.01:
        crop = ImageEnhance.Brightness(crop).enhance(brightness)
    if abs(contrast - 1.0) > 0.01:
        crop = ImageEnhance.Contrast(crop).enhance(contrast)
    crop = crop.resize((crop.width * zoom, crop.height * zoom), Image.LANCZOS)
    if mark:
        d = ImageDraw.Draw(crop)
        c, radius, gap = crop.width / 2, crop.width / 6, crop.width / 14
        d.ellipse([c - radius, c - radius, c + radius, c + radius], outline=(255, 90, 90), width=2)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):      # crosshair ticks, centre left clear
            d.line([c + dx * (radius + gap), c + dy * (radius + gap),
                    c + dx * (radius + gap * 2.4), c + dy * (radius + gap * 2.4)],
                   fill=(255, 90, 90), width=2)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/db/stats", dependencies=[Depends(auth)])
def db_stats():
    """Size and span of the measurement archive."""
    return db.stats()


@app.get("/api/db/stars", dependencies=[Depends(auth)])
def db_stars(q: str = "", limit: int = 100, offset: int = 0, min_points: int = 0):
    """Stars in the archive, with the number of nights and the observed range."""
    return db.stars(q=q, limit=min(limit, 500), offset=offset, min_points=min_points)


@app.get("/api/db/star/{name}", dependencies=[Depends(auth)])
def db_star(name: str, csv_format: bool = False):
    """Every measurement of one star, oldest first - its light curve."""
    points = db.light_curve(name)
    if not points:
        raise HTTPException(404, "no measurements of that star")
    if csv_format:
        return _csv(points, ["jd", "mag", "err", "upper_limit", "band", "airmass", "flags",
                             "job_id", "filename"], f"{name.replace(' ', '_')}.csv")
    return {"name": name, "points": points}


@app.get("/api/db/near", dependencies=[Depends(auth)])
def db_near(ra: float, dec: float, radius_arcmin: float = 5.0, limit: int = 200):
    """Everything measured near a position - useful to follow up a candidate."""
    return db.cone(ra, dec, radius_arcmin, limit)


@app.get("/api/db/images", dependencies=[Depends(auth)])
def db_images(limit: int = 50, offset: int = 0):
    return db.images(limit=min(limit, 500), offset=offset)


@app.get("/api/db/backup", dependencies=[Depends(auth)])
def db_backup(download: bool = True):
    """A consistent copy of the archive, taken while the server keeps running."""
    path = db.backup()
    if not download:
        return {"backup": path.name, "bytes": path.stat().st_size, "backups": db.list_backups()}
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)


@app.get("/api/db/backups", dependencies=[Depends(auth)])
def db_backups():
    return db.list_backups()


@app.post("/api/db/restore", dependencies=[Depends(auth)])
async def db_restore(file: UploadFile = File(...), merge: bool = False, confirm: bool = False):
    """Restore the archive from a backup, or merge one into it.

    `confirm=true` is required for a replace, because it discards what is there now - a
    safety copy of the current archive is always written first and named in the reply.
    """
    if not merge and not confirm:
        raise HTTPException(400, "a replace discards the current archive: repeat with confirm=true "
                                 "(or use merge=true to keep both sides)")
    tmp = JOBS_DIR.parent / f"restore-{secrets.token_hex(4)}.sqlite"
    try:
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
        try:
            return db.restore(tmp, merge=merge)
        except ValueError as e:      # not a SkyProbe archive
            raise HTTPException(415, str(e))
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/db/backfill", dependencies=[Depends(auth)])
def db_backfill():
    """Fold every job still on disk into the database (idempotent)."""
    return db.backfill(JOBS_DIR)


FILES = {"preview.jpg": "image/jpeg", "annotated.jpg": "image/jpeg", "wcs.fits": "application/fits",
         "solution.wcs": "text/plain", "aavso.txt": "text/plain", "report.pdf": "application/pdf"}


@app.get("/api/jobs/{job_id}/{name}", dependencies=[Depends(auth)])
def job_file(job_id: str, name: str):
    r = _load(job_id)
    if name == "photometry.csv":
        return _csv(r.get("variables", []), ["name", "type", "ra", "dec", "x", "y", "mag", "err", "upper_limit",
                                             "snr", "max", "min", "period", "airmass", "flags"], f"{job_id}_phot.csv")
    if name == "candidates.csv":
        rows = [{**c, "known": (c.get("known") or {}).get("name", "")} for c in r.get("candidates", [])]
        return _csv(rows, ["status", "kind", "label", "ra", "dec", "x", "y", "mag", "snr", "fwhm_px", "known"],
                    f"{job_id}_candidates.csv")
    if name not in FILES:
        raise HTTPException(404, "unknown file")
    p = JOBS_DIR / job_id / name
    if not p.exists():
        raise HTTPException(404, "not available (yet)")
    return FileResponse(p, media_type=FILES[name], filename=f"{job_id}_{name}" if not name.endswith("jpg") else None)


def _csv(rows, cols, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["; ".join(r.get(c)) if isinstance(r.get(c), list) else ("" if r.get(c) is None else r.get(c))
                    for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/share", include_in_schema=False, dependencies=[Depends(auth)])
async def share_target(file: UploadFile = File(...), device: str = Form("auto")):
    """Web Share Target: the installed web app appears in Android's share sheet.

    The browser POSTs the shared picture here; we queue it and send the user to the result.
    """
    r = await _submit(file, {"device": device or "auto", "photometry": "true", "transients": "true"})
    return RedirectResponse(f"/#job={r['id']}", status_code=303)


# ---- web UI ------------------------------------------------------------------------------------
STATIC = ROOT / "static"
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
