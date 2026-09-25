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
import shutil
import sqlite3
import threading
import time
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


def images_near(ra: float, dec: float, radius_arcmin: float, limit: int = 25,
                exclude: str | None = None) -> list[dict]:
    """Finished, solved images whose field could overlap a position - "other exposures of this
    field", for blinking, comparing or stacking. `radius_arcmin` is the search radius around
    (ra, dec), not either image's field size; the caller intersects on field-of-view itself."""
    conn = connect()
    r_deg = radius_arcmin / 60.0
    dra = r_deg / max(math.cos(math.radians(dec)), 0.01)
    rows = conn.execute(
        "SELECT job_id, filename, created, jd_mid, utc_mid, ra, dec, pixel_scale, fov_w_deg, fov_h_deg"
        " FROM images WHERE ra IS NOT NULL AND dec BETWEEN ? AND ? AND ra BETWEEN ? AND ?",
        (dec - r_deg, dec + r_deg, ra - dra, ra + dra)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if exclude and d["job_id"] == exclude:
            continue
        sep = math.degrees(math.acos(min(1.0, math.sin(math.radians(dec)) * math.sin(math.radians(d["dec"]))
                                         + math.cos(math.radians(dec)) * math.cos(math.radians(d["dec"]))
                                         * math.cos(math.radians(ra - d["ra"])))))
        d["separation_arcmin"] = sep * 60
        out.append(d)
    out.sort(key=lambda d: d["separation_arcmin"])
    return out[:limit]


def images(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM images ORDER BY COALESCE(jd_mid, created) DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# backup / restore
# ----------------------------------------------------------------------------

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DB_PATH.parent / "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", 10))


def backup(dest: Path | None = None) -> Path:
    """Consistent copy of the archive, taken through SQLite's online backup API.

    Safe while the server keeps writing - unlike copying the file, which can catch
    a half-written page or miss the write-ahead log.
    """
    if dest is None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        dest = BACKUP_DIR / f"skyprobe-{stamp}.sqlite"
        n = 1
        while dest.exists():      # two backups in the same second must not collide
            dest = BACKUP_DIR / f"skyprobe-{stamp}-{n}.sqlite"
            n += 1
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = connect()
    with _lock:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            target.execute("VACUUM")
        finally:
            target.close()
    _prune_backups()
    return dest


def _prune_backups() -> None:
    if not BACKUP_DIR.exists() or BACKUP_KEEP <= 0:
        return
    files = sorted(BACKUP_DIR.glob("skyprobe-*.sqlite"),
                   key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for old in files[BACKUP_KEEP:]:
        old.unlink(missing_ok=True)


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size, "modified": p.stat().st_mtime}
            for p in sorted(BACKUP_DIR.glob("skyprobe-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)]


def inspect(path: Path) -> dict:
    """What is inside a candidate backup file - checked before it is allowed near the archive."""
    path = Path(path)
    if path.stat().st_size < 100 or path.read_bytes()[:16] != b"SQLite format 3\x00":
        raise ValueError("not a SQLite database")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"images", "measurements"} - tables
        if missing:
            raise ValueError(f"not a SkyProbe archive (missing tables: {', '.join(sorted(missing))})")
        return {
            "images": con.execute("SELECT COUNT(*) FROM images").fetchone()[0],
            "measurements": con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0],
            "candidates": con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            if "candidates" in tables else 0,
        }
    finally:
        con.close()


def restore(src: Path, merge: bool = False) -> dict:
    """Replace the archive with a backup, or merge a backup into it.

    A safety copy of the current archive is always taken first, so a restore of the
    wrong file is not the end of the story.
    """
    src = Path(src)
    incoming = inspect(src)
    before = stats()
    safety = backup()

    conn = connect()
    if merge:
        # SQLite refuses to ATTACH inside a transaction, so it happens around the writes
        conn.execute("ATTACH DATABASE ? AS backup", (str(src),))
        try:
            with _lock, conn:
                # keep both sides: images whose job_id is already here are left alone
                conn.execute("INSERT OR IGNORE INTO images SELECT * FROM backup.images")
                for table, columns in (
                    ("measurements", "job_id, name, oid, type, ra, dec, jd, band, mag, err,"
                                     " upper_limit, snr, airmass, flags"),
                    ("candidates", "job_id, jd, status, kind, label, ra, dec, mag, snr, fwhm_px,"
                                   " known_name, known_catalog"),
                ):
                    conn.execute(
                        f"INSERT INTO {table} ({columns}) SELECT {columns} FROM backup.{table} b"
                        f" WHERE b.job_id NOT IN (SELECT DISTINCT job_id FROM {table})")
        finally:
            conn.execute("DETACH DATABASE backup")
    else:
        # Swap the file rather than copying pages into the live database: in WAL mode the
        # journal of the open connection would replay over the restored pages.
        global _conn
        with _lock:
            conn.close()
            _conn = None
            shutil.copyfile(src, DB_PATH)
            for journal in (DB_PATH.with_name(DB_PATH.name + "-wal"), DB_PATH.with_name(DB_PATH.name + "-shm")):
                journal.unlink(missing_ok=True)
        connect()

    after = stats()
    return {"mode": "merge" if merge else "replace", "incoming": incoming,
            "before": {k: before[k] for k in ("images", "measurements")},
            "after": {k: after[k] for k in ("images", "measurements")},
            "safety_copy": str(safety)}


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


if __name__ == "__main__":
    import sys

    usage = ("usage: python -m app.db backfill [jobs_dir] | backup [file] | "
             "restore <file> [--merge] | stats | backups")
    command = sys.argv[1] if len(sys.argv) > 1 else "stats"
    args = sys.argv[2:]
    if command == "backfill":
        print(json.dumps(backfill(Path(args[0]) if args else DB_PATH.parent / "jobs"), indent=1))
    elif command == "backup":
        print(json.dumps({"backup": str(backup(Path(args[0]) if args else None))}, indent=1))
    elif command == "restore" and args:
        print(json.dumps(restore(Path(args[0]), merge="--merge" in args), indent=1))
    elif command == "backups":
        print(json.dumps(list_backups(), indent=1))
    elif command == "stats":
        print(json.dumps(stats(), indent=1))
    else:
        print(usage)
