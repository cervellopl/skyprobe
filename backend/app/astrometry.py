"""Plate solving with astrometry.net.

Two back-ends:
  * ``local``  - the ``solve-field`` binary (apt install astrometry.net + index files)
  * ``remote`` - the nova.astrometry.net web API (needs an API key)

Both receive a list of detected star positions (x, y, flux) instead of the full
image, so even 50 MB RAW files solve quickly and the WCS is valid for the
full-resolution pixel grid. The remote back-end falls back to uploading a
downscaled JPEG if the source-list upload is rejected.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import warnings
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS, FITSFixedWarning
from PIL import Image

NOVA_URL = os.environ.get("ASTROMETRY_URL", "https://nova.astrometry.net").rstrip("/")


class SolveError(RuntimeError):
    pass


def _xylist(xs, ys, fluxes, path: Path, max_sources=400):
    order = np.argsort(-np.asarray(fluxes))[:max_sources]
    # astrometry.net uses FITS 1-based pixel coordinates
    t = Table({"X": np.asarray(xs)[order] + 1.0, "Y": np.asarray(ys)[order] + 1.0,
               "FLUX": np.asarray(fluxes)[order]})
    t.write(path, format="fits", overwrite=True)


def _wcs_from_header(hdr: fits.Header) -> WCS:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(hdr, naxis=2)


def scale_bounds(hints: dict) -> tuple[float, float] | None:
    s = hints.get("scale")
    if s:
        return s * (1 - hints.get("scale_tol", 0.2)), s * (1 + hints.get("scale_tol", 0.2))
    if hints.get("scale_low") and hints.get("scale_high"):
        return hints["scale_low"], hints["scale_high"]
    return None


# ----------------------------------------------------------------------------
# local solve-field
# ----------------------------------------------------------------------------

def local_available() -> bool:
    return shutil.which(os.environ.get("SOLVE_FIELD", "solve-field")) is not None


def solve_local(xs, ys, fluxes, width, height, hints: dict, timeout=180, log=print) -> WCS:
    exe = shutil.which(os.environ.get("SOLVE_FIELD", "solve-field"))
    if not exe:
        raise SolveError("solve-field not installed")
    with tempfile.TemporaryDirectory(prefix="solve_") as td:
        td = Path(td)
        xy = td / "stars.xyls"
        _xylist(xs, ys, fluxes, xy)
        cmd = [exe, str(xy), "--width", str(width), "--height", str(height),
               "--x-column", "X", "--y-column", "Y", "--sort-column", "FLUX",
               "--no-plots", "--overwrite", "--no-remove-lines", "--uniformize", "0", "--dir", str(td), "--cpulimit", str(timeout),
               "--tweak-order", str(hints.get("tweak_order", 2)), "--crpix-center",
               "--new-fits", "none", "--corr", "none", "--rdls", "none", "--match", "none",
               "--index-xyls", "none", "--solved", str(td / "solved"), "--wcs", str(td / "out.wcs")]
        sb = scale_bounds(hints)
        if sb:
            cmd += ["--scale-units", "arcsecperpix", "--scale-low", f"{sb[0]:.4f}", "--scale-high", f"{sb[1]:.4f}"]
        else:
            cmd += ["--scale-units", "degwidth", "--scale-low", "0.1", "--scale-high", "180"]
        if hints.get("ra") is not None and hints.get("dec") is not None:
            cmd += ["--ra", f"{hints['ra']:.6f}", "--dec", f"{hints['dec']:.6f}",
                    "--radius", f"{hints.get('radius', 10.0):.3f}"]
        log("solve-field " + " ".join(cmd[2:]))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            raise SolveError("solve-field timed out")
        wcs_path = td / "out.wcs"
        if not wcs_path.exists():
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
            raise SolveError(f"solve-field found no solution. {tail}")
        return _wcs_from_header(fits.getheader(wcs_path))


# ----------------------------------------------------------------------------
# nova.astrometry.net
# ----------------------------------------------------------------------------

class NovaClient:
    def __init__(self, api_key: str, base=NOVA_URL, log=print):
        self.base = base
        self.log = log
        self.s = requests.Session()
        r = self.s.post(f"{base}/api/login", data={"request-json": json.dumps({"apikey": api_key})}, timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "success":
            raise SolveError(f"astrometry.net login failed: {j.get('errormessage', j)}")
        self.session = j["session"]

    def _upload(self, payload: dict, filename: str, content: bytes, ctype: str) -> int:
        payload = {"session": self.session, "allow_commercial_use": "n", "allow_modifications": "n",
                   "publicly_visible": "n", **payload}
        files = {"file": (filename, content, ctype)}
        r = self.s.post(f"{self.base}/api/upload", data={"request-json": json.dumps(payload)}, files=files,
                        timeout=300)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "success":
            raise SolveError(f"astrometry.net upload failed: {j.get('errormessage', j)}")
        return j["subid"]

    def _wait(self, subid: int, timeout: float) -> int:
        t0 = time.time()
        job_id = None
        while time.time() - t0 < timeout:
            if job_id is None:
                j = self.s.get(f"{self.base}/api/submissions/{subid}", timeout=60).json()
                jobs = [x for x in j.get("jobs", []) if x]
                if jobs:
                    job_id = jobs[0]
                    self.log(f"astrometry.net job {job_id}")
                elif j.get("error_message"):
                    raise SolveError(j["error_message"])
            else:
                j = self.s.get(f"{self.base}/api/jobs/{job_id}", timeout=60).json()
                st = j.get("status")
                if st == "success":
                    return job_id
                if st == "failure":
                    raise SolveError("astrometry.net could not solve the field")
            time.sleep(5)
        raise SolveError("astrometry.net timed out")

    def wcs(self, job_id: int) -> fits.Header:
        r = self.s.get(f"{self.base}/wcs_file/{job_id}", timeout=120)
        r.raise_for_status()
        with fits.open(io.BytesIO(r.content)) as hl:
            return hl[0].header.copy()

    def solve(self, payload: dict, filename: str, content: bytes, ctype: str, timeout: float) -> tuple[int, fits.Header]:
        subid = self._upload(payload, filename, content, ctype)
        self.log(f"astrometry.net submission {subid}")
        job = self._wait(subid, timeout)
        return job, self.wcs(job)


def _hint_payload(hints: dict) -> dict:
    p = {"tweak_order": hints.get("tweak_order", 2), "crpix_center": True}
    sb = scale_bounds(hints)
    if sb:
        p.update({"scale_units": "arcsecperpix", "scale_type": "ul", "scale_lower": sb[0], "scale_upper": sb[1]})
    if hints.get("ra") is not None and hints.get("dec") is not None:
        p.update({"center_ra": hints["ra"], "center_dec": hints["dec"], "radius": hints.get("radius", 10.0)})
    return p


def rescale_wcs_header(hdr: fits.Header, s: float) -> fits.Header:
    """WCS for an image downscaled by factor ``s`` (small = full / s) -> full-res WCS.

    x_full + 0.5 = s * (x_small + 0.5) in 1-based FITS pixel coordinates (pixel edges align).
    """
    h = hdr.copy()
    for k in ("CRPIX1", "CRPIX2"):
        h[k] = (h[k] - 0.5) * s + 0.5
    for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2", "CDELT1", "CDELT2"):
        if k in h:
            h[k] = h[k] / s
    for pre in ("A", "B", "AP", "BP"):
        order = h.get(f"{pre}_ORDER")
        if order is None:
            continue
        for p in range(order + 1):
            for q in range(order + 1 - p):
                k = f"{pre}_{p}_{q}"
                if k in h:
                    h[k] = h[k] * s ** (1 - p - q)
    for k in ("IMAGEW", "IMAGEH"):
        if k in h:
            h[k] = int(round(h[k] * s))
    return h


def solve_remote(xs, ys, fluxes, width, height, hints: dict, api_key: str, preview_png: bytes | None = None,
                 preview_scale: float | None = None, timeout=600, log=print) -> WCS:
    if not api_key:
        raise SolveError("No astrometry.net API key (set ASTROMETRY_API_KEY or pass api_key)")
    client = NovaClient(api_key, log=log)
    payload = _hint_payload(hints)
    # 1) source list upload - tiny and keeps full-res geometry
    with tempfile.TemporaryDirectory() as td:
        xy = Path(td) / "stars.xyls"
        _xylist(xs, ys, fluxes, xy)
        content = xy.read_bytes()
    try:
        _, hdr = client.solve({**payload, "image_width": width, "image_height": height}, "stars.xyls",
                              content, "application/fits", timeout)
        return _wcs_from_header(hdr)
    except SolveError as e:
        if preview_png is None:
            raise
        log(f"source-list solve failed ({e}); uploading downscaled image")
    # 2) downscaled image upload, WCS rescaled to full resolution
    p2 = dict(payload)
    if "scale_lower" in p2 and preview_scale:
        p2["scale_lower"] *= preview_scale
        p2["scale_upper"] *= preview_scale
    _, hdr = client.solve(p2, "image.png", preview_png, "image/png", timeout)
    return _wcs_from_header(rescale_wcs_header(hdr, preview_scale or 1.0))


def solve_image_png(data: np.ndarray, max_side=2000) -> tuple[bytes, float]:
    from .imageio import auto_stretch

    im = Image.fromarray(auto_stretch(data))
    w, h = im.size
    s = max(1.0, max(w, h) / max_side)
    if s > 1:
        im = im.resize((round(w / s), round(h / s)), Image.LANCZOS)
    s_exact = w / im.size[0]
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), s_exact


def refine_wcs(wcs: WCS, xs, ys, width: int, height: int, reference_stars, log=print, min_match=25):
    """Fit a distortion model across the whole frame.

    Wide phone lenses are far from the gnomonic (TAN) projection that a plate
    solver assumes, so a solution found in the middle of the frame can be wrong
    by many pixels in the corners - which ruins aperture photometry and invents
    "new" objects. Catalogue stars are matched to detections with a growing
    radius, and a sigma-clipped SIP polynomial is fitted to everything that
    matches. The best-fitting order wins.

    Returns (wcs, info); the input WCS is returned unchanged if nothing helped.
    """
    from astropy.coordinates import SkyCoord
    from astropy.wcs.utils import fit_wcs_from_points
    from scipy.spatial import cKDTree

    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    if len(xs) < min_match:
        return wcs, {"refined": False, "reason": "too few detections"}
    tree = cKDTree(np.c_[xs, ys])
    summary = wcs_summary(wcs, width, height)
    centre = SkyCoord(summary["ra"], summary["dec"], unit="deg")
    cat = reference_stars(centre, min(summary["radius_deg"] * 1.25, 89.0))
    if cat is None or len(cat) < min_match:
        return wcs, {"refined": False, "reason": "no reference stars"}
    cat_coords = SkyCoord(np.asarray(cat["ra"], float), np.asarray(cat["dec"], float), unit="deg")

    def match(current, tol):
        cx, cy = current.world_to_pixel(cat_coords)
        ok = np.isfinite(cx) & np.isfinite(cy) & (cx > -50) & (cy > -50) & (cx < width + 50) & (cy < height + 50)
        if ok.sum() < min_match:
            return None
        dist, idx = tree.query(np.c_[cx[ok], cy[ok]], distance_upper_bound=tol)
        hit = np.isfinite(dist)
        if hit.sum() < min_match:
            return None
        det_i = idx[hit]
        keep, seen = [], set()
        for k in np.argsort(dist[hit]):     # one catalogue star per detection, the closest wins
            if det_i[k] not in seen:
                seen.add(det_i[k])
                keep.append(k)
        keep = np.array(keep)
        return xs[det_i[keep]], ys[det_i[keep]], cat_coords[ok][hit][keep]

    def fit(px, py, sky, degree, iters=3):
        cand = rms = None
        for _ in range(iters):
            try:
                cand = fit_wcs_from_points((px, py), sky, proj_point=centre, projection="TAN", sip_degree=degree)
            except Exception as e:      # ill-conditioned, or too few points for that order
                log(f"distortion fit (order {degree}) failed: {e}")
                return None, None, 0
            fx, fy = cand.world_to_pixel(sky)
            resid = np.hypot(fx - px, fy - py)
            rms = float(np.sqrt(np.mean(resid ** 2)))
            good = resid < max(3 * np.median(resid), 1.0)
            if good.sum() < min_match * 2 or good.all():
                break
            px, py, sky = px[good], py[good], sky[good]
        return cand, rms, len(px)

    # bootstrap: each pass improves the corners, so the next one matches more stars
    current = wcs
    for tol in (12, 25, 40):
        m = match(current, tol)
        if m is None:
            continue
        cand, rms, n = fit(*m, degree=3)
        if cand is not None and rms is not None and rms < tol:
            current = cand

    m = match(current, 25)
    if m is None:
        return wcs, {"refined": False, "reason": "no matches"}
    best, info = wcs, {"refined": False, "reason": "no improvement"}
    best_rms = np.inf
    for degree in (2, 3, 4):
        cand, rms, n = fit(*m, degree=degree)
        if cand is None or rms is None or n < min_match:
            continue
        if rms < best_rms:
            best, best_rms = cand, rms
            info = {"refined": True, "sip_degree": degree, "n_matched": int(n),
                    "residual_rms_px": rms, "reference_stars": int(len(cat))}
    if info["refined"]:
        log(f"distortion fit: SIP order {info['sip_degree']}, {info['n_matched']} stars, "
            f"residual rms {best_rms:.2f} px")
    return best, info


# ----------------------------------------------------------------------------
# WCS summary
# ----------------------------------------------------------------------------

def wcs_summary(wcs: WCS, width: int, height: int) -> dict:
    from astropy.wcs.utils import proj_plane_pixel_scales

    cx, cy = (width - 1) / 2, (height - 1) / 2
    c = wcs.pixel_to_world(cx, cy)
    scales = proj_plane_pixel_scales(wcs.celestial) * 3600.0
    scale = float(np.mean(scales))
    cd = wcs.celestial.pixel_scale_matrix
    det = np.linalg.det(cd)
    parity = "normal" if det < 0 else "flipped"
    # position angle of image "up" (-y in array order is up on screen) measured East of North
    top = wcs.pixel_to_world(cx, cy - 10)
    rot = float(c.position_angle(top).deg) % 360
    corners = wcs.pixel_to_world(np.array([0, width - 1, width - 1, 0]), np.array([0, 0, height - 1, height - 1]))
    radius = float(max(c.separation(corners).deg))
    from astropy.coordinates import Galactic

    gal = c.transform_to(Galactic())
    return {
        "ra": float(c.ra.deg), "dec": float(c.dec.deg),
        "ra_hms": c.ra.to_string(unit="hourangle", sep=":", precision=1, pad=True),
        "dec_dms": c.dec.to_string(sep=":", precision=0, alwayssign=True, pad=True),
        "pixel_scale": scale, "fov_w_deg": scale * width / 3600, "fov_h_deg": scale * height / 3600,
        "radius_deg": radius, "rotation_deg": rot, "parity": parity,
        "gal_l": float(gal.l.deg), "gal_b": float(gal.b.deg),
        "corners": [[float(p.ra.deg), float(p.dec.deg)] for p in corners],
        "has_sip": bool(wcs.sip is not None),
    }
