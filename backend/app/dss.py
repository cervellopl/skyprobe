"""Sky-survey cut-outs that line up with our own image, for blinking a candidate.

A "new object" is only interesting if it is not on an older picture of the same patch of
sky. The CDS hips2fits service renders any HiPS survey - DSS2, Pan-STARRS, SDSS, 2MASS -
into a FITS with a WCS, which we resample onto the exact pixel grid of our own close-up.
Blinking the two then compares like with like: same centre, same scale, same rotation and
the same handedness, whatever the camera did.
"""
from __future__ import annotations

import hashlib
import io
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

SERVICE = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"

SURVEYS = {
    "dss2": ("CDS/P/DSS2/color", "DSS2 colour"),
    "dss2red": ("CDS/P/DSS2/red", "DSS2 red"),
    "dss2blue": ("CDS/P/DSS2/blue", "DSS2 blue"),
    "dss2nir": ("CDS/P/DSS2/NIR", "DSS2 near-infrared"),
    "panstarrs": ("CDS/P/PanSTARRS/DR1/color-i-r-g", "Pan-STARRS DR1"),
    "sdss9": ("CDS/P/SDSS9/color", "SDSS DR9"),
    "2mass": ("CDS/P/2MASS/color", "2MASS (infrared)"),
}
DEFAULT_SURVEY = "dss2"


def survey_list() -> list[dict]:
    return [{"id": k, "label": v[1]} for k, v in SURVEYS.items()]


def _fetch(hips: str, ra: float, dec: float, fov_deg: float, px: int, cache: Path, timeout: float = 60.0):
    """One hips2fits rendering, kept on disk: the same close-up is usually looked at twice."""
    q = urllib.parse.urlencode({"hips": hips, "ra": f"{ra:.6f}", "dec": f"{dec:.6f}",
                                "fov": f"{fov_deg:.6f}", "width": px, "height": px,
                                "projection": "TAN", "coordsys": "icrs", "format": "fits"})
    key = hashlib.sha1(q.encode()).hexdigest()[:16]
    path = cache / f"{key}.fits"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{SERVICE}?{q}", timeout=timeout) as r:
            blob = r.read()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(blob)
        tmp.replace(path)
    with fits.open(path) as hdul:
        hdu = hdul[0]
        return np.asarray(hdu.data), WCS(hdu.header).celestial


def aligned_cutout(job_wcs: WCS, x: float, y: float, size: float, out_px: int,
                   survey: str = DEFAULT_SURVEY, cache: Path | None = None, fetch_px: int = 0):
    """Resample a survey onto the pixel grid of our close-up around (x, y).

    `size` is the width of the close-up in original image pixels and `out_px` the rendered
    size, exactly as for /cutout.jpg, so the two images can be blinked without shifting.
    """
    from scipy.ndimage import map_coordinates

    hips = SURVEYS.get(survey, SURVEYS[DEFAULT_SURVEY])[0]
    step = size / out_px
    grid = x - size / 2 + (np.arange(out_px) + 0.5) * step
    gy = y - size / 2 + (np.arange(out_px) + 0.5) * step
    ux, uy = np.meshgrid(grid, gy)
    ra, dec = job_wcs.all_pix2world(ux.ravel(), uy.ravel(), 0)
    if not np.isfinite(ra).any():
        raise ValueError("this part of the image has no valid sky coordinates")

    # a frame a little wider than the close-up, so rotation cannot cut the corners off
    from astropy.coordinates import SkyCoord
    ra0, dec0 = job_wcs.all_pix2world([x], [y], 0)
    centre = SkyCoord(ra0[0], dec0[0], unit="deg")
    sep = centre.separation(SkyCoord(ra, dec, unit="deg")).deg
    fov = float(np.clip(np.nanmax(sep) * 2 * 1.45, 1 / 120, 20.0))      # 30" to 20 deg
    px = int(np.clip(fetch_px or out_px * 3 // 2, 128, 1000))
    data, wcs = _fetch(hips, float(ra0[0]), float(dec0[0]), fov, px, cache or Path("/tmp/skyprobe-dss"))

    px_x, px_y = wcs.all_world2pix(ra, dec, 0)
    coords = np.vstack([px_y, px_x])
    planes = data if data.ndim == 3 else data[None]
    out = []
    for plane in planes[:3]:
        v = map_coordinates(np.nan_to_num(plane.astype(np.float32)), coords, order=1, mode="constant", cval=0.0)
        out.append(v.reshape(out_px, out_px))
    arr = np.dstack(out) if len(out) == 3 else np.dstack(out * 3)
    return _to_8bit(arr)


def _to_8bit(arr: np.ndarray) -> np.ndarray:
    """Survey planes come as 8-bit renderings already; greyscale FITS may not."""
    a = arr.astype(np.float32)
    if a.max() <= 255.0 and a.min() >= 0.0 and a.max() > 1.5:
        return np.clip(a, 0, 255).astype(np.uint8)
    lo, hi = np.nanpercentile(a, [1.0, 99.5])
    if not np.isfinite(hi) or hi <= lo:
        lo, hi = float(np.nanmin(a)), float(np.nanmax(a) or 1.0)
    return np.clip((a - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)


def jpeg(arr: np.ndarray, quality: int = 88) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    return buf.getvalue()
