"""Render a synthetic Bayer DNG star field (for testing the camera-RAW path).

  python tests/make_dng.py OUTDIR [linear]

"linear" writes a demosaiced ("linear DNG") file instead, like Apple ProRAW.
"""
import sys
from pathlib import Path

import numpy as np
import tifffile
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalogs import band_mag, reference_stars  # noqa: E402

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
linear = len(sys.argv) > 2 and sys.argv[2] == "linear"
W, H, scale = 2000, 1500, 8.0          # ~4.4 x 3.3 deg, like a phone tele lens
ra0, dec0, rot = 84.0, -2.0, 12.0      # Orion's belt
w = WCS(naxis=2); w.wcs.ctype = ["RA---TAN", "DEC--TAN"]; w.wcs.crval = [ra0, dec0]
w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]
th, s = np.radians(rot), scale / 3600
w.wcs.cd = [[-s * np.cos(th), s * np.sin(th)], [s * np.sin(th), s * np.cos(th)]]

rad = np.hypot(W, H) / 2 * scale / 3600 * 1.05
maglim = 12.0
cat = reference_stars(SkyCoord(ra0, dec0, unit="deg"), rad, maglim, 2025.7)
x, y = w.world_to_pixel(SkyCoord(cat["ra"], cat["dec"], unit="deg"))
v = band_mag(cat, "V")

BLACK, WHITE, ZP = 512.0, 4095.0, 17.0
sig = 1.6 / 2.355
img = np.full((H, W), 40.0)            # sky above black level
yy, xx = np.mgrid[0:H, 0:W]
for xc, yc, m in zip(x, y, v):
    if not (6 < xc < W - 7 and 6 < yc < H - 7) or not np.isfinite(m):
        continue
    xi, yi = int(xc), int(yc)
    sl = (slice(yi - 6, yi + 7), slice(xi - 6, xi + 7))
    f = 10 ** (-0.4 * (m - ZP))
    img[sl] += f / (2 * np.pi * sig ** 2) * np.exp(-((xx[sl] - xc) ** 2 + (yy[sl] - yc) ** 2) / (2 * sig ** 2))
# an uncatalogued star, to be found by the transient search
nx, ny = 1400.4, 420.6
img[int(ny) - 6:int(ny) + 7, int(nx) - 6:int(nx) + 7] += (
    10 ** (-0.4 * (10.5 - ZP)) / (2 * np.pi * sig ** 2)
    * np.exp(-((xx[int(ny) - 6:int(ny) + 7, int(nx) - 6:int(nx) + 7] - nx) ** 2
               + (yy[int(ny) - 6:int(ny) + 7, int(nx) - 6:int(nx) + 7] - ny) ** 2) / (2 * sig ** 2)))
rng = np.random.default_rng(7)
img = rng.poisson(np.clip(img, 0, None)).astype(np.float64)

# Bayer RGGB: red and blue photosites see a fraction of the (green-calibrated) flux
planes = {"R": img * 0.55, "G": img, "B": img * 0.45}
if linear:
    data = np.dstack([planes["R"], planes["G"], planes["B"]])
else:
    data = np.zeros((H, W))
    data[0::2, 0::2] = planes["R"][0::2, 0::2]
    data[0::2, 1::2] = planes["G"][0::2, 1::2]
    data[1::2, 0::2] = planes["G"][1::2, 0::2]
    data[1::2, 1::2] = planes["B"][1::2, 1::2]
data = np.clip(data + BLACK, 0, WHITE).astype(np.uint16)

extra = [(50706, "B", 4, (1, 4, 0, 0), False),                 # DNGVersion
         (50708, "s", 0, "SkyProbe synthetic camera", False),  # UniqueCameraModel
         (50714, "I", 1, (int(BLACK),), False),                # BlackLevel
         (50717, "I", 1, (int(WHITE),), False),                # WhiteLevel
         (50721, "2i", 9, (1, 1) * 9, False)]                  # ColorMatrix1
if linear:
    extra += [(50710, "B", 3, (0, 1, 2), False), (50719, "I", 2, (W, H), False),
              (50718, "2i", 2, (1, 1, 1, 1), False), (50728, "2i", 3, (1, 1, 1, 1, 1, 1), False)]
else:
    extra += [(33421, "H", 2, (2, 2), False), (33422, "B", 4, (0, 1, 1, 2), False),
              (50711, "H", 1, (1,), False)]

name = out / ("phone_linear.dng" if linear else "phone_bayer.dng")
tifffile.imwrite(name, data, photometric=34892 if linear else 32803,
                 planarconfig="contig" if linear else None, compression=None,
                 extratags=extra, subfiletype=0)
print(name, data.shape, "stars:", int(np.isfinite(v).sum()),
      "injected new star at", [float(c) for c in w.pixel_to_world_values(nx, ny)], "mag 10.5")
