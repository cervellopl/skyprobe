"""PDF report of one analysis: what was measured, how, and with what caveats."""
from __future__ import annotations

import math
from pathlib import Path

from fpdf import FPDF

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
]
ACCENT = (30, 80, 160)
MUTED = (110, 120, 135)
WARN = (170, 110, 0)


def _f(v, d=2, dash="-"):
    try:
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return dash
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return dash


class Report(FPDF):
    def __init__(self, title: str, subtitle: str, unicode_font: bool):
        super().__init__(format="A4")
        self.title_text = title
        self.subtitle = subtitle
        self.body_font = "DejaVu" if unicode_font else "Helvetica"
        self.set_auto_page_break(True, margin=18)

    def header(self):
        self.set_font(self.body_font, "B", 13)
        self.set_text_color(*ACCENT)
        self.cell(0, 7, self.title_text, new_x="LMARGIN", new_y="NEXT")
        self.set_font(self.body_font, "", 8.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, self.subtitle, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(220, 225, 235)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-14)
        self.set_font(self.body_font, "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "SkyProbe - astrometry.net, Gaia DR3 / Tycho-2 and AAVSO VSX via VizieR, "
                        "IMCCE SkyBoT", align="L")
        self.cell(0, 4, f"{self.page_no()}", align="R")

    def section(self, text):
        self.ln(2)
        self.set_font(self.body_font, "B", 10.5)
        self.set_text_color(*ACCENT)
        self.cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def _clip(self, text, width):
        text = str(text)
        if self.get_string_width(text) <= width - 1.5:
            return text
        while text and self.get_string_width(text + "…") > width - 1.5:
            text = text[:-1]
        return text + "…"

    def kv(self, pairs, cols=2, label_share=0.46):
        self.set_font(self.body_font, "", 9)
        pairs = [(k, v) for k, v in pairs if v is not None]
        column = (self.w - self.l_margin - self.r_margin) / cols
        gutter = 4
        label_w = (column - gutter) * label_share
        value_w = (column - gutter) * (1 - label_share)
        for i, (key, value) in enumerate(pairs):
            if i % cols == 0 and i:
                self.ln(5)
            self.set_xy(self.l_margin + (i % cols) * column, self.get_y())
            self.set_text_color(*MUTED)
            self.cell(label_w, 5, self._clip(key, label_w))
            self.set_text_color(0, 0, 0)
            self.cell(value_w, 5, self._clip(value, value_w))
        self.ln(6)

    def note(self, text, colour=MUTED, size=8.5):
        self.set_font(self.body_font, "", size)
        self.set_text_color(*colour)
        self.multi_cell(0, 4.2, text, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

    def table(self, headers, rows, widths, empty="none"):
        if not rows:
            self.note(empty)
            return
        avail = self.w - self.l_margin - self.r_margin
        widths = [w * avail / sum(widths) for w in widths]
        self.set_font(self.body_font, "B", 8)
        self.set_fill_color(238, 242, 248)
        for head, width in zip(headers, widths):
            self.cell(width, 5.5, str(head), fill=True)
        self.ln(5.5)
        self.set_font(self.body_font, "", 8)
        for n, row in enumerate(rows):
            if self.get_y() > self.h - 24:
                self.add_page()
                self.set_font(self.body_font, "B", 8)
                for head, width in zip(headers, widths):
                    self.cell(width, 5.5, str(head), fill=True)
                self.ln(5.5)
                self.set_font(self.body_font, "", 8)
            if n % 2:
                self.set_fill_color(248, 250, 253)
                self.cell(sum(widths), 4.8, "", fill=True)
                self.set_x(self.l_margin)
            for value, width in zip(row, widths):
                self.cell(width, 4.8, self._clip(value, width))
            self.ln(4.8)
        self.ln(2)


def build_pdf(result: dict, workdir: Path, out: Path, max_rows: int = 60) -> Path:
    unicode_font = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    sol = result.get("solution") or {}
    cal = result.get("calibration") or {}
    file_info = result.get("file") or {}
    time_info = result.get("time") or {}
    meta = result.get("meta") or {}

    subtitle = " · ".join(x for x in [
        result.get("filename"),
        f"{file_info.get('width', '?')}x{file_info.get('height', '?')} {str(file_info.get('format', '')).upper()}",
        (time_info.get("utc_mid", "")[:19] + " UT") if time_info else None,
        f"{meta.get('make', '')} {meta.get('model', '')}".strip() or None,
    ] if x)
    pdf = Report("SkyProbe analysis report", subtitle, bool(unicode_font))
    if unicode_font:
        pdf.add_font("DejaVu", "", unicode_font)
        pdf.add_font("DejaVu", "B", unicode_font)
    pdf.add_page()

    preview = workdir / "annotated.jpg"
    if preview.exists():
        width = pdf.w - pdf.l_margin - pdf.r_margin
        height = width * file_info.get("height", 2) / max(file_info.get("width", 3), 1)
        height = min(height, 95)
        pdf.image(str(preview), x=pdf.l_margin, w=width, h=height, keep_aspect_ratio=True)
        pdf.ln(2)
        pdf.note("Red: unidentified candidates · yellow: identified · blue: measured variables · "
                 "orange: known minor bodies")

    pdf.section("Astrometry")
    fov = (f"{_f(sol.get('fov_w_deg') * 60, 1)}' x {_f(sol.get('fov_h_deg') * 60, 1)}'"
           if sol.get("fov_w_deg", 9) < 2 else
           f"{_f(sol.get('fov_w_deg'), 2)} x {_f(sol.get('fov_h_deg'), 2)} deg")
    pdf.kv([
        ("Centre", f"{sol.get('ra_hms', '-')}  {sol.get('dec_dms', '')}"),
        ("RA / Dec", f"{_f(sol.get('ra'), 5)} / {_f(sol.get('dec'), 5)} deg"),
        ("Pixel scale", f"{_f(sol.get('pixel_scale'), 3)} arcsec/px"),
        ("Field of view", fov),
        ("Rotation", f"{_f(sol.get('rotation_deg'), 1)} deg ({sol.get('parity', '-')})"),
        ("Galactic", f"l {_f(sol.get('gal_l'), 2)}, b {_f(sol.get('gal_b'), 2)}"),
        ("Solver", f"{sol.get('solver', '-')} ({_f(sol.get('solve_seconds'), 1)} s)"),
        ("Distortion fit", (f"SIP {result['distortion'].get('sip_degree')}, resid "
                            f"{_f(result['distortion'].get('residual_rms_px'), 2)} px")
         if (result.get("distortion") or {}).get("refined") else None),
        ("Stars / FWHM", f"{(result.get('detections') or {}).get('count', '-')} / "
                         f"{_f((result.get('detections') or {}).get('fwhm_px'), 1)} px"),
        ("Time (UT)", f"{time_info.get('utc_mid', '-')}  [{time_info.get('source', '-')}]"),
        ("Julian date", _f(time_info.get("jd_mid"), 5)),
        ("Exposure", f"{_f(meta.get('exptime'), 1)} s" if meta.get("exptime") else None),
    ])

    if cal:
        pdf.section("Photometric calibration")
        pdf.kv([
            ("Band", cal.get("band", "-")),
            ("Catalogue", cal.get("catalog", "-")),
            ("Comparison stars", cal.get("n_comps", "-")),
            ("Zero point", f"{_f(cal.get('zero_point'), 3)} +/- {_f(cal.get('rms'), 3)} mag"),
            ("Scatter (local corr.)", _f(cal.get("rms_local"), 3)),
            ("Colour term", _f(cal.get("color_term"), 3)),
            ("Limit (5 sigma)", _f(cal.get("limit_mag_5sigma"), 1)),
            ("Aperture radius", f"{_f(cal.get('aperture_px'), 1)} px"),
            ("Response slope", f"{_f(1 + (cal.get('response_slope') or 0), 3)} (1.0 = linear)"),
        ])

    variables = [v for v in result.get("variables", []) if not v.get("upper_limit")]
    pdf.section(f"Variable stars ({len(result.get('variables', []))} measured, "
                f"{len(variables)} with a magnitude)")
    pdf.table(
        ["Star", "Type", "Mag", "Err", "VSX range", "Period [d]", "Airmass", "Flags"],
        [[v["name"], v.get("type", ""), _f(v.get("mag"), 3), _f(v.get("err"), 3),
          (f"{_f(v.get('max'))}-{_f(v.get('min'))}" if not v.get("min_is_amplitude")
           else f"{_f(v.get('max'))} ampl {_f(v.get('min'))}"),
          _f(v.get("period"), 4), _f(v.get("airmass"), 2), ", ".join(v.get("flags", []))]
         for v in variables[:max_rows]],
        [26, 14, 10, 9, 17, 14, 10, 20], empty="No catalogued variables measured.")
    if len(variables) > max_rows:
        pdf.note(f"{len(variables) - max_rows} further measurements are in photometry.csv.")

    cands = result.get("candidates", [])
    pdf.section(f"New-object search ({sum(1 for c in cands if c.get('status') == 'unidentified')} unidentified)")
    if result.get("search_skipped"):
        skip = result["search_skipped"]
        pdf.note(f"Not searched: {skip.get('reason')} (catalogue {_f(skip.get('catalogue_limit'), 1)} mag, "
                 f"image {_f(skip.get('image_limit'), 1)} mag).", WARN)
    pdf.table(["Status", "Object", "Mag", "SNR", "FWHM", "RA", "Dec"],
              [[c.get("status", ""), c.get("label", "")[:60], _f(c.get("mag")), _f(c.get("snr"), 0),
                _f(c.get("fwhm_px"), 1), _f(c.get("ra"), 5), _f(c.get("dec"), 5)]
               for c in cands[:max_rows]],
              [16, 46, 10, 9, 9, 15, 15], empty="Nothing above the catalogue limit.")

    bodies = result.get("minor_bodies", [])
    pdf.section(f"Known asteroids and comets in field ({len(bodies)})")
    pdf.table(["Name", "Class", "Predicted V", "Detected", "Measured", "Motion [\"/h]"],
              [[b.get("name", ""), b.get("class", ""), _f(b.get("vmag"), 1),
                "yes" if b.get("detected") else "no", _f(b.get("measured_mag"), 2),
                _f(math.hypot(b.get("rate_ra_arcsec_h", 0), b.get("rate_dec_arcsec_h", 0)), 1)]
               for b in bodies[:max_rows]],
              [34, 18, 14, 12, 12, 14], empty="None brighter than the image limit.")

    if result.get("warnings"):
        pdf.section("Warnings")
        for w in result["warnings"]:
            pdf.note("- " + w, WARN)

    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out
