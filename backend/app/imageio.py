"""Loading of phone / Seestar images (JPEG, HEIC, camera RAW, FITS).

Every loader returns an ObsImage holding:
  * ``data``  - 2-D float32 array used for detection and photometry. Whenever
                possible this is the *linear* green channel (AAVSO "TG" band),
                kept at full sensor resolution so pixel geometry is preserved.
  * ``preview`` - 8-bit RGB (or grey) array for display, any size.
  * ``meta``  - observation metadata (time, exposure, optics, location, hints).
"""
from __future__ import annotations

import io
import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pillow_heif = None

RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2",
           ".raf", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f", ".iiq", ".rwl"}
FITS_EXT = {".fits", ".fit", ".fts", ".fz"}
JPEG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp"}

# Pixel scale presets (arcsec / pixel)
DEVICE_PRESETS = {
    "seestar_s50": {"scale": 2.39, "focal_mm": 250.0, "pixel_um": 2.9, "label": "ZWO Seestar S50"},
    "seestar_s30": {"scale": 3.99, "focal_mm": 150.0, "pixel_um": 2.9, "label": "ZWO Seestar S30"},
}


@dataclass
class ObsImage:
    data: np.ndarray
    preview: np.ndarray
    fmt: str
    linear: bool
    saturation: float
    band: str = "TG"
    meta: dict = field(default_factory=dict)
    header_wcs: object = None
    warnings: list = field(default_factory=list)

    @property
    def shape(self):
        return self.data.shape


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def srgb_to_linear(a: np.ndarray) -> np.ndarray:
    a = np.clip(a, 0.0, 1.0)
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4).astype(np.float32)


def interpolate_green(mosaic: np.ndarray, green_mask: np.ndarray) -> np.ndarray:
    """Full resolution green plane from a Bayer mosaic.

    Non-green pixels get the mean of their four (green) neighbours, so pixel
    geometry is unchanged and the result stays linear.
    """
    m = mosaic.astype(np.float32)
    g = np.where(green_mask, m, 0.0).astype(np.float32)
    p = np.pad(g, 1, mode="reflect")
    pm = np.pad(green_mask.astype(np.float32), 1, mode="reflect")
    s = p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
    n = pm[:-2, 1:-1] + pm[2:, 1:-1] + pm[1:-1, :-2] + pm[1:-1, 2:]
    fill = s / np.maximum(n, 1)
    return np.where(green_mask, m, fill).astype(np.float32)


def auto_stretch(img: np.ndarray) -> np.ndarray:
    """Asinh/midtone stretch of linear data to 8-bit for display."""
    a = img.astype(np.float32)

    def _one(x):
        finite = x[np.isfinite(x)]
        if finite.size == 0:
            return np.zeros_like(x, dtype=np.uint8)
        sample = finite[:: max(1, finite.size // 200000)]
        med = np.median(sample)
        mad = np.median(np.abs(sample - med)) * 1.4826 + 1e-12
        lo = med - 2.5 * mad
        hi = np.percentile(sample, 99.95)
        if hi <= lo:
            hi = lo + 1
        y = np.clip((x - lo) / (hi - lo), 0, 1)
        # midtone transfer so the background sits around 0.12
        bg = np.clip((med - lo) / (hi - lo), 1e-6, 0.99)
        target = 0.12
        mtf = (target - 1) * bg / ((2 * target - 1) * bg - target)
        y = ((mtf - 1) * y) / ((2 * mtf - 1) * y - mtf)
        return (np.nan_to_num(np.clip(y, 0, 1)) * 255).astype(np.uint8)

    if a.ndim == 3:
        # per-channel stretch also neutralises the sky background colour
        return np.dstack([_one(a[..., i]) for i in range(a.shape[2])])
    return _one(a)


def downscale_preview(arr: np.ndarray, max_side: int = 2048) -> np.ndarray:
    im = Image.fromarray(arr)
    w, h = im.size
    s = max(w, h) / max_side
    if s > 1:
        im = im.resize((round(w / s), round(h / s)), Image.LANCZOS)
    return np.asarray(im)


def _ratio(v):
    try:
        return float(v)
    except Exception:
        try:
            return float(v.num) / float(v.den)
        except Exception:
            return None


def _gps_to_deg(vals, ref):
    try:
        d, m, s = [_ratio(x) for x in vals]
        deg = d + m / 60 + s / 3600
        if str(ref).strip().upper() in ("S", "W"):
            deg = -deg
        return deg
    except Exception:
        return None


def _parse_exif_datetime(dt: str | None, offset: str | None, subsec: str | None):
    if not dt:
        return None, None
    try:
        t = datetime.strptime(str(dt).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, None
    if subsec:
        try:
            t += timedelta(seconds=float("0." + str(subsec).strip()))
        except ValueError:
            pass
    if offset:
        off = str(offset).strip()
        try:
            sign = -1 if off.startswith("-") else 1
            hh, mm = off.lstrip("+-").split(":")
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
            return t.replace(tzinfo=tz).astimezone(timezone.utc), "exif+offset"
        except Exception:
            pass
    return t.replace(tzinfo=timezone.utc), "exif-local-assumed-utc"


def _exif_from_pillow(im: Image.Image) -> dict:
    out = {}
    try:
        ex = im.getexif()
    except Exception:
        return out
    if not ex:
        return out
    base = dict(ex)
    try:
        sub = dict(ex.get_ifd(0x8769))
    except Exception:
        sub = {}
    try:
        gps = dict(ex.get_ifd(0x8825))
    except Exception:
        gps = {}
    out["make"] = str(base.get(0x010F, "")).strip("\x00 ")
    out["model"] = str(base.get(0x0110, "")).strip("\x00 ")
    dt = sub.get(0x9003) or base.get(0x0132)
    out["_dt"] = dt
    out["_offset"] = sub.get(0x9011) or sub.get(0x9010)
    out["_subsec"] = sub.get(0x9291)
    out["exptime"] = _ratio(sub.get(0x829A)) if sub.get(0x829A) is not None else None
    out["iso"] = sub.get(0x8827)
    out["focal_mm"] = _ratio(sub.get(0x920A)) if sub.get(0x920A) is not None else None
    out["focal35_mm"] = _ratio(sub.get(0xA405)) if sub.get(0xA405) is not None else None
    out["fnumber"] = _ratio(sub.get(0x829D)) if sub.get(0x829D) is not None else None
    if gps.get(2) and gps.get(4):
        out["lat"] = _gps_to_deg(gps[2], gps.get(1, "N"))
        out["lon"] = _gps_to_deg(gps[4], gps.get(3, "E"))
        if gps.get(6) is not None:
            out["elevation_m"] = _ratio(gps[6])
    return out


def _exif_from_exifread(path: Path) -> dict:
    out = {}
    try:
        import exifread

        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return out

    def g(k):
        v = tags.get(k)
        return None if v is None else v.values

    def first_ratio(k):
        v = g(k)
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return _ratio(v)

    out["make"] = str(tags.get("Image Make", "")).strip()
    out["model"] = str(tags.get("Image Model", "")).strip()
    out["_dt"] = str(tags.get("EXIF DateTimeOriginal", "") or tags.get("Image DateTime", "")) or None
    out["_offset"] = str(tags.get("EXIF OffsetTimeOriginal", "")) or None
    out["_subsec"] = str(tags.get("EXIF SubSecTimeOriginal", "")) or None
    out["exptime"] = first_ratio("EXIF ExposureTime")
    out["focal_mm"] = first_ratio("EXIF FocalLength")
    out["focal35_mm"] = first_ratio("EXIF FocalLengthIn35mmFilm")
    out["fnumber"] = first_ratio("EXIF FNumber")
    iso = g("EXIF ISOSpeedRatings")
    out["iso"] = iso[0] if isinstance(iso, list) and iso else iso
    if g("GPS GPSLatitude") and g("GPS GPSLongitude"):
        out["lat"] = _gps_to_deg(g("GPS GPSLatitude"), tags.get("GPS GPSLatitudeRef", "N"))
        out["lon"] = _gps_to_deg(g("GPS GPSLongitude"), tags.get("GPS GPSLongitudeRef", "E"))
    return out


def _as_int(v):
    """EXIF hands ISO over as an int, a string or a one-element list depending on the reader."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finish_exif(meta: dict, exif: dict, width: int, height: int, warn: list):
    t, src = _parse_exif_datetime(exif.pop("_dt", None), exif.pop("_offset", None), exif.pop("_subsec", None))
    exif["iso"] = _as_int(exif.get("iso"))
    meta.update({k: v for k, v in exif.items() if v not in (None, "")})
    if t is not None:
        meta["date_obs"] = t.isoformat()
        meta["date_source"] = src
        if src == "exif-local-assumed-utc":
            warn.append("EXIF time has no timezone offset - assumed UTC. Pass utc_offset or obs_time to fix.")
    # scale hint from 35mm-equivalent focal length (36 mm across the long side)
    f35 = meta.get("focal35_mm")
    if f35:
        meta["scale_hint"] = 206265.0 * 36.0 / (f35 * max(width, height))
    model = f"{meta.get('make', '')} {meta.get('model', '')}".lower()
    if "seestar" in model:
        meta["device"] = "seestar_s30" if "s30" in model else "seestar_s50"


# ----------------------------------------------------------------------------
# loaders
# ----------------------------------------------------------------------------

def load_raster(path: Path) -> ObsImage:
    warn = []
    im = Image.open(path)
    exif = _exif_from_pillow(im)
    im = ImageOps.exif_transpose(im)
    bits = 8
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        arr = np.asarray(im).astype(np.float32)
        bits = 16
        rgb = None
        scale = 65535.0
    else:
        im = im.convert("RGB")
        rgb = np.asarray(im)
        arr = rgb.astype(np.float32)
        scale = 255.0
    if arr.ndim == 3:
        lin = srgb_to_linear(arr / scale)
        data = lin[..., 1]
        preview = rgb
    else:
        data = srgb_to_linear(arr / scale)
        preview = auto_stretch(data)
    h, w = data.shape
    meta = {"width": w, "height": h, "bit_depth": bits}
    _finish_exif(meta, exif, w, h, warn)
    warn.append("Compressed/processed image (JPEG/HEIC): data linearised with inverse sRGB curve; "
                "photometry is approximate (phone tone-mapping, noise reduction, 8-bit quantisation).")
    return ObsImage(data=np.ascontiguousarray(data * 65535.0, dtype=np.float32),
                    preview=downscale_preview(preview), fmt=path.suffix.lower().lstrip("."),
                    linear=False, saturation=0.97 * 65535.0, band="TG", meta=meta, warnings=warn)


def _green_via_libraw(raw) -> tuple[np.ndarray, float]:
    """Fallback for layouts we cannot read directly (Foveon, odd DNGs).

    LibRaw demosaics, but with unit white balance, no auto brightness and gamma 1,
    so the green plane it returns is still linear.
    """
    import rawpy

    rgb = raw.postprocess(output_bps=16, gamma=(1, 1), no_auto_bright=True, user_wb=[1.0, 1.0, 1.0, 1.0],
                          user_flip=0, output_color=rawpy.ColorSpace.raw, no_auto_scale=False)
    return rgb[..., 1].astype(np.float32), 0.95 * 65535.0


CFA_PHOTOMETRIC, LINEAR_RAW_PHOTOMETRIC = 32803, 34892


def _pick_raw_page(tif):
    """The largest CFA / LinearRaw image in a DNG, including the ones hidden in SubIFDs."""
    found = []

    def walk(pages):
        for pg in pages:
            try:
                ph = int(pg.photometric)
            except Exception:
                continue
            if ph in (CFA_PHOTOMETRIC, LINEAR_RAW_PHOTOMETRIC):
                found.append(pg)
            sub = getattr(pg, "pages", None)
            if sub:
                walk(sub)

    walk(tif.pages)
    if not found:
        return None
    return max(found, key=lambda p: int(np.prod(p.shape[:2])))


def _white_level(tag_value, data_max: float) -> float:
    """DNGs often declare 65535 while storing 10- or 12-bit data; believe the smaller one."""
    declared = float(np.max(tag_value)) if tag_value is not None else 65535.0
    for bits in (1023.0, 4095.0, 16383.0, 65535.0):
        if data_max <= bits:
            return min(declared, bits)
    return declared


def load_dng_tifffile(path: Path) -> ObsImage:
    """DNG variants LibRaw cannot open - notably DNG 1.7 with JPEG XL compression,
    as written by recent Samsung Galaxy phones."""
    import tifffile

    warn = []
    with tifffile.TiffFile(path) as tif:
        pg = _pick_raw_page(tif)
        if pg is None:
            raise ValueError("No raw image inside this DNG")
        photometric = int(pg.photometric)
        compression = str(getattr(pg, "compression", ""))
        tags = {k: (pg.tags[k].value if k in pg.tags else None)
                for k in ("BlackLevel", "WhiteLevel", "CFAPattern", "CFARepeatPatternDim", "Make", "Model")}
        arr = pg.asarray()

    arr = arr.astype(np.float32)
    black = float(np.mean(np.asarray(tags["BlackLevel"], dtype=float))) if tags["BlackLevel"] is not None else 0.0
    sat = _white_level(tags["WhiteLevel"], float(arr.max())) - black
    if photometric == LINEAR_RAW_PHOTOMETRIC or arr.ndim == 3:
        layout = "linear-dng"
        data = (arr[..., 1] if arr.ndim == 3 else arr) - black
        warn.append("Linear DNG: the camera already demosaiced this file, so the green plane is "
                    "interpolated data rather than raw photosite values.")
    else:
        layout = "bayer"
        pattern = np.asarray(tags["CFAPattern"] or [0, 1, 1, 2], dtype=int).ravel()
        dim = tuple(tags["CFARepeatPatternDim"] or (2, 2))
        pattern = pattern[: dim[0] * dim[1]].reshape(dim)          # 0=R, 1=G, 2=B
        yy, xx = np.indices(arr.shape)
        green_mask = pattern[yy % dim[0], xx % dim[1]] == 1
        data = interpolate_green(arr - black, green_mask)
    warn.append(f"Read with tifffile ({compression} compression); LibRaw does not support this DNG flavour.")

    h, w = data.shape
    meta = {"width": w, "height": h, "raw_layout": layout, "raw_reader": "tifffile",
            "raw_compression": compression}
    _finish_exif(meta, _exif_from_exifread(path), w, h, warn)
    return ObsImage(data=np.ascontiguousarray(data, dtype=np.float32),
                    preview=downscale_preview(auto_stretch(data)), fmt=path.suffix.lower().lstrip("."),
                    linear=True, saturation=0.95 * sat, band="TG", meta=meta, warnings=warn)


def load_raw(path: Path) -> ObsImage:
    """LibRaw first; DNG flavours it rejects (e.g. JPEG XL) go through tifffile."""
    try:
        return _load_raw_libraw(path)
    except Exception as e:
        if path.suffix.lower() in (".dng", ".tif", ".tiff"):
            return load_dng_tifffile(path)
        raise ValueError(f"LibRaw cannot read this file: {e}") from e


def _load_raw_libraw(path: Path) -> ObsImage:
    import rawpy

    warn = []
    with rawpy.imread(str(path)) as raw:
        desc = raw.color_desc.decode(errors="ignore")
        black = np.array(raw.black_level_per_channel, dtype=np.float32)
        white = float(raw.white_level)
        green_idx = [i for i, c in enumerate(desc) if c == "G"]
        raw_visible = raw.raw_image_visible
        layout = "bayer"

        if raw.raw_type == rawpy.RawType.Stack or raw_visible.ndim == 3:
            # "linear DNG": already demosaiced by the camera (Apple ProRAW, some Android
            # computational RAW). Take the green plane - still linear, still full resolution.
            layout = "linear-dng"
            g = green_idx[0] if green_idx else 1
            data = raw_visible[..., min(g, raw_visible.shape[2] - 1)].astype(np.float32)
            data = data - float(black[min(g, len(black) - 1)])
            sat = white - float(black.max())
            warn.append("Linear DNG: the camera already demosaiced this file, so the green plane is "
                        "interpolated data rather than raw photosite values.")
        else:
            colors = raw.raw_colors_visible
            mosaic = raw_visible.astype(np.float32) - black[colors]  # per-pixel black level
            green_mask = np.isin(colors, green_idx)
            if green_mask.mean() >= 0.2:
                data = interpolate_green(mosaic, green_mask)
                sat = white - float(black.max())
            else:
                layout = "libraw-demosaic"
                data, sat = _green_via_libraw(raw)
                warn.append("Unusual RAW layout: LibRaw demosaiced the frame for us.")
        try:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False,
                                  output_bps=8, user_flip=0, gamma=(2.222, 4.5))
        except Exception:
            rgb = auto_stretch(data)
    h, w = data.shape
    meta = {"width": w, "height": h, "raw_pattern": desc, "raw_layout": layout}
    _finish_exif(meta, _exif_from_exifread(path), w, h, warn)
    # EXIF of raw files is in the orientation of the sensor; we keep sensor geometry (no rotation).
    return ObsImage(data=np.ascontiguousarray(data), preview=downscale_preview(rgb),
                    fmt=path.suffix.lower().lstrip(".") or "raw",
                    linear=True, saturation=0.95 * sat, band="TG", meta=meta, warnings=warn)


def _sexa_to_deg(s: str, hours: bool) -> float | None:
    try:
        from astropy.coordinates import Angle
        import astropy.units as u

        return float(Angle(str(s).strip(), unit=u.hourangle if hours else u.deg).deg)
    except Exception:
        return None


def load_fits(path: Path) -> ObsImage:
    from astropy.io import fits
    from astropy.wcs import WCS, FITSFixedWarning

    warn = []
    with fits.open(path, memmap=False) as hdul:
        hdu = next((h for h in hdul if h.data is not None and h.data.ndim in (2, 3)), None)
        if hdu is None:
            raise ValueError("No image data in FITS file")
        hdr = hdu.header.copy()
        raw = hdu.data
        arr = raw.astype(np.float32)

    preview_rgb = None
    bayer = str(hdr.get("BAYERPAT", "") or hdr.get("COLORTYP", "")).strip().upper()
    if arr.ndim == 3:
        if arr.shape[0] == 3:
            preview_rgb = np.moveaxis(arr, 0, -1)
            data = arr[1]
        elif arr.shape[-1] == 3:
            preview_rgb = arr
            data = arr[..., 1]
        else:
            data = arr[0]
        band = "TG"
    elif bayer in ("RGGB", "BGGR", "GRBG", "GBRG"):
        green_even = bayer in ("GRBG", "GBRG")  # green at (0,0)
        h = arr.shape[0]
        if str(hdr.get("ROWORDER", "")).upper() == "BOTTOM-UP" and h % 2 == 0:
            green_even = not green_even
        yy, xx = np.indices(arr.shape)
        green_mask = ((yy + xx) % 2 == 0) if green_even else ((yy + xx) % 2 == 1)
        data = interpolate_green(arr, green_mask)
        band = "TG"
    else:
        data = arr
        filt = str(hdr.get("FILTER", "")).strip().upper()
        band = {"V": "V", "B": "B", "R": "R", "I": "I", "G": "TG", "L": "CV", "CLEAR": "CV", "": "CV"}.get(filt, "CV")

    data = np.nan_to_num(data, nan=float(np.nanmedian(data)))
    if "SATURATE" in hdr:
        sat = float(hdr["SATURATE"])
    else:
        # unknown full-well: treat anything close to the image maximum as clipped
        sat = 0.95 * float(np.nanmax(data))

    h, w = data.shape
    meta = {"width": w, "height": h}
    for key in ("INSTRUME", "TELESCOP", "OBJECT", "FILTER"):
        if key in hdr:
            meta[key.lower()] = str(hdr[key]).strip()
    exp = hdr.get("EXPTIME", hdr.get("EXPOSURE"))
    if exp is not None:
        meta["exptime"] = float(exp)
    if "STACKCNT" in hdr:
        meta["stack_count"] = int(hdr["STACKCNT"])
    dobs = hdr.get("DATE-OBS") or hdr.get("DATE_OBS")
    if dobs:
        try:
            from astropy.time import Time

            t = Time(str(dobs).replace("Z", ""), format="isot", scale="utc")
            meta["date_obs"] = t.to_datetime(timezone=timezone.utc).isoformat()
            meta["date_source"] = "fits"
        except Exception:
            warn.append(f"Could not parse DATE-OBS={dobs}")
    for lk, ok in (("SITELAT", "lat"), ("SITELONG", "lon"), ("OBSGEO-B", "lat"), ("OBSGEO-L", "lon")):
        if lk in hdr and ok not in meta:
            v = hdr[lk]
            meta[ok] = float(v) if isinstance(v, (int, float)) else _sexa_to_deg(v, False)
    ra = hdr.get("RA", hdr.get("CRVAL1") if "RA" in str(hdr.get("CTYPE1", "")) else None)
    dec = hdr.get("DEC", hdr.get("CRVAL2") if "DEC" in str(hdr.get("CTYPE2", "")) else None)
    if isinstance(ra, str):
        ra = _sexa_to_deg(ra, True)
    if isinstance(dec, str):
        dec = _sexa_to_deg(dec, False)
    if ra is None and "OBJCTRA" in hdr:
        ra = _sexa_to_deg(hdr["OBJCTRA"], True)
        dec = _sexa_to_deg(hdr.get("OBJCTDEC", ""), False)
    if ra is not None and dec is not None:
        meta["ra_hint"], meta["dec_hint"] = float(ra), float(dec)
    focal = hdr.get("FOCALLEN")
    pix = hdr.get("XPIXSZ") or hdr.get("PIXSIZE1")
    if focal and pix:
        binning = float(hdr.get("XBINNING", 1) or 1)
        meta["focal_mm"], meta["pixel_um"] = float(focal), float(pix)
        meta["scale_hint"] = 206.265 * float(pix) * binning / float(focal)
    ident = f"{meta.get('instrume', '')} {meta.get('telescop', '')}".lower()
    if "seestar" in ident:
        meta["device"] = "seestar_s30" if "s30" in ident else "seestar_s50"

    header_wcs = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        try:
            wcs = WCS(hdr, naxis=2)
            if wcs.has_celestial and (wcs.wcs.has_cd() or wcs.wcs.has_pc() or np.any(wcs.wcs.cdelt != 1)):
                if wcs.wcs.ctype[0].startswith("RA") and wcs.pixel_scale_matrix.any():
                    header_wcs = wcs.celestial
        except Exception:
            header_wcs = None

    if preview_rgb is not None:
        preview = auto_stretch(preview_rgb)
    else:
        preview = auto_stretch(data)
    # FITS images are displayed bottom-up; we keep array order everywhere (row 0 = FITS y=1)
    return ObsImage(data=np.ascontiguousarray(data, dtype=np.float32), preview=downscale_preview(preview),
                    fmt="fits", linear=True, saturation=sat, band=band, meta=meta,
                    header_wcs=header_wcs, warnings=warn)


def load_image(path: str | Path) -> ObsImage:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in FITS_EXT:
        img = load_fits(path)
    elif ext in RAW_EXT:
        img = load_raw(path)
    elif ext in JPEG_EXT:
        img = load_raster(path)
    else:
        # sniff
        head = path.read_bytes()[:16]
        if head.startswith(b"SIMPLE"):
            img = load_fits(path)
        else:
            try:
                img = load_raster(path)
            except Exception:
                img = load_raw(path)
    if img.meta.get("device") in DEVICE_PRESETS and "scale_hint" not in img.meta:
        img.meta["scale_hint"] = DEVICE_PRESETS[img.meta["device"]]["scale"]
    return img
