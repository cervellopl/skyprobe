"""Synthetic wide-field phone JPEG with EXIF (focal length, time with offset) from Gaia stars."""
import sys
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalogs import band_mag, reference_stars  # noqa: E402

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
f35 = float(sys.argv[2]) if len(sys.argv) > 2 else 26.0
W, H = 4032, 3024
scale = 206265 * 36 / (f35 * W) * 1.07  # real phones differ a bit from nominal
ra0, dec0 = 305.0, 38.0  # Cygnus
w = WCS(naxis=2); w.wcs.ctype = ["RA---TAN", "DEC--TAN"]; w.wcs.crval = [ra0, dec0]
w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]; th = np.radians(-35); s = scale / 3600
w.wcs.cd = [[-s * np.cos(th), s * np.sin(th)], [s * np.sin(th), s * np.cos(th)]]
rad = np.hypot(W, H) / 2 * scale / 3600 * 1.05
maglim = 9.0 if f35 < 40 else 11.0
g = reference_stars(SkyCoord(ra0, dec0, unit="deg"), rad, maglim, 2025.7)
x, y = w.world_to_pixel(SkyCoord(g["ra"], g["dec"], unit="deg"))
v = band_mag(g, "V")
img = np.full((H, W), 0.02)
yy, xx = np.mgrid[0:H, 0:W]
img *= 1 + 0.6 * ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / (W / 2) ** 2  # sky gradient/vignetting
vig = 1 - 0.35 * ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / (W / 2) ** 2
ZP = maglim + 1.5
sig = 2.4 / 2.355
for xc, yc, m in zip(x, y, v):
    if not (5 < xc < W - 6 and 5 < yc < H - 6) or not np.isfinite(m):
        continue
    xi, yi = int(xc), int(yc)
    sl = (slice(yi - 5, yi + 6), slice(xi - 5, xi + 6))
    f = 10 ** (-0.4 * (m - ZP)) * vig[yi, xi]
    img[sl] += f / (2 * np.pi * sig ** 2) * np.exp(-((xx[sl] - xc) ** 2 + (yy[sl] - yc) ** 2) / (2 * sig ** 2))
rng = np.random.default_rng(3)
img = img + rng.normal(0, 0.003, img.shape)
lin = np.clip(img, 0, 1)
srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
rgb = (np.dstack([srgb * 0.95, srgb, srgb * 0.9]) * 255).astype(np.uint8)
im = Image.fromarray(rgb)
ex = Image.Exif()
ex[0x010F] = "Google"; ex[0x0110] = "Pixel 8"
sub = ex.get_ifd(0x8769)
sub[0x9003] = "2025:09:15 23:30:00"; sub[0x9011] = "+02:00"; sub[0x829A] = 4.0; sub[0xA405] = int(f35)
name = f"phone_{int(f35)}mm.jpg"
im.save(out / name, quality=92, exif=ex.tobytes())
print(name, "scale", scale, "stars", len(g))
