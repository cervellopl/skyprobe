"""Search for new objects (novae, supernovae, comets, outbursts) on a single image.

Every detected source is cross-matched with Gaia DR3. What is left over is
screened for artefacts (hot pixels, cosmic rays, satellite trails, edges,
saturation halos) and then identified against:
  * IMCCE SkyBoT   -> known asteroids / comets at the time of exposure
  * AAVSO VSX      -> known variables (incl. catalogued novae)
  * HyperLEDA PGC  -> galaxies (diffuse candidates)
  * NGC/IC (NGC2000.0) -> galaxies, nebulae and star clusters still unmatched by the above -
                          a diffuse candidate landing on a named nebula or cluster is that
                          object, not an uncatalogued comet
Catalogued stars that appear much brighter than Gaia predicts are reported as
possible outbursts.
"""
from __future__ import annotations

import math

import numpy as np
from astropy.coordinates import SkyCoord
import astropy.units as u

from .catalogs import band_mag, dso_near, pgc_galaxies_near
from .photometry import Apertures, aperture_radii, measure


def search(wcs, det: dict, gaia, vsx, minor_bodies: list, cal, fwhm, pixel_scale, saturation, band,
           gaia_mag_limit, width, height, sub, rms, data, log=print, max_candidates=60, limit_mag=None,
           snr_min: float = 7.0, ap: Apertures | None = None):
    o = det["objs"]
    if not o:
        return [], []
    n = len(o["x"])
    r, r_in, r_out = aperture_radii(fwhm, ap)
    flux, ferr, peak, snr = measure(sub, rms, o["x"], o["y"], r, r_in, r_out, saturation, data)
    with np.errstate(invalid="ignore", divide="ignore"):
        minst = -2.5 * np.log10(flux)
    mags = np.full(n, np.nan)
    for i in range(n):
        if np.isfinite(minst[i]):
            mags[i] = cal.mag(minst[i], None, o["x"][i], o["y"][i])[0] if cal else np.nan
    sky = wcs.pixel_to_world(o["x"], o["y"])

    match_r = max(2.0 * pixel_scale, 0.6 * fwhm * pixel_scale, 2.0)  # arcsec
    gcoords = SkyCoord(gaia["ra"], gaia["dec"], unit="deg") if len(gaia) else None
    if gcoords is not None:
        gidx, gsep, _ = sky.match_to_catalog_sky(gcoords)
        gsep = gsep.arcsec
        gband = np.nan_to_num(band_mag(gaia, band), nan=99.0)
        from scipy.spatial import cKDTree

        gpx, gpy = wcs.world_to_pixel(gcoords)
        ok = np.isfinite(gpx) & np.isfinite(gpy)
        gtree = cKDTree(np.c_[np.where(ok, gpx, -1e9), np.where(ok, gpy, -1e9)])
    else:
        gidx, gsep, gband = np.zeros(n, int), np.full(n, np.inf), np.array([])

    # everything we might need to tell apart
    elong = o["a"] / np.maximum(o["b"], 1e-3)
    hot = (o["fwhm"] < 0.65 * fwhm) | (o["npix"] < 4)
    trail = (elong > 3.0) & (o["a"] > 2.0 * fwhm / 2.355 * 1.5)
    edge = (o["x"] < 3 * r_out) | (o["y"] < 3 * r_out) | (o["x"] > width - 1 - 3 * r_out) | (o["y"] > height - 1 - 3 * r_out)
    sat = peak >= saturation
    diffuse = (o["fwhm"] > 1.7 * fwhm) & ~trail

    # halo / spike artefacts around bright saturated stars
    bright = np.where(sat | (snr > 500))[0]
    near_bright = np.zeros(n, bool)
    if len(bright):
        from scipy.spatial import cKDTree

        tree = cKDTree(np.c_[o["x"][bright], o["y"][bright]])
        rad = np.clip(np.sqrt(o["npix"][bright]) * 3, 8 * fwhm, 200)
        d, j = tree.query(np.c_[o["x"], o["y"]])
        near_bright = (d < rad[j]) & (d > 0.5)

    # Candidates must be clearly brighter than the catalogue depth, otherwise
    # "no Gaia counterpart" only means the star is fainter than the query limit.
    lim_new = gaia_mag_limit - 0.75
    limit_mag = limit_mag if limit_mag is not None else gaia_mag_limit
    candidates = []
    for i in range(n):
        if not o["on_sky"][i]:      # lit foreground: buildings, trees, the ground
            continue
        if edge[i] or hot[i] or trail[i] or sat[i] or near_bright[i] or snr[i] < snr_min or not np.isfinite(mags[i]):
            continue
        matched = gsep[i] < match_r * (2.5 if diffuse[i] else 1.0)
        kind = None
        info = {}
        if not matched:
            if mags[i] > lim_new:
                continue
            kind = "diffuse" if diffuse[i] else "new_star"
        else:
            expected = float(gband[gidx[i]]) if len(gband) else np.nan
            # outburst: clearly brighter than catalogue, not blended with a neighbour
            pointlike = o["fwhm"][i] < 1.4 * fwhm and elong[i] < 1.6  # merged pairs mimic outbursts
            if pointlike and np.isfinite(expected) and expected - mags[i] > max(1.5, 5 * (cal.rms if cal else 0.2)):
                close = gtree.query_ball_point([o["x"][i], o["y"][i]], max(match_r * 2 / pixel_scale, r))
                total = -2.5 * np.log10(np.sum(10 ** (-0.4 * gband[close]))) if len(close) else expected
                if total - mags[i] > 1.5:
                    kind = "brightening"
                    info = {"catalog_source": str(gaia["source"][gidx[i]]), "catalog_mag": expected,
                            "delta_mag": float(mags[i] - expected)}
        if kind is None:
            continue
        if kind == "diffuse" and cal is not None:
            # a stellar aperture misses most of a coma: re-measure with an aperture matched to the object
            rd = max(r, 1.5 * o["fwhm"][i])
            fd, _, _, _ = measure(sub, rms, [o["x"][i]], [o["y"][i]], rd, rd + max(3, fwhm), rd + max(3, fwhm) + 3 * rd,
                                  saturation, data)
            if fd[0] > 0:
                mags[i] = cal.mag(-2.5 * np.log10(fd[0]), None, o["x"][i], o["y"][i])[0]
        candidates.append({"kind": kind, "x": float(o["x"][i]), "y": float(o["y"][i]),
                           "ra": float(sky[i].ra.deg), "dec": float(sky[i].dec.deg),
                           "mag": float(mags[i]), "snr": float(snr[i]), "fwhm_px": float(o["fwhm"][i]),
                           "elongation": float(elong[i]), **info})
    log(f"transient search: {len(candidates)} raw candidates from {n} detections (SNR >= {snr_min:g})")

    # --- identify against known objects --------------------------------------------------------
    mb_results = []
    mb_coords = SkyCoord([m["ra"] for m in minor_bodies], [m["dec"] for m in minor_bodies], unit="deg") \
        if minor_bodies else None
    if mb_coords is not None:
        mx, my = wcs.world_to_pixel(mb_coords)
        for k, m in enumerate(minor_bodies):
            if not (np.isfinite(mx[k]) and 0 <= mx[k] < width and 0 <= my[k] < height):
                continue
            tol = max(match_r * 2, m.get("pos_err_arcsec", 0) * 1.5, 3 * pixel_scale, 10.0)
            d = sky.separation(mb_coords[k]).arcsec
            j = int(np.argmin(d))
            vm = m.get("vmag")
            if vm is not None and vm > limit_mag + 1.0:
                continue  # far below the image limit - not worth listing
            det_ok = d[j] < tol and not hot[j] and gsep[j] > match_r  # must not be a catalogued star
            if det_ok and vm is not None and np.isfinite(mags[j]) and abs(mags[j] - vm) > 2.5:
                det_ok = False
            mb_results.append({**m, "x": float(mx[k]), "y": float(my[k]),
                               "detected": bool(det_ok),
                               "measured_mag": float(mags[j]) if det_ok and np.isfinite(mags[j]) else None,
                               "offset_arcsec": float(d[j]) if det_ok else None})
    if candidates:
        cc = SkyCoord([c["ra"] for c in candidates], [c["dec"] for c in candidates], unit="deg")
        for c, cco in zip(candidates, cc):
            if mb_coords is not None:
                d = cco.separation(mb_coords).arcsec
                j = int(np.argmin(d))
                tol = max(match_r * 2, minor_bodies[j].get("pos_err_arcsec", 0) * 1.5, 3 * pixel_scale, 10.0)
                if d[j] < tol:
                    mb = minor_bodies[j]
                    c["known"] = {"catalog": "SkyBoT", "name": mb["name"], "class": mb["class"],
                                  "vmag": mb["vmag"], "offset_arcsec": float(d[j])}
                    continue
            if len(vsx):
                vc = SkyCoord(vsx["ra"], vsx["dec"], unit="deg")
                d = cco.separation(vc).arcsec
                j = int(np.argmin(d))
                if d[j] < max(match_r, 5.0):
                    c["known"] = {"catalog": "VSX", "name": str(vsx["name"][j]), "class": str(vsx["type"][j]),
                                  "max": float(vsx["max"][j]) if np.isfinite(vsx["max"][j]) else None,
                                  "offset_arcsec": float(d[j])}
        diffuse_idx = [i for i, c in enumerate(candidates) if c["kind"] == "diffuse" and "known" not in c]
        if diffuse_idx:
            gal = pgc_galaxies_near(cc[diffuse_idx], radius_arcsec=max(30.0, 2 * match_r))
            for q in gal:
                candidates[diffuse_idx[q]]["known"] = {"catalog": "HyperLEDA", "name": "galaxy (PGC)",
                                                       "class": "galaxy"}
            # HyperLEDA is galaxies only - anything still diffuse and unidentified might be a
            # named nebula or star cluster instead (HyperLEDA also misses some bright, large,
            # well-known galaxies that NGC2000 catches by their common name)
            still_diffuse = [i for i in diffuse_idx if "known" not in candidates[i]]
            if still_diffuse:
                for q, info in dso_near(cc[still_diffuse], radius_arcsec=max(300.0, 3 * match_r)).items():
                    candidates[still_diffuse[q]]["known"] = info

    for c in candidates:
        c["status"] = "known" if "known" in c else "unidentified"
        c["label"] = _label(c)
    # unidentified first, then by brightness
    candidates.sort(key=lambda c: (c["status"] != "unidentified", c["mag"]))
    return candidates[:max_candidates], mb_results


def _label(c):
    k = c.get("known")
    if k:
        if k["catalog"] == "SkyBoT":
            return f"Known {'comet' if 'omet' in k['class'] else 'minor planet'}: {k['name']}"
        if k["catalog"] == "VSX":
            return f"Known variable {k['name']} ({k['class']})"
        if k["catalog"] == "NGC/IC":
            return f"Known {k['class']}: {k['name']}"
        return "Galaxy (HyperLEDA)"
    if c["kind"] == "diffuse":
        return "Unidentified diffuse object - possible comet"
    if c["kind"] == "brightening":
        return f"Star {abs(c['delta_mag']):.1f} mag brighter than catalogue - possible outburst / nova"
    return "Unidentified star-like source - possible nova / supernova / uncatalogued moving object"
