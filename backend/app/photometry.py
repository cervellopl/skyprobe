"""Aperture photometry calibrated against a Gaia DR3 ensemble.

* Comparison stars: isolated, unsaturated, non-variable Gaia stars transformed
  to the Johnson band that matches the image (V for TG/CV).
* A global zero point + colour term is fitted with sigma clipping; a *local*
  correction (median residual of the nearest comparison stars) absorbs
  vignetting and differential extinction across wide phone fields.
* Every VSX variable in the field is measured with forced photometry at its
  catalogue position; faint ones get an upper limit.
"""
from __future__ import annotations

import math

import numpy as np
import sep
from astropy.coordinates import SkyCoord
import astropy.units as u

from .catalogs import band_mag


def aperture_radii(fwhm: float):
    r = float(np.clip(1.4 * fwhm, 2.0, 40.0))
    r_in = r + max(3.0, fwhm)
    r_out = r_in + max(5.0, 2.0 * fwhm)
    return r, r_in, r_out


def measure(sub, rms, x, y, r, r_in, r_out, saturation, data=None, gain=1.0):
    """Background-annulus aperture photometry; returns flux, flux error, peak and SNR."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) == 0:
        z = np.zeros(0)
        return z, z, z, z
    h, w = sub.shape
    xi = np.clip(np.round(x).astype(int), 0, w - 1)
    yi = np.clip(np.round(y).astype(int), 0, h - 1)
    err = np.ascontiguousarray(rms, dtype=np.float32)
    flux, ferr, _ = sep.sum_circle(sub, x, y, r, err=err, gain=gain, bkgann=(r_in, r_out), subpix=5)
    peak = np.zeros(len(x))
    ri = int(math.ceil(r))
    img = data if data is not None else sub
    for i, (cx, cy) in enumerate(zip(xi, yi)):
        patch = img[max(cy - ri, 0): cy + ri + 1, max(cx - ri, 0): cx + ri + 1]
        peak[i] = patch.max() if patch.size else np.nan
    snr = np.where(ferr > 0, flux / ferr, 0.0)
    return flux, ferr, peak, snr


def _robust_fit(y, c, minst=None, use_color=True, use_nonlinearity=False, iters=5, kappa=3.0):
    """Robust fit of  m_cat - m_inst = zp + k*color [+ b1*dm + b2*dm^2].

    The optional terms in dm = m_inst - median(m_inst) absorb a non-linear camera
    response. Phones that stack and tone-compress their "RAW" files need it;
    a linear sensor simply fits b1 = b2 = 0.
    """
    mask = np.isfinite(y) & np.isfinite(c)
    dm = np.zeros_like(y) if minst is None else np.asarray(minst, float) - np.nanmedian(minst)
    dm = np.where(np.isfinite(dm), dm, 0.0)
    zp, k, b1, b2 = (float(np.nanmedian(y[mask])) if mask.any() else 0.0), 0.0, 0.0, 0.0
    for _ in range(iters):
        if mask.sum() < 3:
            break
        cols = [np.ones(mask.sum())]
        names = []
        if use_color and mask.sum() >= 10 and np.ptp(c[mask]) > 0.3:
            cols.append(c[mask]); names.append("k")
        if use_nonlinearity and mask.sum() >= 30 and np.ptp(dm[mask]) > 1.0:
            # a slope only: a quadratic would extrapolate wildly past the faintest comparison star
            cols.append(dm[mask]); names.append("b1")
        coef, *_ = np.linalg.lstsq(np.vstack(cols).T, y[mask], rcond=None)
        zp, k, b1, b2 = float(coef[0]), 0.0, 0.0, 0.0
        for name, value in zip(names, coef[1:]):
            if name == "k":
                k = float(value)
            elif name == "b1":
                b1 = float(value)
            else:
                b2 = float(value)
        if abs(b1) > 0.5:      # implausible response: refuse to "correct" it
            b1 = 0.0
        res = y - (zp + k * c + b1 * dm + b2 * dm ** 2)
        sig = 1.4826 * np.median(np.abs(res[mask] - np.median(res[mask]))) + 1e-3
        new = np.isfinite(res) & (np.abs(res) < kappa * sig)
        if new.sum() == mask.sum() and np.all(new == mask):
            break
        mask = new
    res = y - (zp + k * c + b1 * dm + b2 * dm ** 2)
    return zp, k, mask, res, b1, b2


class Calibration:
    """Maps instrumental magnitudes to catalogue magnitudes for one image."""

    def __init__(self, x, y, color, resid, zp, k, mask, band, n_local=25, b1=0.0, b2=0.0, m0=0.0):
        self.x, self.y, self.color, self.resid = x, y, color, resid
        self.zp, self.k, self.mask, self.band = zp, k, mask, band
        self.b1, self.b2, self.m0 = b1, b2, m0        # empirical response curve
        self.dm_lo, self.dm_hi = -3.0, 3.0            # never extrapolate the response past the comps
        self.bright_margin = 0.0                      # widened when the response is non-linear
        self.curve_x = self.curve_y = None            # empirical response curve (binned medians)
        self.n_local = n_local
        self.median_color = float(np.nanmedian(color[mask])) if mask.any() else 0.8
        sel = mask
        self.rms = float(np.std(resid[sel])) if sel.sum() > 1 else float("nan")
        from scipy.spatial import cKDTree

        self._used = np.where(mask)[0]
        self._tree = cKDTree(np.c_[x[self._used], y[self._used]]) if len(self._used) else None

    def response(self, m_inst):
        """Magnitude-dependent correction, held flat outside the calibrated range."""
        if self.curve_x is None or not np.isfinite(m_inst):
            return 0.0
        return float(np.interp(m_inst, self.curve_x, self.curve_y))

    def local(self, px, py, exclude=None):
        """Local zero-point correction from the nearest comparison stars."""
        if self._tree is None:
            return 0.0, float("nan"), 0
        excl = set(np.atleast_1d(exclude).tolist()) if exclude is not None else set()
        k = min(self.n_local + len(excl), len(self._used))
        _, nn = self._tree.query([px, py], k=k)
        near = [i for i in self._used[np.atleast_1d(nn)] if i not in excl][: self.n_local]
        if not near:
            return 0.0, float("nan"), 0
        r = self.resid[near]
        corr = float(np.median(r))
        sig = float(1.4826 * np.median(np.abs(r - corr))) if len(r) > 2 else self.rms
        return corr, sig / math.sqrt(max(len(r), 1)), len(near)

    def mag(self, m_inst, color, px, py, exclude=None, with_flags=False):
        c = self.median_color if color is None or not np.isfinite(color) else float(np.clip(color, -0.5, 3.5))
        corr, zerr, n = self.local(px, py, exclude)
        raw_dm = float(m_inst) - self.m0
        dm = float(np.clip(raw_dm, self.dm_lo, self.dm_hi))
        # Brighter than the brightest comparison star (with a margin, because that is exactly
        # where a compressed or clipped response bites), or fainter than the faintest one:
        # the calibration would be an extrapolation.
        outside = raw_dm < self.dm_lo + self.bright_margin or raw_dm > self.dm_hi + 0.2
        value = m_inst + self.zp + self.k * c + self.b1 * dm + self.b2 * dm ** 2 + self.response(m_inst) + corr
        if with_flags:
            return value, zerr, n, outside
        return value, zerr, n


def calibrate(wcs, sub, rms, data, gaia, vsx, fwhm, saturation, band, width, height, log=print, sky=None,
              linear_sensor=True):
    """Build a Calibration from Gaia stars in the image. Returns (Calibration, info dict, comp table)."""
    r, r_in, r_out = aperture_radii(fwhm)
    if len(gaia) == 0:
        raise RuntimeError("No Gaia stars in field")
    gx, gy = wcs.world_to_pixel(SkyCoord(gaia["ra"], gaia["dec"], unit="deg"))
    margin = r_out + 2
    inside = np.isfinite(gx) & np.isfinite(gy) & (gx > margin) & (gy > margin) & (gx < width - 1 - margin) & (gy < height - 1 - margin)
    g = gaia[inside]
    gx, gy = gx[inside], gy[inside]
    color = np.asarray(g["bp_rp"], float)
    catmag = band_mag(g, band)
    on_sky = np.ones(len(gx), bool)
    if sky is not None:      # comparison stars must not sit on a lit building
        on_sky = sky[np.clip(np.round(gy).astype(int), 0, height - 1), np.clip(np.round(gx).astype(int), 0, width - 1)]
    # isolation: no Gaia neighbour within 2 apertures that contributes > 5 % of the flux
    from scipy.spatial import cKDTree

    tree = cKDTree(np.c_[gx, gy])
    pairs = tree.query_pairs(r_in + fwhm)
    crowded = np.zeros(len(gx), bool)
    gm = np.asarray(g["g"], float)
    for i, j in pairs:
        if gm[j] < gm[i] + 3.3:
            crowded[i] = True
        if gm[i] < gm[j] + 3.3:
            crowded[j] = True
    # exclude known variables
    is_var = np.zeros(len(gx), bool)
    if len(vsx):
        vc = SkyCoord(vsx["ra"], vsx["dec"], unit="deg")
        gc = SkyCoord(g["ra"], g["dec"], unit="deg")
        idx, d2d, _ = gc.match_to_catalog_sky(vc)
        is_var = d2d.arcsec < 3.0
    flux, ferr, peak, snr = measure(sub, rms, gx, gy, r, r_in, r_out, saturation, data)
    with np.errstate(invalid="ignore", divide="ignore"):
        minst = -2.5 * np.log10(flux)
    good = (on_sky & np.isfinite(minst) & np.isfinite(catmag) & (snr > 15) & (peak < saturation) & ~crowded
            & ~is_var & np.isfinite(color) & (color > -0.2) & (color < 2.5))
    if good.sum() < 5:  # relax constraints for sparse / wide fields
        good = (on_sky & np.isfinite(minst) & np.isfinite(catmag) & (snr > 8) & (peak < saturation) & ~is_var
                & np.isfinite(color))
    if good.sum() < 3:
        raise RuntimeError(f"Too few comparison stars ({int(good.sum())})")
    y = catmag - minst
    m0 = float(np.nanmedian(minst[good])) if good.any() else 0.0
    zp, k, mask, res, b1, b2 = _robust_fit(np.where(good, y, np.nan), color, minst, use_color=True,
                                           use_nonlinearity=not linear_sensor)
    mask &= good
    cal = Calibration(gx, gy, color, res, zp, k, mask, band, b1=b1, b2=b2, m0=m0)
    if mask.any():          # the response curve is only trusted where comparison stars exist
        cal.dm_lo = float(np.min(minst[mask]) - m0)
        cal.dm_hi = float(np.max(minst[mask]) - m0)
    if not linear_sensor and mask.sum() >= 60:
        # A tone-compressed phone RAW bends: a straight line cannot follow it. Fit binned
        # medians of the residual against instrumental magnitude and interpolate between them.
        mi, ri = minst[mask], res[mask]
        edges = np.quantile(mi, np.linspace(0, 1, 9))
        xs, ys = [], []
        for lo, hi in zip(edges[:-1], edges[1:]):
            inb = (mi >= lo) & (mi <= hi)
            if inb.sum() >= 15:
                xs.append(float(np.median(mi[inb])))
                ys.append(float(np.median(ri[inb])))
        if len(xs) >= 4 and (max(ys) - min(ys)) > 0.15:
            cal.curve_x, cal.curve_y = np.array(xs), np.array(ys)
            cal.bright_margin = 0.3      # do not trust the very brightest stars on such a frame
            res = res - np.array([cal.response(v) if np.isfinite(v) else 0.0 for v in minst])
            cal.resid = res
            log(f"response curve fitted over {max(xs) - min(xs):.1f} mag, "
                f"amplitude {max(ys) - min(ys):.2f} mag")
    # 5-sigma point source limit
    npix = math.pi * r * r
    bg_noise = float(np.median(rms)) * math.sqrt(npix)
    limit = -2.5 * math.log10(5 * bg_noise) + zp + k * cal.median_color if bg_noise > 0 else None
    info = {"band": band, "aperture_px": r, "annulus_px": [r_in, r_out], "n_comps": int(mask.sum()),
            "response_slope": b1, "response_curve": b2,
            "response_curve_amplitude": (float(np.ptp(cal.curve_y)) if cal.curve_y is not None else 0.0),
            "n_gaia_in_field": int(len(g)), "zero_point": zp, "color_term": k, "rms": cal.rms,
            "limit_mag_5sigma": limit, "catalog": ("Tycho-2" if gaia.meta.get("catalog") == "tycho2" else "Gaia DR3")
                       + " -> Johnson " + ("V" if band in ("TG", "CV", "V") else band)}
    local_res = []
    for i in np.where(mask)[0][:400]:
        corr, _, _ = cal.local(gx[i], gy[i], exclude=i)
        local_res.append(res[i] - corr)
    info["rms_local"] = float(np.std(local_res)) if len(local_res) > 10 else None
    log(f"calibration: {mask.sum()} comps, zp={zp:.3f} k={k:.3f} rms={cal.rms:.3f}"
        + (f" (after local correction {info['rms_local']:.3f})" if info["rms_local"] else "")
        + (f" nonlinear b1={b1:+.3f} b2={b2:+.3f}" if abs(b1) > 0.02 or abs(b2) > 0.02 else ""))
    comps = {"source": np.asarray(g["source"]), "x": gx, "y": gy, "catmag": catmag, "color": color,
             "minst": minst, "snr": snr, "used": mask, "ra": np.asarray(g["ra"]), "dec": np.asarray(g["dec"]),
             "g": gm, "peak": peak}
    return cal, info, comps


def measure_variables(wcs, cal: Calibration, comps: dict, vsx, sub, rms, data, fwhm, saturation, width, height,
                      band, airmass_fn=None, sky=None):
    r, r_in, r_out = aperture_radii(fwhm)
    if len(vsx) == 0:
        return []
    vc = SkyCoord(vsx["ra"], vsx["dec"], unit="deg")
    vx, vy = wcs.world_to_pixel(vc)
    margin = r_out + 2
    inside = np.isfinite(vx) & (vx > margin) & (vy > margin) & (vx < width - 1 - margin) & (vy < height - 1 - margin)
    if sky is not None:      # a variable hidden behind a roof cannot be measured
        inside &= sky[np.clip(np.round(np.nan_to_num(vy)).astype(int), 0, height - 1),
                      np.clip(np.round(np.nan_to_num(vx)).astype(int), 0, width - 1)]
    idxs = np.where(inside)[0]
    if len(idxs) == 0:
        return []
    flux, ferr, peak, snr = measure(sub, rms, vx[idxs], vy[idxs], r, r_in, r_out, saturation, data)
    from astropy.wcs.utils import proj_plane_pixel_scales
    from scipy.spatial import cKDTree

    pscale = float(np.mean(proj_plane_pixel_scales(wcs.celestial))) * 3600
    ctree = cKDTree(np.c_[comps["x"], comps["y"]])
    self_r = max(1.5, 3.0 / pscale)  # px
    out = []
    for n, i in enumerate(idxs):
        row = vsx[i]
        tc = vc[i]
        # colour + identity from Gaia counterpart
        dj, j = ctree.query([vx[i], vy[i]])
        self_idx = int(j) if dj < self_r else None
        color = float(comps["color"][j]) if self_idx is not None else None
        # crowding: brightest other Gaia star inside the aperture
        in_ap = np.array([k for k in ctree.query_ball_point([vx[i], vy[i]], r) if k != self_idx], int)
        blend = bool(len(in_ap) and self_idx is not None and np.min(comps["g"][in_ap]) < comps["g"][self_idx] + 2.5)
        rec = {"name": str(row["name"]), "type": str(row["type"]), "oid": int(row["oid"]),
               "max": _f(row["max"]), "min": _f(row["min"]), "min_is_amplitude": bool(row["min_is_amp"]),
               "range_band": str(row["band"]), "period": _f(row["period"]),
               "ra": float(tc.ra.deg), "dec": float(tc.dec.deg), "x": float(vx[i]), "y": float(vy[i]),
               "vsx_url": f"https://www.aavso.org/vsx/index.php?view=detail.top&oid={int(row['oid'])}",
               "saturated": bool(peak[n] >= saturation), "blended": blend, "snr": float(snr[n])}
        # check star: nearest used comp of similar brightness (excluded from the ensemble)
        exclude = [self_idx] if self_idx is not None else []
        kinfo = None
        if cal._tree is not None:
            _, nn = cal._tree.query([vx[i], vy[i]], k=min(6, len(cal._used)))
            kk = np.array([q for q in cal._used[np.atleast_1d(nn)] if q not in exclude][:5], int)
        else:
            kk = np.array([], int)
        if len(kk):
            ref = rec["max"] if rec["max"] is not None else float(np.median(comps["catmag"][kk]))
            kidx = int(kk[np.argmin(np.abs(comps["catmag"][kk] - ref))])
            exclude.append(kidx)
            km, _, _ = cal.mag(comps["minst"][kidx], comps["color"][kidx], comps["x"][kidx], comps["y"][kidx],
                               exclude=exclude)
            kinfo = {"name": _star_name(comps["source"][kidx]), "measured": float(km),
                     "catalog": float(comps["catmag"][kidx])}
        rec["check"] = kinfo
        if flux[n] > 0 and snr[n] >= 5:
            m_inst = -2.5 * math.log10(flux[n])
            mag, zerr, nloc, outside = cal.mag(m_inst, color, vx[i], vy[i], exclude=exclude, with_flags=True)
            err = math.sqrt((1.0857 / snr[n]) ** 2 + zerr ** 2 + (0.5 * cal.rms / math.sqrt(max(nloc, 1))) ** 2)
            if outside:
                err = max(err, 0.3)
            rec.update({"mag": float(mag), "err": float(err), "upper_limit": False, "n_local_comps": nloc,
                        "outside_calibration": bool(outside)})
        else:
            noise = float(ferr[n]) if ferr[n] > 0 else float(np.median(rms)) * math.sqrt(math.pi * r * r)
            lim, _, _ = cal.mag(-2.5 * math.log10(5 * noise), color, vx[i], vy[i], exclude=exclude)
            rec.update({"mag": float(lim), "err": None, "upper_limit": True})
        if airmass_fn:
            rec["airmass"] = airmass_fn(tc)
        flags = []
        if rec.get("outside_calibration"):
            flags.append("outside calibrated range")
        if rec["saturated"]:
            flags.append("saturated")
        if blend:
            flags.append("blended")
        if rec["upper_limit"]:
            flags.append("fainter than")
        rec["flags"] = flags
        out.append(rec)
    out.sort(key=lambda r: (r["upper_limit"], r["saturated"], r["mag"]))
    return out


def _star_name(src) -> str:
    src = str(src)
    return src if src.startswith("TYC") else f"Gaia DR3 {src}"


def _f(v):
    try:
        v = float(v)
        return v if np.isfinite(v) else None
    except Exception:
        return None


def aavso_extended(variables, jd: float, band: str, obscode: str = "XXX", software="SkyProbe") -> str:
    lines = ["#TYPE=EXTENDED", f"#OBSCODE={obscode}", f"#SOFTWARE={software}", "#DELIM=,", "#DATE=JD",
             "#OBSTYPE=DSLR" if band in ("TG", "TB", "TR") else "#OBSTYPE=CCD",
             "#STARID,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES"]
    for v in variables:
        if v.get("saturated") or v.get("outside_calibration"):
            continue      # extrapolated or clipped: not fit to report
        mag = f"<{v['mag']:.3f}" if v["upper_limit"] else f"{v['mag']:.3f}"
        merr = "na" if v["err"] is None else f"{v['err']:.3f}"
        k = v.get("check") or {}
        kname = k.get("name", "na").replace(",", " ")
        kmag = f"{k['measured']:.3f}" if k else "na"
        am = f"{v['airmass']:.3f}" if v.get("airmass") else "na"
        notes = f"Ensemble of catalogue comps (Gaia DR3/Tycho-2 transformed); {v.get('n_local_comps', 0)} local comps"
        if v.get("blended"):
            notes += "; possible blend"
        lines.append(",".join([v["name"].replace(",", " "), f"{jd:.5f}", mag, merr, band, "NO", "STD",
                               "ENSEMBLE", "na", kname, kmag, am, "na", "na", notes]))
    return "\n".join(lines) + "\n"
