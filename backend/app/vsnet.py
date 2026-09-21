"""VSNET (vsnet-obs) observation reports.

Format per the VSNET recommendation
(http://www.kusastro.kyoto-u.ac.jp/vsnet/etc/format.html): four space-separated
fields - object, UT date, magnitude, observer - one observation per line, e.g.

    CYGSS 20000101.345 120 Xyz

The object name puts the three-letter constellation code first and drops the
spaces (SS Cyg -> CYGSS, AG Dra -> DRAAG). CCD magnitudes are written with a
decimal point and a filter letter (16.1C), a fainter-than limit as >16.1, and
an uncertain value with a trailing colon.

The report is *composed* here, never sent: vsnet-obs is a mailing list, so the
message has to come from the observer's own subscribed address.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

CONSTELLATIONS = {
    "and", "ant", "aps", "aql", "aqr", "ara", "ari", "aur", "boo", "cae", "cam", "cap", "car", "cas",
    "cen", "cep", "cet", "cha", "cir", "cma", "cmi", "cnc", "col", "com", "cra", "crb", "crt", "cru",
    "crv", "cvn", "cyg", "del", "dor", "dra", "equ", "eri", "for", "gem", "gru", "her", "hor", "hya",
    "hyi", "ind", "lac", "leo", "lep", "lib", "lmi", "lup", "lyn", "lyr", "men", "mic", "mon", "mus",
    "nor", "oct", "oph", "ori", "pav", "peg", "per", "phe", "pic", "psa", "psc", "pup", "pyx", "ret",
    "scl", "sco", "sct", "ser", "sex", "sge", "sgr", "tau", "tel", "tra", "tri", "tuc", "uma", "umi",
    "vel", "vir", "vol", "vul",
}

# VSNET writes the band as a single letter after the magnitude. "G" is not one of the
# classic codes, so the header block spells out what it means.
BAND_SUFFIX = {"V": "V", "B": "B", "R": "R", "I": "I", "CV": "C", "TG": "G", "TB": "B", "TR": "R"}

DEFAULT_INTRO = """\
{observer} ({site})
{instrument}
Photometry: {band_note}
Reduction: SkyProbe {version} - aperture photometry, ensemble of {ncomp} comparison
stars from {catalog}, zero point {zp} mag, field solved with astrometry.net.
"""

DEFAULT_FOOTER = """\
Limits are 5-sigma detection limits of the frame. A colon marks an uncertain value
(blended star, or a magnitude outside the range covered by the comparison stars).
Please contact me for the images or the full measurement list.

{observer}
"""

LIST_ADDRESS = os.environ.get("VSNET_ADDRESS", "vsnet-obs@ooruri.kusastro.kyoto-u.ac.jp")


def vsnet_object(name: str) -> str:
    """'SS Cyg' -> 'CYGSS'. Designations that are not GCVS-style are left alone."""
    parts = str(name).split()
    if len(parts) >= 2 and parts[-1].lower() in CONSTELLATIONS:
        constellation = parts[-1][:3].upper()
        rest = "".join(parts[:-1]).replace(".", "").replace("-", "")
        return constellation + rest.upper()
    return str(name).replace(" ", "_")


def ut_date(jd: float) -> str:
    """Julian date -> VSNET's YYYYMMDD.ddd in UT."""
    dt = datetime.fromtimestamp((jd - 2440587.5) * 86400.0, tz=timezone.utc)
    frac = round((dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6) / 86400.0, 3)
    if frac >= 1.0:                      # rounded up past midnight
        dt += timedelta(days=1)
        frac = 0.0
    return f"{dt:%Y%m%d}" + f"{frac:.3f}"[1:]


def observation_line(var: dict, jd: float, band: str, observer: str) -> str | None:
    """One VSNET line, or None for measurements that should not be reported."""
    if var.get("saturated"):
        return None
    suffix = BAND_SUFFIX.get(band.upper(), "C")
    mag = var.get("mag")
    if mag is None:
        return None
    if var.get("upper_limit"):
        value = f">{mag:.1f}{suffix}"
    else:
        uncertain = var.get("blended") or var.get("outside_calibration") or (var.get("err") or 0) > 0.2
        value = f"{mag:.2f}{suffix}" + (":" if uncertain else "")
    return f"{vsnet_object(var['name'])} {ut_date(jd)} {value} {observer}"


def is_survey_id(name: str) -> bool:
    """Auto-generated catalogue identifiers rather than designations people discuss."""
    n = str(name).strip().lower()
    return n.startswith(("gaia dr", "gaia edr", "ato j", "ztf j", "2mass", "usno", "ucac", "tyc "))


def select_observations(result: dict, named_only: bool = True, max_error: float = 0.2,
                        include_limits: bool = False, limit: int = 50) -> tuple[list, dict]:
    """Pick what is worth posting. vsnet-obs is a discussion list read by people, not a
    database: a wide phone frame yields ~2000 measurements, and dumping all of them -
    mostly auto-generated Gaia identifiers with 0.4 mag errors - would be spam."""
    stats = {"total": len(result.get("variables", [])), "dropped_survey_id": 0,
             "dropped_flagged": 0, "dropped_error": 0, "dropped_limits": 0}
    picked = []
    for var in result.get("variables", []):
        if var.get("saturated") or var.get("outside_calibration"):
            stats["dropped_flagged"] += 1
            continue
        if named_only and is_survey_id(var.get("name", "")):
            stats["dropped_survey_id"] += 1
            continue
        if var.get("upper_limit"):
            if not include_limits:
                stats["dropped_limits"] += 1
                continue
        elif (var.get("err") or 9) > max_error:
            stats["dropped_error"] += 1
            continue
        picked.append(var)
    picked.sort(key=lambda v: (v.get("upper_limit", False), v.get("mag", 99)))
    stats["selected"] = min(len(picked), limit)
    stats["over_limit"] = max(0, len(picked) - limit)
    return picked[:limit], stats


def build_report(result: dict, observer: str, site: str = "", instrument: str = "",
                 include_limits: bool = False, named_only: bool = True, max_error: float = 0.2,
                 limit: int = 50, intro: str | None = None, footer: str | None = None,
                 subject: str | None = None, version: str = "1.0") -> dict:
    """Compose the message. Returns {to, subject, body, ...}; sending is up to the observer."""
    time_info = result.get("time") or {}
    jd = time_info.get("jd_mid")
    if not jd:
        raise ValueError("The image has no observation time, so it cannot be reported to VSNET")
    cal = result.get("calibration") or {}
    band = (cal.get("band") or (result.get("file") or {}).get("band") or "CV").upper()
    nonlinear = abs(cal.get("response_slope") or 0) > 0.05 or (cal.get("response_curve_amplitude") or 0) > 0.15

    chosen, stats = select_observations(result, named_only, max_error, include_limits, limit)
    lines = [ln for ln in (observation_line(v, jd, band, observer or "obs") for v in chosen) if ln]

    band_note = {
        "TG": "green channel of a colour sensor (AAVSO TG), transformed to Johnson V; "
              "reported here with the suffix G",
        "CV": "unfiltered, transformed to Johnson V (suffix C)",
    }.get(band, f"{band} band")
    fields = {
        "observer": observer or "(observer)",
        "site": site or "site not given",
        "instrument": instrument or (result.get("meta", {}).get("model") or "instrument not given"),
        "band_note": band_note,
        "ncomp": cal.get("n_comps", "?"),
        "catalog": cal.get("catalog", "Gaia DR3 / Tycho-2"),
        "zp": f"{cal.get('zero_point', 0):.2f} +/- {cal.get('rms', 0):.3f}",
        "version": version,
        "n": len(lines),
        "date": time_info.get("utc_mid", ""),
    }
    head = (intro or os.environ.get("VSNET_INTRO") or DEFAULT_INTRO).format(**fields)
    tail = (footer or os.environ.get("VSNET_FOOTER") or DEFAULT_FOOTER).format(**fields)
    note = ""
    if nonlinear:
        note = ("\nNOTE: the camera response of this frame is not linear (the phone stacked and\n"
                "tone-compressed the raw file), so these magnitudes are indicative only.\n")

    body = f"{head}{note}\n" + "\n".join(lines) + f"\n\n{tail}"
    subject = subject or f"{observer or 'SkyProbe'} observations {ut_date(jd)}"
    return {"to": LIST_ADDRESS, "subject": subject, "body": body, "band": band,
            "n_observations": len(lines), "nonlinear": nonlinear, "selection": stats,
            "blocked": nonlinear,
            "blocked_reason": ("The camera response of this image is not linear, so the magnitudes are "
                               "not good enough to post to a mailing list. Send it only if you know "
                               "what you are doing.") if nonlinear else None}
