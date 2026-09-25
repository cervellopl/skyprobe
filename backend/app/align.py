"""Reprojecting one image onto the pixel grid of another.

This is the shared machinery behind blinking two images without anything shifting: a survey
resampled onto our grid (dss.py), one of the user's own analysed images resampled onto
another's grid (main.py's /align.jpg, for blinking two of your own exposures), and the
full-frame alignment that stacking does before co-adding several exposures of the same field.

Same trick every time: walk the destination pixel grid, convert to sky coordinates with the
destination's WCS, convert those back to pixel coordinates with the source's WCS, and bilinearly
resample. Centre, scale, rotation and parity end up identical because both images are expressed
in the same sky frame, whatever the two cameras did.
"""
from __future__ import annotations

import numpy as np
from astropy.wcs import WCS


def _reproject(px_x: np.ndarray, px_y: np.ndarray, dst_wcs: WCS, src_wcs: WCS,
               src_data: np.ndarray, src_scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    from scipy.ndimage import map_coordinates

    ra, dec = dst_wcs.all_pix2world(px_x.ravel(), px_y.ravel(), 0)
    sx, sy = src_wcs.all_world2pix(ra, dec, 0)
    if src_scale != 1.0:
        sx, sy = sx * src_scale, sy * src_scale
    coords = np.vstack([sy, sx])
    out = map_coordinates(np.nan_to_num(src_data.astype(np.float32)), coords, order=1,
                          mode="constant", cval=np.nan)
    return out.reshape(px_x.shape), ra.reshape(px_x.shape)


def resample(dst_wcs: WCS, x: float, y: float, size: float, out_px: int,
             src_wcs: WCS, src_data: np.ndarray, src_scale: float = 1.0) -> np.ndarray:
    """The `out_px`x`out_px` patch of `src_data` that lands on a close-up of `dst_wcs` around
    pixel (x, y), `size` original pixels wide - exactly the geometry /cutout.jpg uses, so a
    cutout of one image and a resample of another are pixel-for-pixel comparable.

    `src_scale` rescales the *source*'s own pixel coordinates after the sky lookup, for when
    `src_data` is a display rendering (e.g. a stretched preview) at a different pixel scale
    than the one `src_wcs` was solved on.
    """
    step = size / out_px
    gx = x - size / 2 + (np.arange(out_px) + 0.5) * step
    gy = y - size / 2 + (np.arange(out_px) + 0.5) * step
    ux, uy = np.meshgrid(gx, gy)
    out, ra = _reproject(ux, uy, dst_wcs, src_wcs, src_data, src_scale)
    if not np.isfinite(ra).any():
        raise ValueError("this part of the image has no valid sky coordinates")
    return out


def resample_full(dst_wcs: WCS, width: int, height: int, src_wcs: WCS, src_data: np.ndarray,
                  src_scale: float = 1.0) -> np.ndarray:
    """`src_data` reprojected onto every pixel of a `width`x`height` frame solved as `dst_wcs`.

    Used to align frames onto a common grid before stacking - the full-frame equivalent of
    `resample`, without the small-patch geometry or the off-sky check (a whole solved frame
    is on the sky by construction).
    """
    yy, xx = np.mgrid[0:height, 0:width]
    out, _ = _reproject(xx.astype(np.float64), yy.astype(np.float64), dst_wcs, src_wcs, src_data, src_scale)
    return out
