"""
client_pdf.py — Twin Cities Health client-facing plan-comparison PDF renderer.

Standalone module. Public entry point:
    render_client_comparison(data: dict) -> bytes   # returns PDF bytes

ISOLATION BY CONSTRUCTION (Phase 3, Part 4 / roadmap #14):
This renderer only knows about the fields defined in the `data` contract below.
It has NO access to provider names, drug tier labels, confidence scores, or any
internal drug-run data. An internal report structurally cannot come out this door.

Font note: this first pass uses ReportLab's built-in Helvetica as a placeholder.
Brand font (Inter) embedding is a defined follow-up polish step.
"""

import io
import math
import os
import base64
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Table, TableStyle,
    Paragraph, Spacer,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.graphics.shapes import Drawing, Polygon
from reportlab.lib.utils import ImageReader

# ----- Brand palette (from the locked mockup) -----
BLUE   = HexColor("#1E5FC1")
NAVY   = HexColor("#16468F")
INK    = HexColor("#242A31")
SLATE  = HexColor("#5B6570")
LINE   = HexColor("#E4E9EF")
BAND   = HexColor("#EEF4FC")
BANDLN = HexColor("#D7E4F6")
ZEBRA  = HexColor("#F7F9FC")
GREEN  = HexColor("#1F8A5B")
AMBER  = HexColor("#B5730C")
GOLD   = HexColor("#E8A400")
STAREMPTY = HexColor("#D5DBE2")
WHITE  = HexColor("#FFFFFF")

PAGE_W, PAGE_H = letter          # 612 x 792 pt
BAND_H = 118
SIDE   = 36                      # 0.5in side margins
CONTENT_W = PAGE_W - 2 * SIDE    # 540

# ----- Paragraph styles -----
_LABEL   = ParagraphStyle("label",   fontName="Helvetica-Bold", fontSize=9.5, leading=11.5,
                          textColor=HexColor("#38414B"), alignment=TA_LEFT)
_CELL    = ParagraphStyle("cell",    fontName="Helvetica",      fontSize=10,  leading=12,
                          textColor=HexColor("#2B333C"), alignment=TA_CENTER)
_SECTION = ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=8.5, leading=10,
                          textColor=NAVY, alignment=TA_LEFT)
_CARRIER = ParagraphStyle("carrier", fontName="Helvetica-Bold", fontSize=13.5, leading=16,
                          textColor=NAVY, alignment=TA_CENTER)
_PNAME   = ParagraphStyle("pname",   fontName="Helvetica",      fontSize=10.5, leading=12.5,
                          textColor=HexColor("#38414B"), alignment=TA_CENTER)
_RATING  = ParagraphStyle("rating",  fontName="Helvetica",      fontSize=9,   leading=11,
                          textColor=SLATE, alignment=TA_CENTER)
_PREM    = ParagraphStyle("prem",    fontName="Helvetica-Bold", fontSize=16,  leading=17,
                          textColor=INK, alignment=TA_CENTER)
_PERMO   = ParagraphStyle("permo",   fontName="Helvetica",      fontSize=8,   leading=9,
                          textColor=SLATE, alignment=TA_CENTER)
_FINE    = ParagraphStyle("fine",    fontName="Helvetica",      fontSize=7.5, leading=9.8,
                          textColor=HexColor("#77808B"), alignment=TA_LEFT, spaceAfter=4)
_FINEREQ = ParagraphStyle("finereq", fontName="Helvetica-Bold", fontSize=7.5, leading=9.8,
                          textColor=HexColor("#5B6570"), alignment=TA_LEFT, spaceAfter=4)

# ---- Carrier / plan-name display + fit-to-column ----
# Client-facing brand names (display only; selection still keys on the engine's family).
CARRIER_DISPLAY = {"UHC": "UnitedHealthcare"}
HEADER_PAD = 4          # side padding for the plan-header row only (benefit rows keep 12)


def _wrapped_lines(text, style, width):
    """Line count exactly as ReportLab will draw it (same routine that renders the PDF)."""
    p = Paragraph(escape(text), style)
    p.wrap(width, 10000)
    return len(p.blPara.lines)


def _fit_style(base, text, width, max_lines, min_size):
    """Shrink `base` in 0.5pt steps until `text` wraps to <= max_lines at `width`.
    Returns (style, lines_used). Never goes below min_size. A single word too wide for
    the column gets split mid-word by ReportLab, which shows up here as an extra line."""
    gap = base.leading - base.fontSize

    def style_at(sz):
        if sz == base.fontSize:
            return base
        return ParagraphStyle(base.name + "_fit", parent=base, fontSize=sz, leading=sz + gap)

    size = base.fontSize
    while size > min_size and _wrapped_lines(text, style_at(size), width) > max_lines:
        size -= 0.5
    style = style_at(size)
    return style, _wrapped_lines(text, style, width)


def _name_layout(plans, text_w):
    """Per-plan fitted styles + ONE shared name-area height so stars/premiums align
    across every column. Carrier: 1 line. Plan name: 2 lines, 3 only if unavoidable."""
    out, max_h = [], 0
    for p in plans:
        carrier = CARRIER_DISPLAY.get(p["carrier"], p["carrier"])   # normal case, not ALL CAPS
        c_style, _ = _fit_style(_CARRIER, carrier, text_w, 1, 8)
        n_style, n_lines = _fit_style(_PNAME, p["plan_name"], text_w, 2, 9)
        if n_lines > 2:
            n_style, n_lines = _fit_style(_PNAME, p["plan_name"], text_w, 3, 8.5)
        out.append((carrier, c_style, n_style))
        max_h = max(max_h, n_lines * n_style.leading)
    return out, max(max_h, 2 * _PNAME.leading)


def _star_points(cx, cy, r_out, r_in):
    pts = []
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5
        r = r_out if i % 2 == 0 else r_in
        pts.append(cx + r * math.cos(ang))
        pts.append(cy + r * math.sin(ang))
    return pts


def _star_drawing(rating, n=5):
    """Whole stars gold, plus a half star (gold left half over a gray star) for .5 ratings.
    Uses floor, not round(): Python's round(4.5) == 4, which drew 4.5-star plans as 4."""
    size, gap = 10.5, 1.5
    d = Drawing(n * (size + gap), size)
    full = int(math.floor(rating + 1e-9))
    has_half = (rating - full) >= 0.5 - 1e-9
    for i in range(n):
        cx = i * (size + gap) + size / 2
        cy = size / 2
        pts = _star_points(cx, cy, size / 2, size / 4.4)
        if i < full:
            d.add(Polygon(pts, fillColor=GOLD, strokeColor=GOLD, strokeWidth=0.3))
        else:
            d.add(Polygon(pts, fillColor=STAREMPTY, strokeColor=STAREMPTY, strokeWidth=0.3))
            if i == full and has_half:
                # points 0..5 trace top tip -> left side -> bottom centre = left half of the star
                d.add(Polygon(pts[:12], fillColor=GOLD, strokeColor=GOLD, strokeWidth=0.3))
    return d


def _cell(main, sub=None, kind="normal"):
    color = {"normal": "#2B333C", "green": "#1F8A5B",
             "amber": "#B5730C", "muted": "#9AA4AF"}[kind]
    weight = "Helvetica-Bold" if kind in ("green", "amber") else "Helvetica"
    txt = f'<font name="{weight}" color="{color}">{main}</font>'
    if sub:
        txt += f'<br/><font name="Helvetica" color="#5B6570" size="7.6">{sub}</font>'
    return Paragraph(txt, _CELL)


def _plan_header(p, text_w, carrier, c_style, n_style, name_h):
    # name_h is shared across all columns (see _name_layout) so stars / premium align
    rating = p.get("star_rating")
    if rating is None:            # no CMS rating loaded yet, or plan too new to be rated
        star_cell = Spacer(1, 10.5)
        rating_cell = Paragraph("Not yet rated", _RATING)
    else:
        star_cell = _star_drawing(rating)
        rating_cell = Paragraph(f"{rating:g} / 5", _RATING)
    inner = [
        [Paragraph(escape(carrier), c_style)],
        [Paragraph(escape(p["plan_name"]), n_style)],
        [star_cell],
        [rating_cell],
        [Paragraph(p["plan_premium"], _PREM)],
        [Paragraph("per month", _PERMO)],
    ]
    t = Table(inner, colWidths=[text_w],
              rowHeights=[_CARRIER.leading, name_h + 6, None, None, None, None])
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 1), (0, 1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 4), (0, 4), 5),   # space above premium
    ]))
    return t


def _rx_cell(p, total):
    rx = p["rx"]
    if not rx.get("not_covered") and rx["covered"] == rx["total"]:
        return _cell(f'All {rx["total"]} covered', kind="green")
    names = ", ".join(rx["not_covered"])
    return _cell(f'{rx["covered"]} of {rx["total"]} covered',
                 sub=f'{names} not covered', kind="amber")


def _prov_cell(p):
    pr = p["providers"]
    if pr.get("status") == "verify":     # no provider chart yet — honest interim
        return _cell("Verify with your agent", kind="muted")
    if not pr.get("out") and pr["in"] == pr["total"]:
        label = "Both in-network" if pr["total"] == 2 else f'All {pr["total"]} in-network'
        return _cell(label, kind="green")
    names = ", ".join(pr["out"])
    return _cell(f'{pr["in"]} of {pr["total"]} in-network',
                 sub=f'{names} not in-network', kind="amber")


def _draw_header(canvas, doc, meta):
    top = PAGE_H
    # blue band
    canvas.setFillColor(BLUE)
    canvas.rect(0, top - BAND_H, PAGE_W, BAND_H, stroke=0, fill=1)

    # brand: real TCHIS logo (repo asset app/assets/tchis_logo.png) on a white panel;
    # falls back to a text wordmark if the asset isn't present.
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tchis_logo.png")
    logo_w, logo_h, pad = 200, 32, 8
    panel_w, panel_h = logo_w + 2 * pad, logo_h + 2 * pad
    panel_x, panel_y = SIDE, top - 6 - panel_h
    canvas.setFillColor(WHITE)
    canvas.roundRect(panel_x, panel_y, panel_w, panel_h, 7, stroke=0, fill=1)
    try:
        canvas.drawImage(logo_path, panel_x + pad, panel_y + pad, width=logo_w, height=logo_h,
                         preserveAspectRatio=True, anchor="sw", mask="auto")
    except Exception:
        # logo missing or unreadable -> clean text wordmark, never crash the PDF
        canvas.setFillColor(NAVY)
        canvas.setFont("Helvetica-Bold", 15)
        canvas.drawString(panel_x + pad, panel_y + panel_h - 21, meta.get("agency_name", "Twin Cities Health"))
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(panel_x + pad, panel_y + pad + 4, meta.get("agency_sub", "Insurance Solutions").upper())

    # agency contact (right) — email over phone; agent-agnostic
    rx = PAGE_W - SIDE
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica", 10.5)
    canvas.drawRightString(rx, top - 28, meta["agency_email"])
    canvas.drawRightString(rx, top - 44, meta["agency_phone"])

    # title row
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 21)
    canvas.drawString(SIDE, top - 98, "Your Medicare plan options")
    canvas.setFont("Helvetica", 10)
    canvas.drawRightString(rx, top - 96, f'{meta["plan_year"]} PLAN YEAR')


def render_client_comparison(data) -> bytes:
    meta = data["meta"]
    plans = data["plans"]
    ncol = 1 + len(plans)
    label_w = 162
    plan_w = (CONTENT_W - label_w) / len(plans)
    col_widths = [label_w] + [plan_w] * len(plans)

    rows = []
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]

    # header row (carrier + plan name fitted per column; one shared name height)
    text_w = plan_w - 2 * HEADER_PAD
    layout, name_h = _name_layout(plans, text_w)
    rows.append([""] + [_plan_header(p, text_w, *layout[i], name_h) for i, p in enumerate(plans)])
    style += [
        ("LEFTPADDING", (1, 0), (-1, 0), HEADER_PAD),
        ("RIGHTPADDING", (1, 0), (-1, 0), HEADER_PAD),
        ("VALIGN", (0, 0), (-1, 0), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, BLUE),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
    ]

    def row(label, cells):
        rows.append([Paragraph(label, _LABEL)] + cells)

    sections = [
        ("Monthly costs", [
            ("Plan premium",           lambda p: _cell(p["plan_premium"])),
            ("Drug (Part D) premium",  lambda p: _cell(p["part_d_premium"])),
            ("Part B give-back",       lambda p: _cell(p["part_b_giveback"], kind="green")
                                                 if p.get("part_b_giveback") else _cell("\u2014", kind="muted")),
            (f'Est. yearly cost of your drugs', lambda p: _cell(p["est_annual_drug_cost"])),
        ]),
        ("Medical coverage", [
            ("Medical deductible",     lambda p: _cell(p["deductible"])),
            ("Yearly out-of-pocket max", lambda p: _cell(p["oop_max"])),
            ("Primary doctor visit",   lambda p: _cell(p["pcp_visit"])),
            ("Specialist visit",       lambda p: _cell(p["specialist_visit"])),
            ("Hospital stay (per day)", lambda p: _cell(p["hospital_per_day"]["amount"],
                                                        sub=p["hospital_per_day"].get("note"))),
            ("Emergency room",         lambda p: _cell(p["emergency_room"])),
        ]),
        ("Extra benefits", [
            ("Dental allowance",       lambda p: _cell(p["dental"])),
            ("Vision allowance",       lambda p: _cell(p["vision"])),
            ("Hearing aids",           lambda p: _cell(p["hearing"])),
            ("Over-the-counter card",  lambda p: _cell(p["otc"])),
            ("Fitness (SilverSneakers)", lambda p: _cell(p["fitness"], kind="green")),
        ]),
        ("Your prescriptions & doctors", [
            (f'Your {meta["num_drugs"]} medications',
             lambda p: _rx_cell(p, meta["num_drugs"])),
            (f'Your {meta["num_providers"]} doctors in-network',
             _prov_cell),
        ]),
    ]

    zebra = 0
    for title, items in sections:
        r = len(rows)
        rows.append([Paragraph(title.upper(), _SECTION)] + [""] * (ncol - 1))
        style += [
            ("SPAN", (0, r), (-1, r)),
            ("BACKGROUND", (0, r), (-1, r), BAND),
            ("LINEABOVE", (0, r), (-1, r), 0.5, BANDLN),
            ("LINEBELOW", (0, r), (-1, r), 0.5, BANDLN),
            ("TOPPADDING", (0, r), (-1, r), 4),
            ("BOTTOMPADDING", (0, r), (-1, r), 4),
        ]
        for label, fn in items:
            r = len(rows)
            row(label, [fn(p) for p in plans])
            style += [
                ("LINEBELOW", (0, r), (-1, r), 0.5, LINE),
                ("TOPPADDING", (0, r), (-1, r), 3),
                ("BOTTOMPADDING", (0, r), (-1, r), 3),
            ]
            if zebra % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
            zebra += 1

    tbl = Table(rows, colWidths=col_widths)
    tbl.setStyle(TableStyle(style))

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter,
                          leftMargin=SIDE, rightMargin=SIDE,
                          topMargin=BAND_H + 12, bottomMargin=34)
    frame = Frame(SIDE, 34, CONTENT_W, PAGE_H - BAND_H - 12 - 34,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame],
                          onPage=lambda c, d: _draw_header(c, d, meta))])

    story = [
        tbl,
        Spacer(1, 8),
        Paragraph(
            "We do not offer every plan available in your area. Any information we provide "
            "is limited to those plans we do offer in your area. Please contact Medicare.gov "
            "or 1-800-MEDICARE to get information on all of your options.", _FINEREQ),
        Paragraph(
            "The costs shown are estimates based on the information you provided and current "
            "CMS data. Benefit figures are summaries \u2014 see each plan's Summary of Benefits "
            "and Evidence of Coverage for complete terms and conditions. Confirm drug coverage "
            "and provider networks before enrolling. Twin Cities Health Insurance Solutions, "
            "Inc. is a licensed independent insurance agency.", _FINE),
    ]
    doc.build(story)
    return buf.getvalue()
