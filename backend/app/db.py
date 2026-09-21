"""Central measurement database.

Every finished analysis is folded into one SQLite file: the image and its
solution, one row per measured star, and the transient candidates. That turns a
pile of single images into something you can ask questions of - the light curve
of a star across nights, everything measured near a position, what was observed
when.

SQLite keeps the whole thing to one file that can be copied, backed up and
queried with any tool; there is no separate server to run on a Raspberry Pi.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).resolve().parent.parent / "data" / "skyprobe.sqlite"))
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    job_id      TEXT PRIMARY KEY,
    file_hash   TEXT,
    filename    TEXT,
    created     REAL,
    jd_mid      REAL,
    utc_mid     TEXT,
    time_source TEXT,
    ra          REAL,
    dec         REAL,
    pixel_scale REAL,
    fov_w_deg   REAL,
    fov_h_deg   REAL,
    band        TEXT,
    zero_point  REAL,
    zp_rms      REAL,
    limit_mag   REAL,
    n_comps     INTEGER,
    camera      TEXT,
    exptime     REAL,
    lat         REAL,
    lon         REAL,
    nonlinear   INTEGER,
    n_measurements INTEGER,
    n_candidates   INTEGER
);
CREATE INDEX IF NOT EXISTS images_jd ON images(jd_mid);
CREATE INDEX IF NOT EXISTS images_hash ON images(file_hash);

CREATE TABLE IF NOT EXISTS measurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES images(job_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    oid         INTEGER,
    type        TEXT,
    ra          REAL,
    dec         REAL,
    jd          REAL,
    band        TEXT,
    mag         REAL,
    err         REAL,
    upper_limit INTEGER,
    snr         REAL,
    airmass     REAL,
    flags       TEXT
);
CREATE INDEX IF NOT EXISTS meas_name ON measurements(name);
CREATE INDEX IF NOT EXISTS meas_jd ON measurements(jd);
CREATE INDEX IF NOT EXISTS meas_pos ON measurements(dec, ra);
CREATE INDEX IF NOT EXISTS meas_job ON measurements(job_id);

CREATE TABLE IF NOT EXISTS candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES images(job_id) ON DELETE CASCADE,
    jd          REAL,
    status      TEXT,
    kind        TEXT,
    label       TEXT,
    ra          REAL,
    dec         REAL,
    mag         REAL,
    snr         REAL,
    fwhm_px     REAL,
    known_name  TEXT,
    known_catalog TEXT
);
CREATE INDEX IF NOT EXISTS cand_pos ON candidates(dec, ra);
CREATE INDEX IF NOT EXISTS cand_job ON candidates(job_id);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def file_hash(path: str | Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _num(v):
    try:
        v = float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def ingest(result: dict, input_path: str | Path | None = None) -> dict:
    """Add (or replace) one analysis. Re-analysing the same file supersedes the old rows."""
    job_id = result.get("id")
    if not job_id or result.get("status") != "done":
        return {"stored": False, "reason": "not a finished analysis"}
    sol = result.get("solution") or {}
    cal = result.get("calibration") or {}
    t = result.get("time") or {}
    meta = result.get("meta") or {}
    jd = _num(t.get("jd_mid"))
    digest = file_hash(input_path) if input_path else None

    conn = connect()
    with _lock, conn:
        if digest:      # the same picture analysed twice should not count twice
            conn.execute("DELETE FROM images WHERE file_hash = ? AND job_id <> ?", (digest, job_id))
        conn.execute("DELETE FROM images WHERE job_id = ?", (job_id,))
        conn.execute(
            "INSERT INTO images (job_id, file_hash, filename, created, jd_mid, utc_mid, time_source, ra, dec,"
            " pixel_scale, fov_w_deg, fov_h_deg, band, zero_point, zp_rms, limit_mag, n_comps, camera, exptime,"
            " lat, lon, nonlinear, n_measurements, n_candidates)"
            " VALUES (" + ",".join("?" * 24) + ")",
            (job_id, digest, result.get("filename"), _num(result.get("created")), jd, t.get("utc_mid"),
             t.get("source"), _num(sol.get("ra")), _num(sol.get("dec")), _num(sol.get("pixel_scale")),
             _num(sol.get("fov_w_deg")), _num(sol.get("fov_h_deg")), cal.get("band"),
             _num(cal.get("zero_point")), _num(cal.get("rms")), _num(cal.get("limit_mag_5sigma")),
             cal.get("n_comps"), f"{meta.get('make', '')} {meta.get('model', '')}".strip() or None,
             _num(meta.get("exptime")), _num(meta.get("lat")), _num(meta.get("lon")),
             int(abs(cal.get("response_slope") or 0) > 0.05 or (cal.get("response_curve_amplitude") or 0) > 0.15),
             len(result.get("variables", [])), len(result.get("candidates", []))),
        )
        conn.executemany(
            "INSERT INTO measurements (job_id, name, oid, type, ra, dec, jd, band, mag, err, upper_limit,"
            " snr, airmass, flags) VALUES (" + ",".join("?" * 14) + ")",
            [(job_id, v.get("name"), v.get("oid"), v.get("type"), _num(v.get("ra")), _num(v.get("dec")), jd,
              cal.get("band"), _num(v.get("mag")), _num(v.get("err")), int(bool(v.get("upper_limit"))),
              _num(v.get("snr")), _num(v.get("airmass")), ",".join(v.get("flags", [])) or None)
             for v in result.get("variables", [])],
        )
        conn.executemany(
            "INSERT INTO candidates (job_id, jd, status, kind, label, ra, dec, mag, snr, fwhm_px,"
            " known_name, known_catalog) VALUES (" + ",".join("?" * 12) + ")",
            [(job_id, jd, c.get("status"), c.get("kind"), c.get("label"), _num(c.get("ra")), _num(c.get("dec")),
              _num(c.get("mag")), _num(c.get("snr")), _num(c.get("fwhm_px")),
              (c.get("known") or {}).get("name"), (c.get("known") or {}).get("catalog"))
             for c in result.get("candidates", [])],
        )
    return {"stored": True, "job_id": job_id, "measurements": len(result.get("variables", [])),
            "candidates": len(result.get("candidates", []))}


def forget(job_id: str) -> None:
    conn = connect()
    with _lock, conn:
        conn.execute("DELETE FROM images WHERE job_id = ?", (job_id,))


# ----------------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------------

def stats() -> dict:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS images, COALESCE(SUM(n_measurements), 0) AS measurements,"
        " MIN(jd_mid) AS first_jd, MAX(jd_mid) AS last_jd FROM images").fetchone()
    stars = conn.execute("SELECT COUNT(DISTINCT name) AS n FROM measurements").fetchone()["n"]
    cands = conn.execute("SELECT COUNT(*) AS n FROM candidates WHERE status = 'unidentified'").fetchone()["n"]
    nights = conn.execute("SELECT COUNT(DISTINCT CAST(jd_mid - 0.5 AS INTEGER)) AS n"
                          " FROM images WHERE jd_mid IS NOT NULL").fetchone()["n"]
    out = dict(row)
    size = sum(p.stat().st_size for p in (DB_PATH, DB_PATH.with_suffix(".sqlite-wal")) if p.exists())
    out.update({"stars": stars, "unidentified_candidates": cands, "nights": nights, "db_bytes": size})
    return out


def stars(q: str = "", limit: int = 100, min_points: int = 0, offset: int = 0) -> list[dict]:
    """Stars in the database with a summary of their measurements."""
    conn = connect()
    where, args = "WHERE 1=1", []
    if q:
        where += " AND (name LIKE ? OR type LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    # "points" counts real measurements; a star that was only ever fainter than the limit
    # still belongs in the list, because "we looked and saw nothing" is an observation too
    rows = conn.execute(
        f"SELECT name, MAX(type) AS type,"
        f" SUM(CASE WHEN upper_limit = 0 THEN 1 ELSE 0 END) AS points,"
        f" SUM(upper_limit) AS limits,"
        f" MIN(CASE WHEN upper_limit = 0 THEN mag END) AS brightest,"
        f" MAX(CASE WHEN upper_limit = 0 THEN mag END) AS faintest,"
        f" AVG(CASE WHEN upper_limit = 0 THEN mag END) AS mean_mag,"
        f" MAX(jd) AS last_jd, MIN(jd) AS first_jd, AVG(ra) AS ra, AVG(dec) AS dec"
        f" FROM measurements {where} GROUP BY name HAVING points >= ?"
        f" ORDER BY points DESC, limits DESC, name LIMIT ? OFFSET ?",
        (*args, min_points, limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amplitude"] = (d["faintest"] - d["brightest"]) if d["brightest"] is not None else None
        out.append(d)
    return out


def light_curve(name: str) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT m.jd, m.mag, m.err, m.upper_limit, m.band, m.airmass, m.flags, m.job_id,"
        " i.filename, i.nonlinear, i.camera FROM measurements m JOIN images i ON i.job_id = m.job_id"
        " WHERE m.name = ? ORDER BY m.jd", (name,)).fetchall()
    return [dict(r) for r in rows]


def cone(ra: float, dec: float, radius_arcmin: float = 5.0, limit: int = 200) -> list[dict]:
    """Measurements near a position (small-angle box filter, then exact separation)."""
    conn = connect()
    r_deg = radius_arcmin / 60.0
    dra = r_deg / max(math.cos(math.radians(dec)), 0.01)
    rows = conn.execute(
        "SELECT name, type, ra, dec, jd, mag, err, upper_limit, job_id FROM measurements"
        " WHERE dec BETWEEN ? AND ? AND ra BETWEEN ? AND ? LIMIT 5000",
        (dec - r_deg, dec + r_deg, ra - dra, ra + dra)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        sep = math.degrees(math.acos(min(1.0, math.sin(math.radians(dec)) * math.sin(math.radians(d["dec"]))
                                         + math.cos(math.radians(dec)) * math.cos(math.radians(d["dec"]))
                                         * math.cos(math.radians(ra - d["ra"])))))
        if sep <= r_deg:
            d["separation_arcmin"] = sep * 60
            out.append(d)
    out.sort(key=lambda d: d["separation_arcmin"])
    return out[:limit]


def images(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM images ORDER BY COALESCE(jd_mid, created) DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def backfill(jobs_dir: Path) -> dict:
    """Fold every finished job on disk into the database (idempotent)."""
    stored = skipped = 0
    for result_file in sorted(Path(jobs_dir).glob("*/result.json")):
        try:
            result = json.loads(result_file.read_text())
        except Exception:
            skipped += 1
            continue
        inputs = list(result_file.parent.glob("input.*"))
        if ingest(result, inputs[0] if inputs else None)["stored"]:
            stored += 1
        else:
            skipped += 1
    return {"stored": stored, "skipped": skipped, **stats()}


if __name__ == "__main__":  # python -m app.db [jobs_dir]
    import sys

    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH.parent / "jobs"
    print(json.dumps(backfill(directory), indent=1))
