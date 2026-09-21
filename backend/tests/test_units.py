"""Offline unit tests: python -m pytest tests/test_units.py  (no network needed)."""
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import astrometry, photometry  # noqa: E402
from app.catalogs import band_mag, choose_mag_limit, gaia_to_band  # noqa: E402
from app.detect import detect_sources  # noqa: E402
from app.imageio import auto_stretch, interpolate_green, srgb_to_linear  # noqa: E402
from astropy.table import Table  # noqa: E402


def test_srgb_roundtrip():
    assert srgb_to_linear(np.array([0.0, 1.0]))[1] == 1.0
    assert abs(float(srgb_to_linear(np.array([0.5]))[0]) - 0.2140) < 1e-3


def test_interpolate_green_keeps_geometry_and_values():
    yy, xx = np.indices((8, 8))
    green = (yy + xx) % 2 == 0
    mosaic = np.where(green, 10.0, 999.0)
    out = interpolate_green(mosaic, green)
    assert out.shape == mosaic.shape
    assert np.allclose(out, 10.0)  # non-green pixels filled from green neighbours


def test_gaia_transform_matches_published_values():
    # Gaia DR3 documentation Table 5.9: G - V for BP-RP = 0 is +0.02704
    assert abs(float(gaia_to_band(np.array([10.0]), np.array([0.0]), "V")[0]) - 10.02704) < 1e-5
    t = Table({"g": [10.0], "bp_rp": [0.7]})
    t.meta["catalog"] = "tycho2"
    assert float(band_mag(t, "V")[0]) == 10.0  # Tycho V passes through


def test_mag_limit_shrinks_for_wide_fields():
    narrow = choose_mag_limit(2.4, 0.9, 40)
    wide = choose_mag_limit(70.0, 4000, 0)
    assert narrow > wide and 6 <= wide <= 12


def test_wcs_rescale_matches_full_resolution():
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [10.0, 20.0]
    w.wcs.crpix = [500.5, 400.5]
    w.wcs.cd = [[-1e-4, 0], [0, 1e-4]]
    full = w.pixel_to_world(123.0, 456.0)
    s = 2.0
    small = WCS(naxis=2)
    small.wcs.ctype = w.wcs.ctype
    small.wcs.crval = w.wcs.crval
    small.wcs.crpix = [(500.5 - 0.5) / s + 0.5, (400.5 - 0.5) / s + 0.5]
    small.wcs.cd = np.array(w.wcs.cd) * s
    back = WCS(astrometry.rescale_wcs_header(small.to_header(relax=True), s))
    got = back.pixel_to_world(123.0, 456.0)
    assert full.separation(got).arcsec < 1e-6


def test_detection_and_photometry_on_a_fake_frame():
    rng = np.random.default_rng(0)
    img = np.full((200, 200), 100.0)
    yy, xx = np.mgrid[0:200, 0:200]
    truth = [(50.0, 60.0, 20000.0), (120.0, 140.0, 5000.0), (160.0, 40.0, 1200.0)]
    sig = 2.0 / 2.355
    for x, y, flux in truth:
        img += flux / (2 * np.pi * sig ** 2) * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sig ** 2))
    img = rng.poisson(img).astype(np.float32)
    det = detect_sources(img, saturation=60000)
    assert det["n"] == len(truth)
    assert abs(det["fwhm"] - 2.0) < 0.6
    o = det["objs"]
    order = np.argsort(o["x"])
    assert np.allclose(np.sort(o["x"]), sorted(t[0] for t in truth), atol=0.3)
    r, r_in, r_out = photometry.aperture_radii(det["fwhm"])
    flux, ferr, peak, snr = photometry.measure(det["sub"], det["rms"], o["x"], o["y"], r, r_in, r_out, 60000, img)
    ratio = flux[order] / np.array([t[2] for t in sorted(truth)])
    assert np.all(ratio > 0.8) and np.all(ratio < 1.05)  # aperture catches most of the PSF
    assert snr.min() > 5


def test_aavso_report_format():
    variables = [{"name": "SS Cyg", "mag": 11.9, "err": 0.02, "upper_limit": False, "saturated": False,
                  "airmass": 1.2, "n_local_comps": 20, "check": {"name": "Gaia DR3 1", "measured": 12.5},
                  "blended": False}]
    txt = photometry.aavso_extended(variables, 2460000.5, "TG", "ABC")
    lines = txt.strip().splitlines()
    assert lines[1] == "#OBSCODE=ABC"
    row = lines[-1].split(",")
    assert row[0] == "SS Cyg" and row[4] == "TG" and row[7] == "ENSEMBLE" and row[11] == "1.200"


def test_calibration_recovers_zero_point():
    rng = np.random.default_rng(3)
    n = 120
    x = rng.uniform(20, 480, n)
    y = rng.uniform(20, 480, n)
    color = rng.uniform(0.2, 1.8, n)
    catmag = rng.uniform(10, 15, n)
    zp, k = 24.3, 0.05
    minst = catmag - zp - k * color + rng.normal(0, 0.01, n)
    resid = np.zeros(n)
    fitted_zp, fitted_k, mask, resid, b1, b2 = photometry._robust_fit(catmag - minst, color, use_color=True)
    assert b1 == 0 and b2 == 0      # linear data: no response curve fitted
    assert abs(fitted_zp - zp) < 0.02 and abs(fitted_k - k) < 0.02
    cal = photometry.Calibration(x, y, color, resid, fitted_zp, fitted_k, mask, "V")
    mag, err, nloc = cal.mag(minst[0], color[0], x[0], y[0], exclude=[0])
    assert abs(mag - catmag[0]) < 0.05 and nloc > 10


def test_robust_fit_recovers_a_nonlinear_response():
    """A tone-compressed phone RAW shows up as a fitted slope != 1."""
    rng = np.random.default_rng(5)
    n = 300
    catmag = rng.uniform(4.0, 9.0, n)
    color = rng.uniform(0.2, 1.6, n)
    zp, slope = 14.0, 0.25
    m0 = 6.5
    # camera compresses bright stars: measured instrumental mag is stretched around m0
    minst = (catmag - zp) - slope * (catmag - zp - (m0 - zp)) + rng.normal(0, 0.02, n)
    fit_zp, k, mask, resid, b1, b2 = photometry._robust_fit(
        catmag - minst, color, minst, use_color=True, use_nonlinearity=True)
    dm = minst - np.median(minst)
    model = minst + fit_zp + k * color + b1 * dm + b2 * dm ** 2
    assert abs(b1) > 0.1                       # the non-linearity was detected
    assert np.std(model - catmag) < 0.05       # and corrected


def test_auto_stretch_is_8bit():
    out = auto_stretch(np.random.default_rng(0).normal(1000, 30, (64, 64)))
    assert out.dtype == np.uint8 and out.shape == (64, 64)


def _write_dng(path, data, linear: bool, black=512, white=4095):
    """Minimal but valid DNG, as produced by phones (Bayer) or by ProRAW-style pipelines (linear)."""
    import tifffile

    extra = [(50706, "B", 4, (1, 4, 0, 0), False), (50708, "s", 0, "SkyProbe test", False),
             (50714, "I", 1, (black,), False), (50717, "I", 1, (white,), False),
             (50721, "2i", 9, (1, 1) * 9, False)]
    if linear:
        extra += [(50710, "B", 3, (0, 1, 2), False), (50719, "I", 2, (data.shape[1], data.shape[0]), False),
                  (50718, "2i", 2, (1, 1, 1, 1), False), (50728, "2i", 3, (1, 1, 1, 1, 1, 1), False)]
    else:
        extra += [(33421, "H", 2, (2, 2), False), (33422, "B", 4, (0, 1, 1, 2), False),
                  (50711, "H", 1, (1,), False)]
    tifffile.imwrite(path, data, photometric=34892 if linear else 32803,
                     planarconfig="contig" if linear else None, compression=None,
                     extratags=extra, subfiletype=0)


def _star_frame(h=64, w=96, black=512):
    img = np.full((h, w), 60.0)
    yy, xx = np.mgrid[0:h, 0:w]
    for (xc, yc, flux) in [(30.0, 20.0, 9000.0), (70.0, 44.0, 4000.0)]:
        img += flux / (2 * np.pi) * np.exp(-((xx - xc) ** 2 + (yy - yc) ** 2) / 2.0)
    return img


def test_load_bayer_dng(tmp_path):
    """Phone DNG: Bayer mosaic -> interpolated green plane at full resolution."""
    from app.imageio import load_image

    pytest.importorskip("tifffile")
    h, w, black = 64, 96, 512
    img = _star_frame(h, w)
    mosaic = np.zeros((h, w))
    mosaic[0::2, 0::2] = img[0::2, 0::2] * 0.55      # R
    mosaic[0::2, 1::2] = img[0::2, 1::2]             # G
    mosaic[1::2, 0::2] = img[1::2, 0::2]             # G
    mosaic[1::2, 1::2] = img[1::2, 1::2] * 0.45      # B
    p = tmp_path / "bayer.dng"
    _write_dng(p, np.clip(mosaic + black, 0, 4095).astype(np.uint16), linear=False)
    obs = load_image(p)
    assert obs.fmt == "dng" and obs.linear and obs.band == "TG"
    assert obs.data.shape == (h, w)                  # not binned: geometry preserved
    assert obs.meta["raw_layout"] == "bayer"
    # the brighter star is where we put it, and the green plane is not the red/blue level
    peak = np.unravel_index(np.argmax(obs.data), obs.data.shape)
    assert abs(peak[1] - 30) <= 1 and abs(peak[0] - 20) <= 1


def test_load_linear_dng(tmp_path):
    """Apple ProRAW-style DNG: already demosaiced; used to be rejected outright."""
    from app.imageio import load_image

    pytest.importorskip("tifffile")
    h, w, black = 64, 96, 512
    img = _star_frame(h, w)
    rgb = np.dstack([img * 0.55, img, img * 0.45])
    p = tmp_path / "linear.dng"
    _write_dng(p, np.clip(rgb + black, 0, 4095).astype(np.uint16), linear=True)
    obs = load_image(p)
    assert obs.fmt == "dng" and obs.linear
    assert obs.data.shape == (h, w)
    assert obs.meta["raw_layout"] == "linear-dng"
    assert any("Linear DNG" in x for x in obs.warnings)
    peak = np.unravel_index(np.argmax(obs.data), obs.data.shape)
    assert abs(peak[1] - 30) <= 1 and abs(peak[0] - 20) <= 1


def test_vsnet_object_names():
    from app.vsnet import vsnet_object

    assert vsnet_object("SS Cyg") == "CYGSS"          # the documented example
    assert vsnet_object("AG Dra") == "DRAAG"
    assert vsnet_object("V1500 Cyg") == "CYGV1500"
    # designations that are not GCVS-style keep their own form
    assert vsnet_object("ASASSN-V J020011.01+083956.0") == "ASASSN-V_J020011.01+083956.0"


def test_vsnet_date_and_lines():
    from app.vsnet import observation_line, ut_date

    assert ut_date(2451545.0) == "20000101.500"       # 2000 Jan 1.5 UT
    jd = 2451545.0
    measured = {"name": "SS Cyg", "mag": 11.83, "err": 0.02, "upper_limit": False}
    assert observation_line(measured, jd, "V", "Xyz") == "CYGSS 20000101.500 11.83V Xyz"
    limit = {"name": "SS Cyg", "mag": 14.2, "upper_limit": True}
    assert observation_line(limit, jd, "TG", "Xyz").split()[2] == ">14.2G"
    uncertain = {"name": "SS Cyg", "mag": 11.8, "err": 0.02, "blended": True, "upper_limit": False}
    assert observation_line(uncertain, jd, "CV", "Xyz").split()[2] == "11.80C:"
    assert observation_line({"name": "X Cyg", "mag": 5.0, "saturated": True}, jd, "V", "X") is None


def test_vsnet_report_selects_and_blocks():
    from app.vsnet import build_report

    result = {
        "time": {"jd_mid": 2451545.0, "utc_mid": "2000-01-01T12:00:00"},
        "calibration": {"band": "TG", "n_comps": 40, "zero_point": 20.0, "rms": 0.03,
                        "response_slope": 0.0, "catalog": "Gaia DR3 -> Johnson V"},
        "variables": [
            {"name": "SS Cyg", "mag": 11.8, "err": 0.02, "upper_limit": False},
            {"name": "Gaia DR3 12345", "mag": 12.0, "err": 0.02, "upper_limit": False},   # survey id
            {"name": "AG Dra", "mag": 13.0, "err": 0.5, "upper_limit": False},            # too noisy
            {"name": "RR Lyr", "mag": 15.0, "upper_limit": True},                         # limit
        ],
    }
    rep = build_report(result, observer="Xyz", site="Warsaw")
    assert rep["to"].startswith("vsnet-obs@")
    assert rep["n_observations"] == 1 and "CYGSS" in rep["body"]
    assert rep["selection"]["dropped_survey_id"] == 1 and rep["selection"]["dropped_error"] == 1
    assert not rep["blocked"]
    assert "Xyz" in rep["body"] and "Warsaw" in rep["body"]

    result["calibration"]["response_slope"] = 0.2       # tone-compressed phone raw
    assert build_report(result, observer="Xyz")["blocked"]

    result.pop("time")
    with pytest.raises(ValueError):
        build_report(result, observer="Xyz")


def test_pdf_report_is_generated(tmp_path):
    from app.report import build_pdf

    result = {
        "filename": "test.fits", "status": "done",
        "file": {"width": 100, "height": 80, "format": "fits", "band": "CV"},
        "time": {"jd_mid": 2451545.0, "utc_mid": "2000-01-01T12:00:00", "source": "fits"},
        "solution": {"ra": 10.0, "dec": 20.0, "ra_hms": "00:40:00", "dec_dms": "+20:00:00",
                     "pixel_scale": 2.4, "fov_w_deg": 0.5, "fov_h_deg": 0.4, "rotation_deg": 12.0,
                     "parity": "normal", "gal_l": 1.0, "gal_b": 2.0, "solver": "test", "solve_seconds": 1.0},
        "calibration": {"band": "CV", "catalog": "Gaia DR3", "n_comps": 30, "zero_point": 20.0, "rms": 0.02,
                        "color_term": 0.01, "limit_mag_5sigma": 17.0, "aperture_px": 4.0},
        "detections": {"count": 100, "fwhm_px": 3.0},
        "variables": [{"name": "SS Cyg", "type": "UGSS", "mag": 11.8, "err": 0.02, "upper_limit": False,
                       "max": 7.7, "min": 12.4, "min_is_amplitude": False, "period": None,
                       "airmass": 1.2, "flags": []}],
        "candidates": [{"status": "unidentified", "kind": "new_star", "label": "possible nova",
                        "mag": 12.0, "snr": 100.0, "fwhm_px": 3.1, "ra": 10.1, "dec": 20.1}],
        "minor_bodies": [{"name": "(1) Ceres", "class": "MB", "vmag": 8.0, "detected": True,
                          "measured_mag": 8.1, "rate_ra_arcsec_h": 10.0, "rate_dec_arcsec_h": 5.0}],
        "warnings": ["a warning"],
    }
    out = build_pdf(result, tmp_path, tmp_path / "r.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 2000
