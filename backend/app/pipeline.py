"""End-to-end processing of one uploaded image."""
from __future__ import annotations

import math
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time, TimeDelta
from astropy.utils import iers
from PIL import Image, ImageDraw, ImageFont

from . import astrometry, catalogs, photometry, transients
from .detect import detect_sources
from .imageio import DEVICE_PRESETS, load_image

iers.conf.auto_download = False  # never block on IERS downloads; precision is not needed here
iers.conf.auto_max_age = None


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def build_hints(opts: dict, meta: dict) -> dict:
    hints = {}
    device = opts.get("device") or meta.get("device") or "auto"
    if device in DEVICE_PRESETS:
        hints["scale"] = DEVICE_PRESETS[device]["scale"]
        hints["scale_tol"] = 0.1
    if _num(opts.get("scale")):
        hints["scale"] = _num(opts["scale"])
        hints["scale_tol"] = 0.15
    elif _num(opts.get("scale_low")) and _num(opts.get("scale_high")):
        hints.pop("scale", None)
        hints["scale_low"], hints["scale_high"] = _num(opts["scale_low"]), _num(opts["scale_high"])
    elif "scale" not in hints and meta.get("scale_hint"):
        hints["scale"] = meta["scale_hint"]
        hints["scale_tol"] = 0.35  # EXIF focal lengths / crops are approximate
    ra, dec = _num(opts.get("ra")), _num(opts.get("dec"))
    if ra is None and meta.get("ra_hint") is not None:
        ra, dec = meta["ra_hint"], meta["dec_hint"]
    if ra is not None and dec is not None:
        hints["ra"], hints["dec"] = ra, dec
        hints["radius"] = _num(opts.get("radius")) or 15.0
    return hints


def observation_time(opts: dict, img) -> tuple[Time | None, str | None]:
    if opts.get("obs_time"):
        t = Time(str(opts["obs_time"]).replace("Z", ""), format="isot", scale="utc")
        src = "user"
    elif img.meta.get("date_obs"):
        dt = datetime.fromisoformat(img.meta["date_obs"])
        off = _num(opts.get("utc_offset"))
        if off is not None and img.meta.get("date_source") == "exif-local-assumed-utc":
            dt = dt - timedelta(hours=off)
            img.meta["date_source"] = "exif+user-offset"
        t = Time(dt.astimezone(timezone.utc).replace(tzinfo=None), scale="utc")
        src = img.meta.get("date_source")
    else:
        return None, None
    exp = img.meta.get("exptime")
    # FITS DATE-OBS / EXIF time mark the start of exposure -> use mid-exposure
    if exp and src in ("fits", "exif+offset", "exif-local-assumed-utc", "exif+user-offset"):
        t = t + TimeDelta(float(exp) / 2.0, format="sec")
    return t, src


def run(job, opts: dict, workdir: Path, log):
    t_start = time.time()
    res = job.result
    in_path = Path(job.input_path)

    job.set_stage("loading", 5)
    img = load_image(in_path)
    h, w = img.shape
    res["file"] = {"name": job.filename, "format": img.fmt, "width": w, "height": h, "linear": img.linear,
                   "band": img.band}
    res["meta"] = _clean(img.meta)
    res["warnings"] = list(img.warnings)
    Image.fromarray(img.preview).convert("RGB").save(workdir / "preview.jpg", quality=88)
    pv = Image.open(workdir / "preview.jpg")
    res["preview"] = {"width": pv.width, "height": pv.height, "scale": pv.width / w}
    log(f"loaded {img.fmt} {w}x{h}, band {img.band}, linear={img.linear}")

    tobs, tsrc = observation_time(opts, img)
    if tobs is not None:
        res["time"] = {"utc_mid": tobs.isot, "jd_mid": float(tobs.jd), "source": tsrc}
    else:
        res["warnings"].append("No observation time found: minor-body identification disabled; "
                               "pass obs_time (ISO UTC) to enable it.")

    job.set_stage("detecting stars", 15)
    det = detect_sources(img.data, img.saturation, thresh=float(opts.get("detect_sigma") or 5.0))
    if det["n"] < 8:
        raise RuntimeError(f"Only {det['n']} stars detected - image too shallow, out of focus, or not a star field")
    fwhm = det["fwhm"]
    res["detections"] = {"count": det["n"], "fwhm_px": fwhm, "background_rms": det["bkg_rms"],
                         "threshold_sigma": det["thresh"], "sky_fraction": det.get("sky_fraction", 1.0),
                         "count_on_sky": det.get("n_on_sky", det["n"])}
    log(f"{det['n']} sources, median FWHM {fwhm:.2f}px")
    if det.get("sky_fraction", 1.0) < 0.97:
        pct = 100 * (1 - det["sky_fraction"])
        log(f"lit foreground masked out: {pct:.0f}% of the frame, {det['n'] - det['n_on_sky']} sources dropped")
        res["warnings"].append(f"{pct:.0f}% of the frame looks like lit foreground (buildings, trees, ground); "
                               "sources there are ignored for solving, photometry and the transient search.")

    # ---- astrometry ------------------------------------------------------------------------
    job.set_stage("plate solving", 25)
    o = det["objs"]
    usable = o["on_sky"] & ~o["edge"] & (o["fwhm"] > 0.6 * fwhm)  # saturated stars are fine for solving
    if usable.sum() < 8:
        usable = ~o["edge"] & (o["fwhm"] > 0.6 * fwhm)
    xs, ys, fl = o["x"][usable], o["y"][usable], o["flux"][usable]
    hints = build_hints(opts, img.meta)
    res["solve_hints"] = hints
    solver = (opts.get("solver") or os.environ.get("SOLVER", "auto")).lower()
    api_key = opts.get("api_key") or os.environ.get("ASTROMETRY_API_KEY", "")
    wcs, used = None, None
    if img.header_wcs is not None and str(opts.get("use_header_wcs", "true")).lower() != "false":
        if _verify_wcs(img.header_wcs, xs, ys, fl, w, h):
            wcs, used = img.header_wcs, "fits-header"
        else:
            log("WCS in FITS header did not verify against Gaia - re-solving")
    t0 = time.time()
    crop_used = None
    if wcs is None:
        order = []
        if solver in ("auto", "local") and astrometry.local_available():
            order.append("local")
        if solver in ("auto", "remote") and (api_key or solver == "remote"):
            order.append("remote")
        if not order:
            raise RuntimeError("No plate solver available: install astrometry.net (solve-field) "
                               "or provide an astrometry.net API key")
        # A wide phone lens is nowhere near the TAN projection a solver assumes, so quads that
        # span the frame never match. Solve the middle of the frame first, then fit the distortion.
        est_fov = (hints.get("scale") or meta_scale(img)) * max(w, h) / 3600.0
        crops = [1.0, 0.45, 0.3] if est_fov < 30 else [0.45, 0.3, 1.0]
        errors = []
        for backend in order:
            for crop in crops:
                sel = usable if crop >= 1.0 else usable & _central(o, w, h, crop)
                if sel.sum() < 12:
                    continue
                cxs, cys, cfl = o["x"][sel], o["y"][sel], o["flux"][sel]
                for attempt_hints in _hint_ladder(hints):
                    try:
                        if backend == "local":
                            wcs = astrometry.solve_local(cxs, cys, cfl, w, h, attempt_hints,
                                                         timeout=int(os.environ.get("SOLVE_TIMEOUT", 120)), log=log)
                        else:
                            png, sc = astrometry.solve_image_png(img.data)
                            wcs = astrometry.solve_remote(cxs, cys, cfl, w, h, attempt_hints, api_key, png, sc, log=log)
                        used = f"astrometry.net ({'solve-field' if backend == 'local' else 'nova.astrometry.net'})"
                        crop_used = crop
                        break
                    except astrometry.SolveError as e:
                        errors.append(f"{backend}: {e}")
                        log(f"solve attempt failed ({backend}, crop {crop:.2f}, hints={list(attempt_hints)}): {e}")
                if wcs is not None:
                    if crop < 1.0:
                        log(f"solved on the central {crop * 100:.0f}% of the frame")
                    break
            if wcs is not None:
                break
        if wcs is None:
            raise RuntimeError(
                "Plate solving failed. Check that the image really shows stars, then try giving a pixel "
                "scale or an RA/Dec hint, or switch to nova.astrometry.net (it has index files for every "
                "field size). Solver output: " + " | ".join(errors[-2:]))

    # widen the solution to the whole frame (lens distortion)
    first = astrometry.wcs_summary(wcs, w, h)
    if first["fov_w_deg"] > 8 or (crop_used or 1.0) < 1.0:
        def _ref(centre, radius):
            # a couple of magnitudes deeper than the photometry limit: more stars pin the corners down
            lim = min(catalogs.choose_mag_limit(first["pixel_scale"],
                                                first["fov_w_deg"] * first["fov_h_deg"], first["gal_b"]) + 2.0, 11.0)
            return catalogs.reference_stars(centre, radius, lim,
                                            tobs.jyear if tobs is not None else None)

        wcs, dist_info = astrometry.refine_wcs(wcs, o["x"][usable], o["y"][usable], w, h, _ref, log=log)
        res["distortion"] = dist_info
    summary = astrometry.wcs_summary(wcs, w, h)
    summary.update({"solver": used, "solve_seconds": round(time.time() - t0, 1)})
    res["solution"] = summary
    res["detections"]["fwhm_arcsec"] = fwhm * summary["pixel_scale"]
    (workdir / "solution.wcs").write_text(wcs.to_header(relax=True).tostring(sep="\n"))
    wcs.to_fits(relax=True).writeto(workdir / "wcs.fits", overwrite=True)
    log(f"solved: RA {summary['ra_hms']} Dec {summary['dec_dms']}, {summary['pixel_scale']:.2f}\"/px, "
        f"FOV {summary['fov_w_deg']:.2f}x{summary['fov_h_deg']:.2f} deg")
    job.save()

    want_phot = str(opts.get("photometry", "true")).lower() != "false"
    want_trans = str(opts.get("transients", "true")).lower() != "false"
    if not (want_phot or want_trans):
        _annotate(workdir, img, res)
        res["processing_seconds"] = round(time.time() - t_start, 1)
        return

    # ---- catalogues ---------------------------------------------------------------------------
    job.set_stage("querying catalogues", 45)
    center = SkyCoord(summary["ra"], summary["dec"], unit="deg")
    area = summary["fov_w_deg"] * summary["fov_h_deg"]
    maglim = _num(opts.get("mag_limit")) or catalogs.choose_mag_limit(summary["pixel_scale"], area, summary["gal_b"])
    radius = summary["radius_deg"] * 1.02
    epoch = tobs.jyear if tobs is not None else None
    with ThreadPoolExecutor(3) as ex:
        f_gaia = ex.submit(catalogs.reference_stars, center, radius, maglim, epoch)
        f_vsx = ex.submit(catalogs.vsx_stars, center, radius, maglim)
        f_sb = ex.submit(catalogs.skybot_field, wcs, w, h, float(tobs.jd), radius, center) \
            if (tobs is not None and want_trans) else None
        gaia = f_gaia.result()
        if gaia.meta.get("catalog") == "tycho2":
            maglim = min(maglim, 11.5)
        try:
            vsx = f_vsx.result()
        except Exception as e:
            log(f"VSX query failed: {e}")
            res["warnings"].append("VSX query failed - variable stars not measured")
            vsx = catalogs.Table(names=("oid", "name", "ra", "dec", "type", "max", "min", "min_is_amp", "band",
                                        "period", "flag"),
                                 dtype=(int, str, float, float, str, float, float, bool, str, float, int))
        try:
            minor = f_sb.result() if f_sb else []
        except Exception as e:
            log(f"SkyBoT query failed: {e}")
            res["warnings"].append("SkyBoT query failed - known asteroids/comets not checked")
            minor = []
    log(f"catalogues: {len(gaia)} {gaia.meta.get('catalog')} (<{maglim}), {len(vsx)} VSX, {len(minor)} minor bodies")

    # ---- photometric calibration ---------------------------------------------------------------
    job.set_stage("photometric calibration", 60)
    band = (opts.get("band") or img.band).upper()
    cal, comps = None, None
    try:
        cal, cal_info, comps = photometry.calibrate(wcs, det["sub"], det["rms"], img.data, gaia, vsx, fwhm,
                                                    img.saturation, band, w, h, log=log, sky=det.get("sky"),
                                                    linear_sensor=img.linear and img.fmt == "fits")
        cal_info["gaia_mag_limit"] = maglim
        res["calibration"] = cal_info
        if abs(cal_info.get("response_slope", 0.0)) > 0.05 or abs(cal_info.get("response_curve", 0.0)) > 0.02:
            res["warnings"].append(
                f"The camera response is not linear (fitted slope {1 + cal_info['response_slope']:.2f} instead "
                "of 1.00): the phone's night mode stacked and tone-compressed this file. Bright stars read "
                "too faint, by more than a magnitude near the top of the range. Magnitudes are calibrated "
                "empirically against the catalogue and are indicative only.")
        if cal_info.get("limit_mag_5sigma") and cal_info["limit_mag_5sigma"] > maglim + 0.5:
            res["warnings"].append(f"Image reaches mag {cal_info['limit_mag_5sigma']:.1f} but catalogues were "
                                   f"queried to {maglim:.1f}; fainter transients are not searched "
                                   "(set mag_limit to go deeper).")
    except Exception as e:
        log(f"calibration failed: {e}")
        res["warnings"].append(f"Photometric calibration failed: {e}")

    airmass_fn = _airmass_fn(opts, img.meta, tobs)
    if want_phot and cal is not None:
        job.set_stage("variable star photometry", 70)
        res["variables"] = photometry.measure_variables(wcs, cal, comps, vsx, det["sub"], det["rms"], img.data,
                                                        fwhm, img.saturation, w, h, band, airmass_fn,
                                                        sky=det.get("sky"))
        log(f"measured {len(res['variables'])} VSX variables")
        nonlinear = (abs(res.get("calibration", {}).get("response_slope", 0.0)) > 0.05
                     or res.get("calibration", {}).get("response_curve_amplitude", 0.0) > 0.15)
        if tobs is not None and not nonlinear:
            (workdir / "aavso.txt").write_text(photometry.aavso_extended(
                res["variables"], float(tobs.jd), band, opts.get("obscode") or "XXX"))
        elif nonlinear:
            res["warnings"].append("AAVSO export disabled for this image: the camera response is not "
                                   "linear, so the magnitudes are not suitable for submission.")

    limit_mag = (res.get("calibration") or {}).get("limit_mag_5sigma")
    # "not in the catalogue" only means something if the catalogue goes deeper than the image.
    # On a wide phone frame the image reaches ~11 mag while Tycho-2 stops at ~8, and every
    # unresolved clump of Milky Way stars would be reported as a discovery.
    searchable = cal is not None and (limit_mag is None or maglim >= limit_mag - 0.5)
    if want_trans and not searchable:
        res["search_skipped"] = {
            "reason": "catalogue shallower than the image",
            "catalogue_limit": maglim, "image_limit": limit_mag,
        }
        res["warnings"].append(
            f"New-object search skipped: this image reaches about magnitude {limit_mag:.1f}, but the "
            f"reference catalogue for a field this wide only goes to {maglim:.1f}. Anything fainter than "
            "the catalogue would look like a discovery - at this pixel scale most 'sources' are unresolved "
            "blends of faint stars. Use a longer focal length (narrower field) for a meaningful search.")
        log(f"new-object search skipped: catalogue {maglim:.1f} vs image limit {limit_mag:.1f}")
    if want_trans:
        job.set_stage("searching for new objects", 82)
        cands, mbs = transients.search(wcs, det, gaia, vsx, minor, cal, fwhm, summary["pixel_scale"],
                                       img.saturation, band, maglim, w, h, det["sub"], det["rms"], img.data,
                                       log=log, limit_mag=(res.get("calibration") or {}).get("limit_mag_5sigma"))
        # known asteroids/comets stay useful even when the candidate search is not meaningful
        res["candidates"] = cands if searchable else []
        res["minor_bodies"] = sorted(mbs, key=lambda m: (m["vmag"] is None, m["vmag"] or 99))
        if cal is None:
            res["warnings"].append("Without photometric calibration, transient magnitudes are unavailable.")

    job.set_stage("rendering", 95)
    _annotate(workdir, img, res)
    res["processing_seconds"] = round(time.time() - t_start, 1)


def _central(o, w, h, frac):
    """Detections inside the central `frac` of the frame."""
    return (np.abs(o["x"] - w / 2) < w * frac / 2) & (np.abs(o["y"] - h / 2) < h * frac / 2)


def meta_scale(img) -> float:
    return float(img.meta.get("scale_hint") or 2.0)


def _hint_ladder(hints: dict):
    """Try with all hints first, then progressively blind."""
    yield hints
    if "ra" in hints:
        h2 = {k: v for k, v in hints.items() if k not in ("ra", "dec", "radius")}
        yield h2
    if any(k in hints for k in ("scale", "scale_low")) and "ra" not in hints:
        yield {}


def _verify_wcs(wcs, xs, ys, fl, w, h) -> bool:
    """Check that a header WCS actually matches the stars (Seestar headers can be approximate)."""
    try:
        s = astrometry.wcs_summary(wcs, w, h)
        center = SkyCoord(s["ra"], s["dec"], unit="deg")
        area = s["fov_w_deg"] * s["fov_h_deg"]
        g = catalogs.reference_stars(center, s["radius_deg"], min(catalogs.choose_mag_limit(s["pixel_scale"], area, s["gal_b"]), 13.0))
        if len(g) < 10:
            return False
        order = np.argsort(-fl)[:150]
        sky = wcs.pixel_to_world(xs[order], ys[order])
        _, d2d, _ = sky.match_to_catalog_sky(SkyCoord(g["ra"], g["dec"], unit="deg"))
        frac = float(np.mean(d2d.arcsec < 3 * s["pixel_scale"]))
        return frac > 0.4
    except Exception:
        return False


def _airmass_fn(opts, meta, tobs):
    lat = _num(opts.get("lat")) if _num(opts.get("lat")) is not None else meta.get("lat")
    lon = _num(opts.get("lon")) if _num(opts.get("lon")) is not None else meta.get("lon")
    if lat is None or lon is None or tobs is None:
        return None
    from astropy.coordinates import AltAz, EarthLocation
    import astropy.units as u

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=(meta.get("elevation_m") or 0) * u.m)
    frame = AltAz(obstime=tobs, location=loc)

    def fn(coord):
        try:
            alt = float(coord.transform_to(frame).alt.deg)
            if not math.isfinite(alt) or alt <= 1:
                return None
            z = math.radians(90 - alt)
            sec = 1 / math.cos(z)
            # Hardie (1962)
            am = sec - 0.0018167 * (sec - 1) - 0.002875 * (sec - 1) ** 2 - 0.0008083 * (sec - 1) ** 3
            return float(am) if math.isfinite(am) else None
        except Exception:
            return None

    return fn


def _annotate(workdir: Path, img, res):
    """Server-side annotated JPEG (used by the Android app and for sharing)."""
    base = Image.open(workdir / "preview.jpg").convert("RGB")
    s = res["preview"]["scale"]
    d = ImageDraw.Draw(base)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(11, int(base.width / 110)))
    except Exception:
        font = ImageFont.load_default()
    rr = max(6, int(base.width / 150))

    def circ(x, y, col, r=rr, width=2):
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], outline=col, width=width)

    def text(x, y, t, col):
        d.text((x * s + rr + 3, y * s - rr - 2), t, fill=col, font=font, stroke_width=2, stroke_fill=(0, 0, 0))

    for v in res.get("variables", [])[:150]:
        col = (80, 200, 255) if not v["upper_limit"] else (120, 140, 160)
        circ(v["x"], v["y"], col)
        label = v["name"] + ("" if v["upper_limit"] else f" {v['mag']:.2f}")
        text(v["x"], v["y"], label, col)
    for m in res.get("minor_bodies", [])[:100]:
        col = (255, 170, 60) if m["detected"] else (160, 120, 80)
        x, y = m["x"] * s, m["y"] * s
        d.rectangle([x - rr, y - rr, x + rr, y + rr], outline=col, width=2)
        text(m["x"], m["y"], m["name"], col)
    for c in res.get("candidates", []):
        col = (255, 60, 60) if c["status"] == "unidentified" else (255, 220, 60)
        circ(c["x"], c["y"], col, r=int(rr * 1.6), width=3)
        if c["status"] == "unidentified":
            text(c["x"], c["y"], f"? {c['mag']:.1f}", col)
    base.save(workdir / "annotated.jpg", quality=88)


def _clean(meta: dict) -> dict:
    out = {}
    for k, v in meta.items():
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        if isinstance(v, float) and not math.isfinite(v):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
