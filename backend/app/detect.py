"""Background estimation and source extraction (SEP / SExtractor algorithms)."""
from __future__ import annotations

import numpy as np
import sep


def sky_mask(data: np.ndarray, block: int = 32) -> np.ndarray:
    """True where the pixel looks like sky, False over a lit foreground.

    Landscape astrophotos from a phone usually contain buildings, trees or the
    ground. Their edges and textures are extracted as thousands of "sources",
    which wrecks plate solving and produces false transients.

    A block of empty sky is smooth: its median absolute deviation is a couple of
    ADU. Foreground is textured (walls, windows, leaves) and usually brighter.
    Since the sky itself has a strong light-pollution gradient, texture is the
    primary test and brightness only a secondary one; isolated blocks are then
    dropped, because real foreground comes in large connected pieces.
    """
    from scipy import ndimage

    h, w = data.shape
    by, bx = max(1, h // block), max(1, w // block)
    if by < 4 or bx < 4:
        return np.ones((h, w), bool)
    ys = np.linspace(0, h, by + 1).astype(int)
    xs = np.linspace(0, w, bx + 1).astype(int)
    level = np.empty((by, bx), np.float32)
    spread = np.empty((by, bx), np.float32)
    for i in range(by):
        for j in range(bx):
            tile = data[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            med = np.median(tile)
            level[i, j] = med
            spread[i, j] = np.median(np.abs(tile - med))

    sky_spread = float(np.median(spread))
    spread_mad = float(np.median(np.abs(spread - sky_spread))) * 1.4826 + 1e-6
    low = float(np.percentile(level, 25))
    low_sigma = float(np.percentile(level, 50) - np.percentile(level, 10)) + 1e-6
    bad = (spread > max(3.0 * sky_spread, sky_spread + 6 * spread_mad)) | (level > low + 10 * low_sigma)

    labels, n = ndimage.label(bad)
    if n:
        sizes = ndimage.sum(bad, labels, range(1, n + 1))
        big = [i + 1 for i, s in enumerate(sizes) if s >= 0.004 * bad.size]
        bad = np.isin(labels, big) if big else np.zeros_like(bad)
        bad = ndimage.binary_fill_holes(bad)
        bad = ndimage.binary_dilation(bad, iterations=1)
    if bad.mean() > 0.9:           # almost everything flagged: trust nothing, keep all
        return np.ones((h, w), bool)
    mask = np.ones((h, w), bool)
    for i in range(by):
        for j in range(bx):
            if bad[i, j]:
                mask[ys[i]:ys[i + 1], xs[j]:xs[j + 1]] = False
    return mask


def detect_sources(data: np.ndarray, saturation: float, thresh: float = 5.0, minarea: int = 5,
                   mask_foreground: bool = True) -> dict:
    data = np.ascontiguousarray(data, dtype=np.float32)
    h, w = data.shape
    box = int(np.clip(min(h, w) / 24, 32, 128))
    bkg = sep.Background(data, bw=box, bh=box, fw=3, fh=3)
    sub = data - bkg.back()
    rms = bkg.rms()
    sep.set_extract_pixstack(10_000_000)
    sep.set_sub_object_limit(8192)
    objs = None
    t = thresh
    for _ in range(4):
        try:
            objs = sep.extract(sub, t, err=rms, minarea=minarea, deblend_cont=0.005, clean=True)
            break
        except Exception:  # deblending / pixel-stack overflow on crowded or noisy frames
            t *= 1.6
    if objs is None or len(objs) == 0:
        return {"n": 0, "sub": sub, "rms": rms, "bkg_rms": float(bkg.globalrms), "fwhm": None,
                "thresh": t, "objs": {}, "sky": np.ones_like(data, bool), "sky_fraction": 1.0}
    x, y = objs["x"], objs["y"]
    a = np.maximum(objs["a"], 0.3)
    rhalf, _ = sep.flux_radius(sub, x, y, np.clip(6.0 * a, 3, 60), 0.5, subpix=5)
    fwhm = 2.0 * rhalf
    peak = objs["peak"] + bkg.back()[np.clip(y.round().astype(int), 0, h - 1), np.clip(x.round().astype(int), 0, w - 1)]
    rms_at = rms[np.clip(y.round().astype(int), 0, h - 1), np.clip(x.round().astype(int), 0, w - 1)]
    snr = objs["flux"] / np.sqrt(np.maximum(objs["npix"], 1) * rms_at ** 2 + np.maximum(objs["flux"], 0) / 1.0 + 1e-12)
    saturated = peak >= saturation
    edge = (x < 5) | (y < 5) | (x > w - 6) | (y > h - 6)
    out = {
        "x": x.astype(float), "y": y.astype(float), "flux": objs["flux"].astype(float),
        "peak": peak.astype(float), "a": objs["a"].astype(float), "b": objs["b"].astype(float),
        "theta": objs["theta"].astype(float), "npix": objs["npix"].astype(int), "fwhm": fwhm.astype(float),
        "snr": snr.astype(float), "flag": objs["flag"].astype(int), "saturated": saturated, "edge": edge,
    }
    sky = sky_mask(data) if mask_foreground else np.ones_like(data, bool)
    out["on_sky"] = sky[np.clip(y.round().astype(int), 0, h - 1), np.clip(x.round().astype(int), 0, w - 1)]
    good = out["on_sky"] & (~saturated) & (~edge) & (out["flag"] == 0) & np.isfinite(fwhm) & (fwhm > 0.8) & (snr > 20)
    if good.sum() < 5:
        good = np.isfinite(fwhm) & (fwhm > 0.8) & ~edge
    stellar_fwhm = float(np.median(fwhm[good])) if good.any() else float(np.nanmedian(fwhm))
    return {"n": int(len(x)), "objs": out, "sub": sub, "rms": rms, "bkg_rms": float(bkg.globalrms),
            "bkg_level": float(bkg.globalback), "fwhm": stellar_fwhm, "thresh": t,
            "sky": sky, "sky_fraction": float(sky.mean()), "n_on_sky": int(out["on_sky"].sum())}
