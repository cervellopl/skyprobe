"""Offline unit tests: python -m pytest tests/test_units.py  (no network needed)."""
import time
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


def test_database_ingest_and_queries(tmp_path, monkeypatch):
    """One image in, a light curve out - and the same file twice does not double-count."""
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(db, "_conn", None)
    image = tmp_path / "input.fits"
    image.write_bytes(b"not really a fits, but it hashes")

    def result(job_id, jd, mag):
        return {
            "id": job_id, "status": "done", "filename": "a.fits", "created": 1.0,
            "time": {"jd_mid": jd, "utc_mid": "2000-01-01T12:00:00", "source": "fits"},
            "solution": {"ra": 10.0, "dec": 20.0, "pixel_scale": 2.4, "fov_w_deg": 0.5, "fov_h_deg": 0.4},
            "calibration": {"band": "CV", "zero_point": 20.0, "rms": 0.02, "limit_mag_5sigma": 17.0,
                            "n_comps": 30, "response_slope": 0.0},
            "meta": {"make": "ZWO", "model": "Seestar S50", "exptime": 600.0},
            "variables": [
                {"name": "SS Cyg", "oid": 1, "type": "UGSS", "ra": 10.0, "dec": 20.0, "mag": mag,
                 "err": 0.02, "upper_limit": False, "snr": 90.0, "flags": []},
                {"name": "RR Lyr", "oid": 2, "type": "RRAB", "ra": 10.1, "dec": 20.1, "mag": 15.0,
                 "upper_limit": True, "flags": ["fainter than"]},
            ],
            "candidates": [{"status": "unidentified", "kind": "new_star", "label": "possible nova",
                            "ra": 10.2, "dec": 20.2, "mag": 12.0, "snr": 50.0, "fwhm_px": 3.0}],
        }

    assert db.ingest(result("a" * 16, 2451545.0, 11.8), image)["stored"]
    assert db.ingest(result("b" * 16, 2451546.0, 12.1), None)["stored"]
    s = db.stats()
    assert s["images"] == 2 and s["measurements"] == 4 and s["unidentified_candidates"] == 2

    curve = db.light_curve("SS Cyg")
    assert [p["mag"] for p in curve] == [11.8, 12.1]        # ordered in time
    assert curve[0]["camera"] == "ZWO Seestar S50"

    listed = db.stars(q="SS")
    assert listed[0]["name"] == "SS Cyg" and listed[0]["points"] == 2
    assert abs(listed[0]["amplitude"] - 0.3) < 1e-6
    rr = db.stars(q="RR")[0]          # only ever seen as a limit, but still listed
    assert rr["points"] == 0 and rr["limits"] == 2 and rr["brightest"] is None

    near = db.cone(10.0, 20.0, radius_arcmin=1.0)
    assert near and near[0]["name"] == "SS Cyg" and near[0]["separation_arcmin"] < 0.01

    # re-analysing the same file supersedes the earlier rows instead of duplicating them
    assert db.ingest(result("c" * 16, 2451547.0, 12.4), image)["stored"]
    assert db.stats()["images"] == 2
    assert [p["mag"] for p in db.light_curve("SS Cyg")] == [12.1, 12.4]

    db.forget("c" * 16)
    assert db.stats()["images"] == 1


def test_database_backup_and_restore(tmp_path, monkeypatch):
    """A backup round-trips, a merge keeps both sides, and rubbish is refused."""
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "live.sqlite")
    monkeypatch.setattr(db, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(db, "_conn", None)

    def result(job_id, jd, name):
        return {"id": job_id, "status": "done", "filename": f"{job_id}.fits", "created": 1.0,
                "time": {"jd_mid": jd, "utc_mid": "2000-01-01T12:00:00"},
                "solution": {"ra": 1.0, "dec": 2.0}, "calibration": {"band": "CV"},
                "variables": [{"name": name, "mag": 12.0, "err": 0.01, "upper_limit": False, "flags": []}],
                "candidates": []}

    db.ingest(result("a" * 16, 2451545.0, "SS Cyg"))
    first = db.backup()
    assert db.inspect(first)["images"] == 1

    # the archive moves on, then we go back to the backup
    db.ingest(result("b" * 16, 2451546.0, "RR Lyr"))
    assert db.stats()["images"] == 2
    report = db.restore(first)
    assert report["mode"] == "replace" and db.stats()["images"] == 1
    assert db.light_curve("RR Lyr") == []
    assert Path(report["safety_copy"]).exists()          # the discarded state is still recoverable

    # merging that safety copy back brings the second image in without duplicating the first
    merged = db.restore(Path(report["safety_copy"]), merge=True)
    assert merged["mode"] == "merge"
    assert db.stats()["images"] == 2
    assert len(db.light_curve("SS Cyg")) == 1            # not doubled
    assert len(db.light_curve("RR Lyr")) == 1

    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"definitely not a database, just some bytes pretending to be one")
    with pytest.raises(ValueError):
        db.inspect(junk)
    empty = tmp_path / "other.sqlite"
    import sqlite3
    sqlite3.connect(empty).execute("CREATE TABLE unrelated (x)")
    with pytest.raises(ValueError):
        db.inspect(empty)


def test_interrupted_job_is_reported_as_failed(tmp_path, monkeypatch):
    """A job left "running" by a restart must not keep clients waiting for ever."""
    import json as _json
    from app import main

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    job_id = "0123456789abcdef"
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "input.fits").write_bytes(b"x")
    (tmp_path / job_id / "result.json").write_text(_json.dumps(
        {"id": job_id, "status": "running", "stage": "solving", "progress": 40, "created": 1.0, "log": []}))

    r = main._load(job_id)
    assert r["status"] == "failed" and r["interrupted"] is True
    # and the verdict is persisted, so the next reader sees it too
    assert _json.loads((tmp_path / job_id / "result.json").read_text())["status"] == "failed"

    # a job this process really is working on is left alone
    (tmp_path / job_id / "result.json").write_text(_json.dumps(
        {"id": job_id, "status": "running", "stage": "solving", "progress": 40,
         "created": 1.0, "updated": time.time(), "log": []}))
    main.RUNNING.add(job_id)
    try:
        assert main._load(job_id)["status"] == "running"
    finally:
        main.RUNNING.discard(job_id)


def test_duplicate_upload_finds_the_earlier_job(tmp_path, monkeypatch):
    import json as _json
    from app import db, main

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    old, new = "a" * 16, "b" * 16
    for jid, payload in ((old, b"same bytes"), (new, b"other bytes")):
        (tmp_path / jid).mkdir()
        (tmp_path / jid / "input.jpg").write_bytes(payload)
        (tmp_path / jid / "result.json").write_text(_json.dumps({"id": jid, "status": "done", "created": 1.0}))

    digest = db.file_hash(tmp_path / old / "input.jpg")
    assert main._find_duplicate(digest, skip=new)["id"] == old      # matched by contents, not by name
    assert main._find_duplicate(digest, skip=old) is None           # the only copy is the one we skipped
    # the digest is cached in result.json so the next scan does not read the image again
    assert _json.loads((tmp_path / old / "result.json").read_text())["file_hash"] == digest


def test_results_never_carry_nan_into_json(tmp_path, monkeypatch):
    """A NaN anywhere in a result used to make every later read of that job a 500."""
    import json as _json
    from app import main

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    assert main._dumps({"a": float("nan"), "b": [float("inf"), 1.5]}) == '{"a": null, "b": [null, 1.5]}'

    # a result written before this fix still holds a bare NaN token
    job_id = "f" * 16
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "result.json").write_text(
        '{"id": "%s", "status": "done", "variables": [{"name": "SS Cyg", "airmass": NaN}]}' % job_id)
    r = main._load(job_id)
    assert r["variables"][0]["airmass"] is None
    _json.dumps(r, allow_nan=False)      # what the API does; used to raise


def test_aperture_settings_are_honoured_and_ordered():
    from app.photometry import Apertures, aperture_radii

    fwhm = 4.0
    assert aperture_radii(fwhm) == aperture_radii(fwhm, Apertures())          # no setting = old behaviour
    r, r_in, r_out = aperture_radii(fwhm, Apertures(2.0, 3.0, 5.0))
    assert (r, r_in, r_out) == (8.0, 12.0, 20.0)
    # a sky annulus set inside the aperture is pushed back out instead of poisoning the sky
    r, r_in, r_out = aperture_radii(fwhm, Apertures(2.0, 0.5, 0.6))
    assert r_in > r and r_out > r_in


def test_survey_cutout_lands_on_our_pixel_grid(tmp_path, monkeypatch):
    """The survey image must be resampled onto our own grid, rotation and parity included."""
    import numpy as _np
    from astropy.wcs import WCS as _WCS

    from app import dss

    # our frame: 1"/px, rotated 30 deg, flipped parity
    ours = _WCS(naxis=2)
    ours.wcs.crpix = [50, 50]
    ours.wcs.crval = [150.0, 20.0]
    ours.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    a = _np.radians(30.0)
    ours.wcs.cd = _np.array([[_np.cos(a), -_np.sin(a)], [_np.sin(a), _np.cos(a)]]) * (1 / 3600) * _np.array([[1], [1]])

    # the "survey": north up, east left, 0.5"/px, with one bright pixel at a known sky position
    surv = _WCS(naxis=2)
    surv.wcs.crpix = [100, 100]
    surv.wcs.crval = [150.0, 20.0]
    surv.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    surv.wcs.cdelt = [-0.5 / 3600, 0.5 / 3600]
    plane = _np.zeros((200, 200), _np.float32)
    mark_ra, mark_dec = ours.all_pix2world([[56.0, 50.0]], 0)[0]      # 6 px east-ish of centre
    mx, my = surv.all_world2pix([[mark_ra, mark_dec]], 0)[0]
    plane[int(round(my)), int(round(mx))] = 255.0

    monkeypatch.setattr(dss, "_fetch", lambda *a, **k: (plane, surv))
    out = dss.aligned_cutout(ours, 50.0, 50.0, 20.0, 40, cache=tmp_path)
    assert out.shape == (40, 40, 3)
    # 20 original px across 40 output px: 6 px along +x of our frame -> 12 output px, same row
    j, i = _np.unravel_index(_np.argmax(out[:, :, 0]), out.shape[:2])
    assert abs(i - (20 + 12)) < 3 and abs(j - 20) < 3, (i, j)


def test_align_resample_full_reprojects_a_second_frame_onto_the_reference_grid():
    """The full-frame reprojection stacking relies on: a marker at a known sky position in a
    rotated, differently-scaled "other" frame must land at the matching pixel of the
    reference grid - same trick as the survey blink, just frame-to-frame instead of survey."""
    from app import align

    ref = WCS(naxis=2)
    ref.wcs.crpix = [50, 50]
    ref.wcs.crval = [150.0, 20.0]
    ref.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    ref.wcs.cdelt = [-1 / 3600, 1 / 3600]

    other = WCS(naxis=2)
    other.wcs.crpix = [80, 80]
    other.wcs.crval = [150.0, 20.0]
    other.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    a = np.radians(15.0)
    other.wcs.cd = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]) * (0.7 / 3600)

    src = np.zeros((160, 160), np.float32)
    ref_x, ref_y = 65.0, 50.0                       # 15 px east of the reference frame's centre
    mark_ra, mark_dec = ref.all_pix2world([[ref_x, ref_y]], 0)[0]
    sx, sy = other.all_world2pix([[mark_ra, mark_dec]], 0)[0]
    src[int(round(sy)), int(round(sx))] = 1000.0

    out = align.resample_full(ref, 100, 100, other, src)
    assert out.shape == (100, 100)
    j, i = np.unravel_index(np.nanargmax(out), out.shape)      # edge pixels off the source land as NaN
    assert abs(i - ref_x) < 2 and abs(j - ref_y) < 2, (i, j)


def test_multiframe_finds_a_mover_and_flags_a_stationary_unidentified():
    from app import multiframe

    # frame A: a mover at (10, 20) and a stationary unidentified source at (30, 40)
    # frame B, one hour later: the mover has shifted 36" east (36"/h), the stationary one hasn't
    frames = [
        {"job_id": "a", "jd_mid": 2460000.0,
         "candidates": [{"ra": 10.0, "dec": 20.0, "x": 1, "y": 1, "mag": 15.0, "snr": 20},
                        {"ra": 30.0, "dec": 40.0, "x": 2, "y": 2, "mag": 14.0, "snr": 30}]},
        {"job_id": "b", "jd_mid": 2460000.0 + 1 / 24,
         "candidates": [{"ra": 10.0 + 0.01 / np.cos(np.radians(20.0)), "dec": 20.0, "x": 3, "y": 3,
                         "mag": 15.0, "snr": 18},
                        {"ra": 30.0, "dec": 40.0, "x": 4, "y": 4, "mag": 14.0, "snr": 29}]},
    ]
    out = multiframe.find_movers(frames, tol_arcsec=5.0)
    assert len(out["movers"]) == 1
    mover = out["movers"][0]
    assert mover["confidence"] == "pair"
    assert abs(mover["rate_arcsec_h"] - 36.0) < 2.0
    assert [f["job_id"] for f in mover["frames"]] == ["a", "b"]

    assert len(out["stationary_unidentified"]) == 1
    assert out["stationary_unidentified"][0]["seen_in"] == ["a", "b"]


def test_multiframe_chains_a_mover_across_three_frames():
    from app import multiframe

    rate = 20.0   # arcsec/hour, due east
    frames = []
    for i in range(3):
        dt_h = i * 1.0
        ra = 10.0 + (rate * dt_h / 3600.0) / np.cos(np.radians(20.0))
        frames.append({"job_id": f"f{i}", "jd_mid": 2460000.0 + dt_h / 24.0,
                       "candidates": [{"ra": ra, "dec": 20.0, "x": i, "y": i, "mag": 15.0, "snr": 20}]})
    out = multiframe.find_movers(frames, tol_arcsec=3.0)
    assert len(out["movers"]) == 1
    assert out["movers"][0]["confidence"] == "track"
    assert len(out["movers"][0]["frames"]) == 3
    assert abs(out["movers"][0]["rate_arcsec_h"] - rate) < 2.0


def test_images_near_finds_overlapping_fields_and_excludes_far_ones(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(db, "_conn", None)

    def result(job_id, ra, dec):
        return {"id": job_id, "status": "done", "filename": f"{job_id}.fits", "created": 1.0,
                "time": {"jd_mid": 2460000.0, "utc_mid": "2000-01-01T00:00:00", "source": "fits"},
                "solution": {"ra": ra, "dec": dec, "pixel_scale": 2.4, "fov_w_deg": 0.5, "fov_h_deg": 0.4}}

    db.ingest(result("a" * 16, 150.0, 20.0), None)
    db.ingest(result("b" * 16, 150.05, 20.02), None)      # a few arcmin away - overlapping field
    db.ingest(result("c" * 16, 200.0, -10.0), None)        # a different part of the sky entirely

    near = db.images_near(150.0, 20.0, radius_arcmin=30.0, exclude="a" * 16)
    ids = [r["job_id"] for r in near]
    assert "b" * 16 in ids and "c" * 16 not in ids and "a" * 16 not in ids


def test_nearby_and_compare_routes(tmp_path, monkeypatch):
    """Exercise main.job_nearby and main.compare_jobs the way the web UI calls them."""
    import json as _json
    from app import db, main

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(db, "_conn", None)

    def make_job(job_id, ra, dec, jd, cands):
        d = tmp_path / job_id
        d.mkdir()
        result = {"id": job_id, "status": "done", "filename": f"{job_id}.fits", "created": 1.0,
                  "time": {"jd_mid": jd, "utc_mid": "2000-01-01T00:00:00", "source": "fits"},
                  "solution": {"ra": ra, "dec": dec, "pixel_scale": 2.4, "fov_w_deg": 0.5, "fov_h_deg": 0.4},
                  "candidates": cands}
        (d / "result.json").write_text(_json.dumps(result))
        (d / "wcs.fits").write_bytes(b"placeholder - only its existence is checked by /nearby")
        db.ingest(result, None)
        return result

    make_job("a" * 16, 150.0, 20.0, 2460000.0,
            [{"status": "unidentified", "ra": 150.01, "dec": 20.0, "mag": 15.0, "snr": 10}])
    make_job("b" * 16, 150.02, 20.01, 2460000.0 + 1 / 24,
            [{"status": "unidentified", "ra": 150.01 + 0.01, "dec": 20.0, "mag": 15.0, "snr": 10}])
    make_job("c" * 16, 200.0, -10.0, 2460000.0, [])       # unrelated field

    nearby = main.job_nearby("a" * 16)
    assert [r["job_id"] for r in nearby] == ["b" * 16]

    cmp = main.compare_jobs(jobs="a" * 16 + "," + "b" * 16, tol_arcsec=5.0, max_rate_arcsec_h=2000.0)
    assert len(cmp["movers"]) == 1
    assert set(f["job_id"] for f in cmp["movers"][0]["frames"]) == {"a" * 16, "b" * 16}


def test_stack_combines_frames_and_runs_the_normal_pipeline():
    """run_stack should align (identical WCS here, so an identity reprojection), co-add, and
    hand off to the same catalogue/photometry/transient stages a single-image job uses."""
    import tempfile
    from app import pipeline

    wcs = WCS(naxis=2)
    wcs.wcs.crpix = [100, 100]
    wcs.wcs.crval = [150.0, 20.0]
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.cdelt = [-1 / 3600, 1 / 3600]

    rng = np.random.default_rng(1)
    base = np.full((200, 200), 100.0, np.float32)
    yy, xx = np.mgrid[0:200, 0:200]
    stars = [(60, 70), (140, 130), (100, 40), (30, 150), (170, 90), (80, 180), (150, 60), (40, 40),
            (110, 160), (170, 30)]
    sig = 2.0 / 2.355
    for x, y in stars:
        base += 8000.0 / (2 * np.pi * sig ** 2) * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sig ** 2))

    class FakeJob:
        def __init__(self):
            self.result = {"id": "s" * 16, "log": []}
            self.filename = "stack"

        def set_stage(self, stage, progress):
            self.result.update(stage=stage, progress=progress)

        def log(self, msg):
            self.result["log"].append(msg)

        def save(self):
            pass

    sources = [{"job_id": f"src{seed}", "data": rng.poisson(base).astype(np.float32), "wcs": wcs,
               "band": "TG", "saturation": 60000.0, "linear": True, "jd_mid": 2460000.0 + seed}
              for seed in (1, 2)]

    job = FakeJob()
    with tempfile.TemporaryDirectory() as td:
        pipeline.run_stack(job, {"photometry": "false", "transients": "false"}, Path(td), job.log, sources)

    res = job.result
    assert res["file"]["format"] == "stack"
    assert res["stack"] == {"of": ["src1", "src2"], "n": 2, "reference": "src1"}
    assert res["solution"]["ra"] == pytest.approx(150.0, abs=1e-3)
    assert res["detections"]["count"] >= 8
