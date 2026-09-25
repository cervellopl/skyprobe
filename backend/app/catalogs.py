"""Online catalogues: Gaia DR3 + AAVSO VSX (VizieR) and IMCCE SkyBoT (minor bodies).

Results are cached on disk so re-processing the same field is fast.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
import requests
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table

CACHE = Path(os.environ.get("CATALOG_CACHE", Path(__file__).resolve().parent.parent / "data" / "catalog_cache"))
VIZIER_SERVER = os.environ.get("VIZIER_SERVER", "vizier.cds.unistra.fr")
SKYBOT_URL = "https://ssp.imcce.fr/webservices/skybot/api/conesearch.php"


def _cache_path(kind: str, **kw) -> Path:
    key = hashlib.sha1(json.dumps(kw, sort_keys=True, default=str).encode()).hexdigest()[:20]
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{kind}_{key}.ecsv"


def _vizier(columns, filters, catalog, center: SkyCoord, radius_deg: float, row_limit=-1) -> Table:
    from astroquery.vizier import Vizier

    v = Vizier(columns=columns, column_filters=filters, row_limit=row_limit, timeout=180)
    v.VIZIER_SERVER = VIZIER_SERVER
    res = v.query_region(center, radius=radius_deg * u.deg, catalog=catalog)
    if len(res) == 0:
        return Table()
    return res[0]


def expected_gaia_count(mag: float, area_deg2: float, gal_b: float) -> float:
    """Rough all-sky-average Gaia G star counts, boosted towards the Milky Way plane."""
    per_deg2 = 10 ** (0.85 + 0.37 * (mag - 10))
    return per_deg2 * area_deg2 * (1 + 5 * math.exp(-abs(gal_b) / 12))


def choose_mag_limit(pixel_scale: float, area_deg2: float, gal_b: float, max_rows=40000) -> float:
    guess = 17.5 - 4.5 * math.log10(max(pixel_scale, 0.3) / 2.4)
    guess = float(np.clip(guess, 7.0, 19.0))
    while guess > 6.0 and expected_gaia_count(guess, area_deg2, gal_b) > max_rows:
        guess -= 0.25
    return round(guess, 2)


def gaia_stars(center: SkyCoord, radius_deg: float, mag_lim: float, epoch_jyear: float | None = None) -> Table:
    cp = _cache_path("gaia", ra=round(center.ra.deg, 4), dec=round(center.dec.deg, 4), r=round(radius_deg, 4), m=mag_lim)
    if cp.exists():
        t = Table.read(cp)
    else:
        t = _vizier(["Source", "RA_ICRS", "DE_ICRS", "Gmag", "BP-RP", "pmRA", "pmDE"], {"Gmag": f"<{mag_lim}"},
                    "I/355/gaiadr3", center, radius_deg)
        if len(t):
            t = Table({"source": np.asarray(t["Source"]).astype(str), "ra": np.asarray(t["RA_ICRS"], float),
                       "dec": np.asarray(t["DE_ICRS"], float), "g": np.asarray(t["Gmag"], float),
                       "bp_rp": np.asarray(t["BP-RP"].filled(np.nan) if hasattr(t["BP-RP"], "filled") else t["BP-RP"], float),
                       "pmra": np.asarray(t["pmRA"].filled(0) if hasattr(t["pmRA"], "filled") else t["pmRA"], float),
                       "pmde": np.asarray(t["pmDE"].filled(0) if hasattr(t["pmDE"], "filled") else t["pmDE"], float)})
        else:
            t = Table(names=("source", "ra", "dec", "g", "bp_rp", "pmra", "pmde"),
                      dtype=(str, float, float, float, float, float, float))
        t.write(cp, overwrite=True)
    if epoch_jyear and len(t):
        dt = epoch_jyear - 2016.0
        pmra = np.nan_to_num(np.asarray(t["pmra"], float))
        pmde = np.nan_to_num(np.asarray(t["pmde"], float))
        t["ra"] = t["ra"] + pmra * dt / 3.6e6 / np.cos(np.radians(t["dec"]))
        t["dec"] = t["dec"] + pmde * dt / 3.6e6
    return t


GAIA_MAX_RADIUS = float(os.environ.get("GAIA_MAX_RADIUS", 2.5))  # deg; VizieR truncates larger Gaia cones


def reference_stars(center: SkyCoord, radius_deg: float, mag_lim: float, epoch_jyear: float | None = None) -> Table:
    """Photometric/astrometric reference stars.

    Gaia DR3 for narrow fields (telescopes, Seestar); Tycho-2 for wide fields (phones, lenses),
    where huge Gaia cone searches are slow and get truncated by VizieR.
    Columns: source, ra, dec, g (brightness proxy), bp_rp (colour index); meta['catalog'].
    """
    if radius_deg <= GAIA_MAX_RADIUS:
        t = gaia_stars(center, radius_deg, mag_lim, epoch_jyear)
        t.meta["catalog"] = "gaia"
        return t
    return tycho2_stars(center, radius_deg, min(mag_lim, 11.5), epoch_jyear)


def tycho2_stars(center: SkyCoord, radius_deg: float, mag_lim: float, epoch_jyear: float | None = None) -> Table:
    cp = _cache_path("tyc2", ra=round(center.ra.deg, 4), dec=round(center.dec.deg, 4), r=round(radius_deg, 4), m=mag_lim)
    if cp.exists():
        t = Table.read(cp)
    else:
        r = _vizier(["TYC1", "TYC2", "TYC3", "RAmdeg", "DEmdeg", "BTmag", "VTmag", "pmRA", "pmDE"],
                    {"VTmag": f"<{mag_lim + 0.3}"}, "I/259/tyc2", center, radius_deg)

        def col(name, fill):
            c = r[name]
            return np.asarray(c.filled(fill) if hasattr(c, "filled") else c, float)

        if len(r):
            bt, vt = col("BTmag", np.nan), col("VTmag", np.nan)
            bv = 0.850 * (bt - vt)  # ESA 1997, Tycho -> Johnson
            v = vt - 0.090 * (bt - vt)
            v = np.where(np.isfinite(v), v, vt)
            ids = [f"TYC {a}-{b}-{c}" for a, b, c in zip(np.asarray(r["TYC1"]), np.asarray(r["TYC2"]), np.asarray(r["TYC3"]))]
            t = Table({"source": np.array(ids), "ra": col("RAmdeg", np.nan), "dec": col("DEmdeg", np.nan),
                       "g": v, "bp_rp": bv, "pmra": col("pmRA", 0.0), "pmde": col("pmDE", 0.0)})
            t = t[np.isfinite(t["ra"]) & np.isfinite(t["dec"]) & (t["g"] < mag_lim)]
        else:
            t = Table(names=("source", "ra", "dec", "g", "bp_rp", "pmra", "pmde"),
                      dtype=(str, float, float, float, float, float, float))
        t.write(cp, overwrite=True)
    if epoch_jyear and len(t):
        dt = epoch_jyear - 2000.0
        t["ra"] = t["ra"] + np.nan_to_num(np.asarray(t["pmra"], float)) * dt / 3.6e6 / np.cos(np.radians(t["dec"]))
        t["dec"] = t["dec"] + np.nan_to_num(np.asarray(t["pmde"], float)) * dt / 3.6e6
    t.meta["catalog"] = "tycho2"
    return t


def band_mag(t: Table, band: str):
    """Catalogue magnitude in the photometric band of the image."""
    if t.meta.get("catalog") == "tycho2":
        v = np.asarray(t["g"], float)
        bv = np.asarray(t["bp_rp"], float)
        if band in ("B", "TB"):
            return v + bv
        if band in ("R", "TR"):
            return v - (0.53 * bv + 0.02)  # rough main-sequence V-Rc
        if band == "I":
            return v - (1.05 * bv + 0.03)  # rough main-sequence V-Ic
        return v
    return gaia_to_band(t["g"], t["bp_rp"], band)


def gaia_to_band(g, bp_rp, band: str):
    """Gaia DR3 -> Johnson-Cousins transformations (Gaia DR3 documentation, Sect. 5.5.1, Table 5.9)."""
    c = np.asarray(bp_rp, float)
    g = np.asarray(g, float)
    if band in ("R", "TR"):
        return g - (-0.02275 + 0.3961 * c - 0.1243 * c ** 2 - 0.01396 * c ** 3 + 0.003775 * c ** 4)
    if band == "I":
        return g - (0.01753 + 0.76 * c - 0.0991 * c ** 2)
    if band == "B" or band == "TB":
        v = g - (-0.02704 + 0.01424 * c - 0.2156 * c ** 2 + 0.01426 * c ** 3)
        # B-V from BP-RP (empirical, valid 0 < BP-RP < 2.5)
        bv = -0.0453 + 0.8488 * c - 0.1311 * c ** 2 + 0.0224 * c ** 3
        return v + bv
    # V (also used for TG and unfiltered CV per AAVSO practice)
    return g - (-0.02704 + 0.01424 * c - 0.2156 * c ** 2 + 0.01426 * c ** 3)


def vsx_stars(center: SkyCoord, radius_deg: float, mag_lim: float) -> Table:
    cp = _cache_path("vsx", ra=round(center.ra.deg, 4), dec=round(center.dec.deg, 4), r=round(radius_deg, 4), m=mag_lim)
    if cp.exists():
        return Table.read(cp)
    t = _vizier(["OID", "Name", "V", "RAJ2000", "DEJ2000", "Type", "max", "min", "f_min", "n_max", "Period"],
                {"max": f"<{mag_lim}"}, "B/vsx/vsx", center, radius_deg)
    if len(t) == 0:
        out = Table(names=("oid", "name", "ra", "dec", "type", "max", "min", "min_is_amp", "band", "period", "flag"),
                    dtype=(int, str, float, float, str, float, float, bool, str, float, int))
    else:
        def col(name, fill):
            c = t[name]
            return np.asarray(c.filled(fill) if hasattr(c, "filled") else c)

        out = Table({"oid": col("OID", 0).astype(int), "name": col("Name", "").astype(str),
                     "ra": col("RAJ2000", np.nan).astype(float), "dec": col("DEJ2000", np.nan).astype(float),
                     "type": col("Type", "").astype(str), "max": col("max", np.nan).astype(float),
                     "min": col("min", np.nan).astype(float), "min_is_amp": col("f_min", "").astype(str) == "(",
                     "band": col("n_max", "").astype(str), "period": col("Period", np.nan).astype(float),
                     "flag": col("V", 0).astype(int)})
        # VSX flags amplitudes with "(" in f_min, but not always - a "min" brighter than "max" is an amplitude
        out["min_is_amp"] = out["min_is_amp"] | (np.isfinite(out["min"]) & (out["min"] < out["max"]))
        out = out[out["flag"] != 2]  # 2 = not variable / non-existent
    out.write(cp, overwrite=True)
    return out


def skybot(center: SkyCoord, radius_deg: float, jd: float, location: str = "500") -> list[dict]:
    """Known asteroids and comets inside a cone (radius <= 10 deg per request)."""
    params = {"-ep": f"{jd:.5f}", "-ra": f"{center.ra.deg:.6f}", "-dec": f"{center.dec.deg:.6f}",
              "-rd": f"{min(radius_deg, 10.0):.5f}", "-mime": "json", "-output": "basic", "-loc": location,
              "-filter": "0", "-objFilter": "111"}
    r = requests.get(SKYBOT_URL, params=params, timeout=120)
    if r.status_code != 200 or not r.text.strip().startswith("["):
        return []
    out = []
    for o in r.json():
        try:
            c = SkyCoord(o["RA (hms)"], o["DEC (dms)"], unit=(u.hourangle, u.deg))
        except Exception:
            continue
        name = str(o.get("Name", ""))
        num = o.get("Num")
        cls = str(o.get("Class", ""))
        vmag = o.get("VMag (mag)")
        out.append({
            "name": f"({num}) {name}" if num else name, "class": cls,
            "is_comet": cls.lower().startswith("comet") or name[:2] in ("C/", "P/") or "P/" in name[:6],
            "ra": float(c.ra.deg), "dec": float(c.dec.deg),
            # SkyBoT reports 0.0 for comets without a magnitude model
            "vmag": float(vmag) if vmag not in (None, "") and float(vmag) != 0.0 else None,
            "pos_err_arcsec": float(o.get("Err (arcsec)", 0) or 0),
            "rate_ra_arcsec_h": float(o.get("dRA (arcsec/h)", 0) or 0),
            "rate_dec_arcsec_h": float(o.get("dDEC (arcsec/h)", 0) or 0),
        })
    return out


def skybot_field(wcs, width: int, height: int, jd: float, radius_deg: float, center: SkyCoord) -> list[dict]:
    """SkyBoT limits the cone radius to 10 deg, so tile wide (phone) fields with parallel cones."""
    if radius_deg <= 10:
        return skybot(center, radius_deg, jd)
    from concurrent.futures import ThreadPoolExecutor
    from astropy.wcs.utils import proj_plane_pixel_scales

    scale = float(np.mean(proj_plane_pixel_scales(wcs.celestial)))  # deg/px
    nx = max(1, int(math.ceil(width * scale / 13.0)))
    ny = max(1, int(math.ceil(height * scale / 13.0)))
    xs = (np.arange(nx) + 0.5) * width / nx
    ys = (np.arange(ny) + 0.5) * height / ny
    cones = []
    for xi in xs:
        for yi in ys:
            c = wcs.pixel_to_world(xi, yi)
            corner = wcs.pixel_to_world(xi + width / nx / 2, yi + height / ny / 2)
            cones.append((c, min(10.0, float(c.separation(corner).deg) * 1.05)))
    seen = {}
    with ThreadPoolExecutor(6) as ex:
        for res in ex.map(lambda cr: _safe_skybot(cr[0], cr[1], jd), cones):
            for o in res:
                seen[o["name"]] = o
    return list(seen.values())


def _safe_skybot(c, r, jd):
    try:
        return skybot(c, r, jd)
    except Exception:
        return []


def pgc_galaxies_near(coords: SkyCoord, radius_arcsec: float = 30) -> set[int]:
    """Indices of coordinates that have a HyperLEDA (PGC) galaxy within radius."""
    if len(coords) == 0:
        return set()
    from astroquery.vizier import Vizier

    v = Vizier(columns=["PGC", "RAJ2000", "DEJ2000", "_q"], row_limit=-1, timeout=120)
    v.VIZIER_SERVER = VIZIER_SERVER
    try:
        res = v.query_region(coords, radius=radius_arcsec * u.arcsec, catalog="VII/237/pgc")
    except Exception:
        return set()
    if len(res) == 0 or "_q" not in res[0].colnames:
        return set()
    return {int(q) - 1 for q in np.asarray(res[0]["_q"])}


# NGC2000.0 object-type codes worth reporting as a known deep-sky object; everything else
# (bare/double/triple stars, asterisms, galaxy knots, blank or "?"/"-" uncertain entries -
# many of the original Dreyer NGC numbers turned out to be nonexistent) is not a distinct
# object and is left for the candidate to be identified some other way, or stay unidentified.
_NGC_TYPES = {
    "Gx": "galaxy", "OC": "open cluster", "Gb": "globular cluster",
    "Nb": "nebula", "Pl": "planetary nebula", "C+N": "cluster with nebulosity",
}


def dso_near(coords: SkyCoord, radius_arcsec: float = 300.0) -> dict[int, dict]:
    """NGC/IC galaxies, nebulae and star clusters near each of `coords`.

    NGC2000.0 positions are only good to about 1 arcminute (they come from the original
    19th-century Dreyer compilation) - and for a large object the catalogued "position" is
    itself a judgement call, sometimes several arcminutes from the coordinate another source
    quotes (M42's listed centre and the Trapezium are 4' apart, for a nebula 66' across). The
    default radius is generous for that reason; a truly huge object's outer reaches can still
    fall outside it, but a diffuse candidate detected as one compact blob is realistically
    nowhere near that large to begin with. Returns
    `{index into coords: {"catalog": "NGC/IC", "name": "NGC 6205", "class": "globular cluster"}}`
    for the closest reported match of each coordinate that lands on a real object type.
    """
    if len(coords) == 0:
        return {}
    from astroquery.vizier import Vizier

    v = Vizier(columns=["Name", "Type", "RAB2000", "DEB2000", "_q"], row_limit=-1, timeout=120)
    v.VIZIER_SERVER = VIZIER_SERVER
    try:
        res = v.query_region(coords, radius=radius_arcsec * u.arcsec, catalog="VII/118/ngc2000")
    except Exception:
        return {}
    if len(res) == 0 or "_q" not in res[0].colnames:
        return {}
    # more than one object can fall inside a several-arcminute radius (e.g. the galaxy IC 1296
    # sits under 2' from the Ring Nebula) - row order is not distance order, so the actual
    # separation has to be checked to keep the closest real match for each coordinate
    best: dict[int, tuple[float, dict]] = {}
    for row in res[0]:
        cls = _NGC_TYPES.get(str(row["Type"]).strip())
        if cls is None:
            continue
        idx = int(row["_q"]) - 1
        try:
            pos = SkyCoord(f"{row['RAB2000']} {row['DEB2000']}", unit=(u.hourangle, u.deg))
        except Exception:
            continue
        sep = float(coords[idx].separation(pos).arcsec)
        if idx in best and best[idx][0] <= sep:
            continue
        raw = str(row["Name"]).strip()
        name = f"IC {raw[1:].strip()}" if raw[:1] == "I" else f"NGC {raw}"
        best[idx] = (sep, {"catalog": "NGC/IC", "name": name, "class": cls})
    return {i: info for i, (_sep, info) in best.items()}
