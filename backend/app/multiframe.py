"""Comparing the "unidentified" candidates of several of the user's own analysed images.

Each single-image job already screens every detection against Gaia/Tycho, VSX, SkyBoT and
HyperLEDA; what is left in `candidates` with `status == "unidentified"` is, by definition, not
in any catalogue. That is exactly what a real asteroid/comet/nova hunt needs to look at next -
but a single frame cannot tell a moving object from a hot pixel, a cosmic ray or a plate flaw.
Blinking is the classic way a human does this by eye; this module does the same comparison
algorithmically across 2+ already-analysed frames of the same field:

  * a candidate that stays within `tol_arcsec` of a candidate in the next frame is stationary
    between those two frames, and not reported as a mover for that pair;
  * one that has no such match, but is found within a physically plausible motion annulus
    (`tol_arcsec` to `max_rate_arcsec_h * dt_hours`), is a candidate mover - flagged with its
    rate and direction, and chained across further frames when the motion stays consistent;
  * one that stays put across *every* frame supplied is reported separately: still nothing in
    any catalogue, but not moving either, which is interesting for a different reason (a
    possible nova/supernova too faint or too new for the catalogues that were queried).
"""
from __future__ import annotations

from astropy.coordinates import SkyCoord


def find_movers(frames: list[dict], tol_arcsec: float = 8.0, max_rate_arcsec_h: float = 1500.0) -> dict:
    """`frames`: [{"job_id", "jd_mid", "candidates": [unidentified candidate dicts]}].

    `tol_arcsec` is how close two detections must be to count as "the same star" - generous
    enough to absorb each frame's own astrometric error. `max_rate_arcsec_h` bounds how far a
    real object could plausibly move between the two frames' epochs (default ~25'/h, well past
    a fast near-Earth object); anything faster is treated as an unrelated pair of detections,
    not a mover.
    """
    frames = sorted((f for f in frames if f.get("jd_mid") is not None), key=lambda f: f["jd_mid"])
    if len(frames) < 2:
        return {"movers": [], "stationary_unidentified": [],
                "warning": "need at least two timed, analysed frames to compare"}

    coords = [SkyCoord([c["ra"] for c in f["candidates"]], [c["dec"] for c in f["candidates"]], unit="deg")
             if f["candidates"] else None
             for f in frames]

    # link[i][ci] = (cj, rate_arcsec_h, direction_deg): candidate `ci` of frame i matched to
    # candidate `cj` of frame i+1, only when the pair moved a plausible amount. None means it
    # stayed put, had nothing to match at all, or jumped further than a real object would.
    link: list[list[tuple[int, float, float] | None]] = []
    for i in range(len(frames) - 1):
        dt_h = (frames[i + 1]["jd_mid"] - frames[i]["jd_mid"]) * 24.0
        row: list[tuple[int, float, float] | None] = [None] * len(frames[i]["candidates"])
        if dt_h > 0 and coords[i] is not None and coords[i + 1] is not None:
            idx, d2d, _ = coords[i].match_to_catalog_sky(coords[i + 1])
            for ci in range(len(row)):
                sep = d2d.arcsec[ci]
                if tol_arcsec < sep <= max_rate_arcsec_h * dt_h:
                    cj = int(idx[ci])
                    pa = coords[i][ci].position_angle(coords[i + 1][cj]).deg
                    row[ci] = (cj, sep / dt_h, float(pa))
        link.append(row)

    movers = []
    claimed: set[tuple[int, int]] = set()
    for i in range(len(frames) - 1):
        for ci in range(len(link[i])):
            if link[i][ci] is None or (i, ci) in claimed:
                continue
            entries = [_entry(frames[i], ci)]
            rates, pas = [], []
            fi, fci = i, ci
            while fi < len(frames) - 1 and link[fi][fci] is not None:
                claimed.add((fi, fci))
                cj, rate, pa = link[fi][fci]
                rates.append(rate)
                pas.append(pa)
                fi, fci = fi + 1, cj
                entries.append(_entry(frames[fi], fci))
            movers.append({"frames": entries, "rate_arcsec_h": sum(rates) / len(rates),
                          "direction_deg": sum(pas) / len(pas),
                          "confidence": "track" if len(entries) >= 3 else "pair"})

    # a candidate present, within tolerance, in every frame supplied (starting from the first)
    stationary = []
    if coords[0] is not None:
        for ci, cand in enumerate(frames[0]["candidates"]):
            point = SkyCoord([cand["ra"]], [cand["dec"]], unit="deg")
            seen = [frames[0]["job_id"]]
            ok = True
            for j in range(1, len(frames)):
                if coords[j] is None:
                    ok = False
                    break
                idx, d2d, _ = point.match_to_catalog_sky(coords[j])
                if d2d.arcsec[0] > tol_arcsec:
                    ok = False
                    break
                seen.append(frames[j]["job_id"])
            if ok:
                stationary.append({"ra": cand["ra"], "dec": cand["dec"], "seen_in": seen,
                                   "label": "unidentified and did not move in any of the frames supplied - "
                                            "not a moving object, but still in no catalogue"})
    return {"movers": movers, "stationary_unidentified": stationary}


def _entry(frame: dict, ci: int) -> dict:
    c = frame["candidates"][ci]
    return {"job_id": frame["job_id"], "jd": frame["jd_mid"], "x": c.get("x"), "y": c.get("y"),
            "ra": c["ra"], "dec": c["dec"], "mag": c.get("mag"), "snr": c.get("snr")}
