"""Render a synthetic Seestar-like frame from real Gaia DR3 stars (for end-to-end tests).

Injects an artificial "nova" (star with no catalogue counterpart), a diffuse "comet"
and a brightened catalogue star so the transient search can be verified.
"""
import json
import sys
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalogs import gaia_stars, gaia_to_band  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "synthetic")
out.mkdir(parents=True, exist_ok=True)
ra0, dec0 = 325.6787, 43.5862  # SS Cyg
DATE = "2025-09-15T21:30:00"
asteroid = None
if len(sys.argv) > 2 and sys.argv[2] == "asteroid":
    # centre the field near the brightest numbered asteroid around RA 2h, Dec +10 at DATE
    from astropy.time import Time
    from app.catalogs import skybot

    jd = Time(DATE).jd + 300 / 86400
    bodies = [b for b in skybot(SkyCoord(30.0, 10.0, unit="deg"), 5.0, jd) if b["vmag"] and not b["is_comet"]]
    asteroid = min(bodies, key=lambda b: b["vmag"])
    ra0, dec0 = asteroid["ra"] + 0.15, asteroid["dec"] - 0.3
    print("asteroid", asteroid)
W, H, scale, rot = 1080, 1920, 2.39, 17.0
w = WCS(naxis=2)
w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
w.wcs.crval = [ra0, dec0]
w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]
th = np.radians(rot)
s = scale / 3600
w.wcs.cd = [[-s * np.cos(th), s * np.sin(th)], [s * np.sin(th), s * np.cos(th)]]

g = gaia_stars(SkyCoord(ra0, dec0, unit="deg"), 0.8, 17.0, 2025.7)
x, y = w.world_to_pixel(SkyCoord(g["ra"], g["dec"], unit="deg"))
v = gaia_to_band(g["g"], np.nan_to_num(np.asarray(g["bp_rp"], float), nan=0.8), "V")
rng = np.random.default_rng(1)
img = np.full((H, W), 800.0)
ZP = 25.0  # flux = 10**(-0.4*(m-ZP))
sig = 3.0 / 2.355


def add(xc, yc, mag, sigma=sig, rad=None):
    rad = rad or int(max(6, 5 * sigma))
    xi, yi = int(round(xc)), int(round(yc))
    x0, x1, y0, y1 = max(xi - rad, 0), min(xi + rad + 1, W), max(yi - rad, 0), min(yi + rad + 1, H)
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    f = 10 ** (-0.4 * (mag - ZP))
    img[y0:y1, x0:x1] += f / (2 * np.pi * sigma ** 2) * np.exp(-((xx - xc) ** 2 + (yy - yc) ** 2) / (2 * sigma ** 2))


inside = (x > -10) & (x < W + 10) & (y > -10) & (y < H + 10) & np.isfinite(v)
for xc, yc, m in zip(x[inside], y[inside], v[inside]):
    add(xc, yc, m)
truth = {}
if asteroid:
    ax, ay = w.world_to_pixel(SkyCoord(asteroid["ra"], asteroid["dec"], unit="deg"))
    add(float(ax), float(ay), min(asteroid["vmag"], 13.0))
    truth["asteroid"] = [asteroid["name"], asteroid["ra"], asteroid["dec"], asteroid["vmag"]]
# nova: in an empty spot
nx, ny = 300.3, 1400.7
add(nx, ny, 12.0)
truth["nova"] = [float(c) for c in w.pixel_to_world_values(nx, ny)] + [12.0]
# comet: diffuse blob
cx, cy = 780.2, 500.4
add(cx, cy, 11.0, sigma=6.0, rad=40)
truth["comet"] = [float(c) for c in w.pixel_to_world_values(cx, cy)] + [11.0]
# brightening of a catalogued faint star (~16.3 -> 12.5)
cand = np.where(inside & (v > 16) & (v < 16.6) & (x > 100) & (x < W - 100) & (y > 100) & (y < H - 100))[0][0]
add(x[cand], y[cand], 12.5)
truth["outburst"] = [float(g["ra"][cand]), float(g["dec"][cand]), 12.5, float(v[cand])]

img = rng.poisson(np.clip(img, 0, None)).astype(np.float32) + rng.normal(0, 5, img.shape).astype(np.float32)
img = np.clip(img, 0, 65535)
# hot pixels
for _ in range(40):
    img[rng.integers(0, H), rng.integers(0, W)] = 60000
hdr = fits.Header()
hdr["DATE-OBS"] = DATE
hdr["EXPTIME"] = 600.0
hdr["INSTRUME"] = "Seestar S50"
hdr["FOCALLEN"] = 250.0
hdr["XPIXSZ"] = 2.9
hdr["RA"] = ra0 + 0.3
hdr["DEC"] = dec0 - 0.2
stem = "seestar_asteroid" if asteroid else "seestar_sscyg"
fits.PrimaryHDU(img.astype(np.uint16), header=hdr).writeto(out / f"{stem}.fits", overwrite=True)
# JPEG version (gamma, 8 bit) like a Seestar/phone export
lin = np.clip((img - 700) / 30000, 0, 1)
srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
Image.fromarray((srgb * 255).astype(np.uint8)).convert("RGB").save(out / f"{stem}.jpg", quality=95)
(out / f"{stem}_truth.json").write_text(json.dumps(truth, indent=1))
print("stars rendered", int(inside.sum()), "truth", truth)
