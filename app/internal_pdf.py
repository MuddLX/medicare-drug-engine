"""Internal agent report, one-table layout (2026-10-02).

One landscape US-letter page: every chosen plan is a column (pick order; standalone
Part D plans last, darker header). Rows, top to bottom: cost for the period, one row
per medication (formulary tier), pharmacy (cheapest nearby, mail order, first months),
and one row per doctor (network status for that column's carrier).

Design rules:
  * Plan names appear once (column headers), with the CMS plan number.
  * Colour only for exceptions: Tier 4-5, not covered, not identified, injectables to
    verify, doctors not found. Tiers 1-3 are plain text.
  * A doctor missing from a directory is "Not found - verify", never "out of network".
  * Text shrinks step by step to keep everything on page 1; if even the smallest size
    does not fit, the table continues on page 2 with the header row repeated.

Same inputs as main.build_pdf_v1 (the previous layout, kept as a fallback).
"""
import io
import re
from datetime import datetime
from xml.sax.saxutils import escape

# carrier key used by the provider lookups (r["<key>_status"]) per CMS contract
CONTRACT_PROVIDER_KEY = {
    "H5959": "bcbs",
    "H4882": "hp", "H6309": "hp",
    "H6154": "medica", "H8889": "medica", "H2450": "medica",
    "H5216": "humana", "H8145": "humana",
    "H2001": "uhc",
    "H3219": "aetna",
}
CARRIER_PHONES = {
    "medica": "Medica 1-800-952-3455", "bcbs": "Blue Cross 1-800-711-9865",
    "hp": "HealthPartners 1-800-233-9645", "humana": "Humana 1-800-457-4708",
    "uhc": "UnitedHealthcare 1-844-867-3487", "aetna": "Aetna 1-800-307-4830",
}
TIER_NOTE = {4: "non-preferred brand", 5: "specialty"}
_SOA_ERA = ("soa date", "appointment date", "signature", "coverage type", "scope of appointment",
            "intake sheet", "sample", "test data")
_MED_WORDS = ("medication", "medications", "medicine", "drug", "drugs", "dose", "dosage", "pill",
              "prescription", "tablet", "capsule", "insulin", "inhaler", "injection", "mg", "mcg", "rx")


def _money(v, cents=True):
    if v is None:
        return "—"
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def _short_pharmacy(name):
    n = (name or "").split("#")[0].replace(" Pharmacy", "").replace(" Pharm", "").strip()
    return n[:22]


def group_warnings(warnings, drug_detail):
    """(label, [items]) for the alert box: medications / form notes / plan requests."""
    names = set()
    for d in drug_detail or []:
        for n in (d.get("original_name"), d.get("drug_name")):
            w = (n or "").strip().lower().split(" ")[0].strip(".,")
            if len(w) >= 4:
                names.add(w)

    def is_med(text):
        low = text.lower()
        if any(re.search(r"\b" + re.escape(k) + r"\b", low) for k in _MED_WORDS):
            return True
        return any(n in low for n in names)

    med, form, plan = [], [], []
    for w in warnings or []:
        drug = (w.get("drug") or "").strip()
        normalized = (w.get("normalized_to") or "").strip()
        flag = (w.get("flag") or "").strip()
        if drug == "Plan Request":
            if flag:
                plan.append(flag)
            continue
        if not normalized and not flag:                 # extraction flag (free text)
            if not drug or any(k in drug.lower() for k in _SOA_ERA):
                continue
            (med if is_med(drug) else form).append(drug)
            continue
        if "not found in rxnav" in flag.lower():
            note = f"{drug}: couldn't identify — verify name"
        else:
            note = drug
            if normalized and normalized.lower() != drug.lower():
                note += f" (read as {normalized})"
            if flag:
                note += f" — {flag}"
        med.append(note)

    def dedupe(items):
        seen, out = set(), []
        for it in items:
            if it.lower() not in seen:
                seen.add(it.lower())
                out.append(it)
        return out
    return [(lbl, dedupe(items)) for lbl, items in
            (("Medications to verify", med), ("Form notes", form), ("Plan requests", plan)) if items]


def pharmacy_summary(label, drug_detail, months):
    totals = {}
    for drug in drug_detail or []:
        for pc in drug.get("plans", {}).get(label, {}).get("pharmacy_costs", []) or []:
            t = totals.setdefault(pc["name"], {"annual": 0.0, "distance": pc.get("distance_miles", 99),
                                               "approx": pc.get("dist_approximate", False), "monthly": {}})
            t["annual"] += pc.get("annual_total", 0) or 0
            for m in pc.get("monthly_costs", []) or []:
                t["monthly"][m["month"]] = t["monthly"].get(m["month"], 0) + (m["cost"] or 0)
    if not totals:
        return None
    cheapest = min(totals, key=lambda k: (round(totals[k]["annual"], 2), totals[k]["distance"]))
    c = totals[cheapest]
    monthly = [c["monthly"].get(mn, 0) for mn in months] if months else []
    steady = monthly[-1] if monthly else (c["annual"] / 12)
    transitions, prev = [], None
    for mn, cost in zip(months or [], monthly):
        if prev is None or abs(cost - prev) > 0.005:
            transitions.append((mn[:3], cost))
            prev = cost
    mail_annual = sum((d.get("plans", {}).get(label, {}).get("mail_order_costs", {}) or {}).get("annual_total", 0) or 0
                      for d in drug_detail or [])
    n = len(months) if months else 12
    return {
        "name": _short_pharmacy(cheapest),
        "distance": ("~" if c["approx"] else "") + f"{c['distance']} mi",
        "steady": steady,
        "annual": c["annual"],
        "all_same": len({round(v["annual"]) for v in totals.values()}) <= 1,
        "transitions": transitions,
        "mail_monthly": (mail_annual / n) if mail_annual else None,
        "mail_saves": (c["annual"] - mail_annual) if mail_annual else 0,
    }


def _written_name(r):
    """The doctor/clinic as the CLIENT wrote it (row label)."""
    first = (r.get("first_name") or "").strip()
    last = (r.get("last_name") or "").strip()
    if first or last:
        name = f"{first} {last}".strip()
        return name if name.lower().startswith("dr") else f"Dr. {name}"
    clinic = (r.get("clinic_name") or "").strip()
    return (clinic or (r.get("raw_text") or "Doctor")).strip()[:45]


def _match_conflict(r):
    """The directory match is a different person: the client wrote a first name and the
    matched first name starts with a different letter (last-name-only false match)."""
    wrote = (r.get("first_name") or "").strip().lower().strip(".")
    got = (r.get("matched_first") or "").strip().lower().strip(".")
    if not wrote or not got:
        return False
    if len(wrote) <= 2 or len(got) <= 2:          # an initial: compare first letters only
        return wrote[0] != got[0]
    return not (wrote.startswith(got[:3]) or got.startswith(wrote[:3]))   # Sarah vs Steven -> conflict


def _doctor_name(r):
    first = (r.get("matched_first") or r.get("first_name") or "").strip()
    last = (r.get("matched_last") or r.get("last_name") or "").strip()
    creds = (r.get("credentials") or "").strip()
    if first or last:
        name = f"{first} {last}".strip()
        return f"{name}, {creds}" if creds else name
    for key in ("bcbs", "hp", "medica", "humana", "uhc", "aetna"):
        d = r.get(f"{key}_detail") or ""
        if r.get(f"{key}_status") == "In Network" and d:
            return d.split(" · ")[0].strip()[:45]
    return (r.get("raw_text") or "Doctor")[:45]


def render(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining,
           confidence=None, warnings=None, drug_detail_full=None, client_address=None,
           client_city=None, provider_results=None, plan_year=None, county=None):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from app.client_comparison import carrier_display, display_plan_name

    INK = colors.HexColor("#1E293B")
    MUTED = colors.HexColor("#475569")
    FAINT = colors.HexColor("#64748B")
    LINE = colors.HexColor("#E2E8F0")
    FRAME = colors.HexColor("#CBD5E1")
    GROUP_BG = colors.HexColor("#DCE3EC")       # section bands (COST / MEDICATIONS / PHARMACY / DOCTORS)
    GROUP_INK = colors.HexColor("#0F172A")
    HEAD_BG = colors.HexColor("#1E293B")
    PD_HEAD_BG = colors.HexColor("#0F5F5C")      # deep teal: Part D stands apart from MA (navy)
    PD_LINE = colors.HexColor("#0F766E")
    AMBER_BG, AMBER_TX = colors.HexColor("#FEF3C7"), colors.HexColor("#92400E")
    RED_BG, RED_TX = colors.HexColor("#FEE2E2"), colors.HexColor("#991B1B")
    GREEN_BG, GREEN_TX = colors.HexColor("#DCFCE7"), colors.HexColor("#166534")
    WARN_BG, WARN_LINE, WARN_TX = colors.HexColor("#FFF7ED"), colors.HexColor("#FED7AA"), colors.HexColor("#7C2D12")

    page_w, page_h = landscape(letter)
    margin = 10 * mm
    avail_w = page_w - 2 * margin

    labels = list(plan_summaries.keys())
    ma = [l for l in labels if plan_summaries[l].get("plan_type") != "PD"]
    pd = [l for l in labels if plan_summaries[l].get("plan_type") == "PD"]
    cols = ma + pd                                     # pick order kept within each group
    months = months_remaining or []
    full_year = len(months) == 12
    period = f"{plan_year} plan year" if (full_year and plan_year) else (
        "full year" if full_year else (f"{months[0][:3]}–{months[-1][:3]}" if months else "period"))

    def S(name, size, **kw):
        base = dict(fontName="Helvetica", fontSize=size, leading=size * 1.25, textColor=INK)
        base.update(kw)
        return ParagraphStyle(name, **base)

    # ---------------------------------------------------------------- header + alerts
    def header_flowables():
        facts = [f"DOB {dob or '—'}", f"ZIP {zip_code or '—'}"]
        if county:
            facts.append(f"{county} County")
        nd = len(drug_detail or [])
        facts.append(f"{nd} medication{'s' if nd != 1 else ''}")
        docs = [_written_name(r) for r in (provider_results or [])]
        if docs:
            facts.append(", ".join(docs[:3]) + (f" +{len(docs) - 3}" if len(docs) > 3 else ""))
        right = [f"Generated {datetime.today().strftime('%m/%d/%Y')}"]
        if plan_year:
            right.append(f"{plan_year} plan year")
        right.append("Data: CMS Q1 2026")
        if confidence:
            try:
                right.append(f"Confidence {float(confidence):.0%}")
            except (TypeError, ValueError):
                pass
        left = [Paragraph(escape(client_name or "Client"), S("h1", 18, fontName="Helvetica-Bold", leading=21)),
                Paragraph(escape(" · ".join(facts)), S("h2", 9, textColor=MUTED))]
        rightp = [Paragraph("INTERNAL USE ONLY", S("b", 8.5, fontName="Helvetica-Bold",
                                                    textColor=colors.HexColor("#B91C1C"), alignment=TA_RIGHT)),
                  Paragraph(escape(" · ".join(right)), S("r", 8, textColor=MUTED, alignment=TA_RIGHT))]
        t = Table([[left, rightp]], colWidths=[avail_w * 0.52, avail_w * 0.48])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                               ("LINEBELOW", (0, 0), (-1, 0), 1.5, INK)]))
        out = [t, Spacer(1, 2.5 * mm)]
        groups = group_warnings(warnings, drug_detail)
        if groups:
            rows = [[Paragraph(f"<b>{escape(lbl)}</b>", S("al", 8.5, textColor=WARN_TX)),
                     Paragraph(escape("  ·  ".join(items)), S("av", 8.5, textColor=WARN_TX))]
                    for lbl, items in groups]
            at = Table(rows, colWidths=[38 * mm, avail_w - 38 * mm])
            at.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
                                    ("BOX", (0, 0), (-1, -1), 0.6, WARN_LINE),
                                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
            out += [at, Spacer(1, 2.5 * mm)]
        return out

    # ---------------------------------------------------------------- the main table
    def build_table(fs):
        cell = S("c", fs, alignment=TA_CENTER)
        cell_b = S("cb", fs + 0.5, alignment=TA_CENTER, fontName="Helvetica-Bold")
        sub = S("s", max(fs - 1.5, 5), alignment=TA_CENTER, textColor=FAINT)
        lab = S("l", fs, fontName="Helvetica-Bold")
        lab_sub = S("ls", max(fs - 1.5, 5), textColor=FAINT)
        grp = S("g", fs, fontName="Helvetica-Bold", textColor=GROUP_INK)   # full size, near-black: easy to find
        head = S("hd", fs, alignment=TA_CENTER, fontName="Helvetica-Bold", textColor=colors.white)
        head_sub = S("hs", max(fs - 1.5, 5), alignment=TA_CENTER, textColor=colors.HexColor("#CBD5E1"))
        pd_tag = S("pt", max(fs - 2, 5), alignment=TA_CENTER, fontName="Helvetica-Bold",
                   textColor=colors.HexColor("#99F6E4"))

        def C(text, subtext=None, style=None):
            parts = [Paragraph(escape(text), style or cell)]
            if subtext:
                parts.append(Paragraph(escape(subtext), sub))
            return parts

        rows, ts = [], []
        label_w = 46 * mm if len(cols) <= 6 else 40 * mm
        col_w = (avail_w - label_w) / max(len(cols), 1)

        # header row
        hrow = [Paragraph("", head)]
        for l in cols:
            s = plan_summaries[l]
            cid, pid = s.get("contract_id", ""), str(s.get("plan_id", "")).zfill(3)
            carrier = carrier_display(cid)
            pname = display_plan_name(s.get("plan_name"), l)
            parts = []
            if l in pd:
                parts.append(Paragraph("PART D · DRUG PLAN", pd_tag))
            parts += [Paragraph(escape(carrier), head), Paragraph(escape(pname), head),
                      Paragraph(escape(f"{cid}-{pid}"), head_sub)]
            hrow.append(parts)
        rows.append(hrow)
        ts += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG)]
        if pd:
            first_pd = 1 + len(ma)
            ts += [("BACKGROUND", (first_pd, 0), (-1, 0), PD_HEAD_BG)]

        def group(title):
            rows.append([Paragraph(escape(title), grp)] + [""] * len(cols))
            r = len(rows) - 1
            ts.extend([("BACKGROUND", (0, r), (-1, r), GROUP_BG), ("SPAN", (0, r), (-1, r)),
                       ("LINEABOVE", (0, r), (-1, r), 1.4, INK), ("LINEBELOW", (0, r), (-1, r), 0.6, FRAME),
                       ("TOPPADDING", (0, r), (-1, r), 5), ("BOTTOMPADDING", (0, r), (-1, r), 5)])

        def tint(r, c, bg, tx=None):
            ts.append(("BACKGROUND", (c, r), (c, r), bg))

        # cost
        group(f"COST — {period.upper()}")
        drug_cost_label = "Est. yearly drug cost" if full_year else f"Est. drug cost ({period})"
        # Drugs the cost can't include (injectables to verify, unidentified): say so on the row.
        excluded = [(d.get("original_name") or d.get("drug_name") or "").strip() for d in (drug_detail or [])
                    if d.get("error") or d.get("is_injectable")]
        excluded = [e for e in excluded if e]
        total_label = "Est. yearly total" if full_year else f"Est. total ({period})"
        for title, fn, bold in (
            ("Monthly premium", lambda s: _money(s.get("premium_monthly")), False),
            ("Drug deductible", lambda s: _money(s.get("deductible"), cents=False), False),
            (drug_cost_label, lambda s: _money(s.get("total_drug_cost")), False),
            (total_label, lambda s: _money(s.get("total_drug_plus_premium")), True),
        ):
            row = [Paragraph(escape(title), lab)]
            if title in (drug_cost_label, total_label) and excluded:
                row = [[Paragraph(escape(title), lab),
                        Paragraph(escape("excludes " + ", ".join(excluded)), lab_sub)]]
            for l in cols:
                s = plan_summaries[l]
                extra = "drugs + premium only" if (bold and l in pd) else None
                row.append(C(fn(s), extra, cell_b if bold else cell))
            rows.append(row)

        # medications
        if drug_detail:
            group("MEDICATIONS — FORMULARY TIER")
            for d in drug_detail:
                name = (d.get("drug_name") or "").strip()
                dose = (d.get("dosage") or "").strip()
                original = (d.get("original_name") or "").strip()
                lab_parts = [Paragraph(escape(f"{name} {dose}".strip() or original or "Medication"), lab)]
                if original and name and original.split()[0].lower() != name.split()[0].lower():
                    lab_parts.append(Paragraph(escape(f"written: {original}"), lab_sub))
                row = [lab_parts]
                r = len(rows)
                for ci, l in enumerate(cols, start=1):
                    pcost = d.get("plans", {}).get(l, {})
                    if d.get("error"):
                        row.append(C("Not identified", "verify the name"))
                        tint(r, ci, AMBER_BG)
                    elif pcost.get("injectable") or d.get("is_injectable"):
                        row.append(C("Verify coverage", "injectable"))
                        tint(r, ci, AMBER_BG)
                    elif not pcost.get("covered", False):
                        row.append(C("Not covered"))
                        tint(r, ci, RED_BG)
                    else:
                        tier = pcost.get("tier")
                        if tier:
                            row.append(C(f"Tier {tier}", TIER_NOTE.get(tier)))
                            if tier in (4, 5):
                                tint(r, ci, AMBER_BG)
                        else:
                            row.append(C("—"))
                rows.append(row)

        # pharmacy
        if drug_detail:
            where = f"{client_address}, {client_city}" if (client_address and client_city) else f"ZIP {zip_code}"
            group(f"PHARMACY — NEAREST IN-NETWORK TO {where.upper()}")
            summaries = {l: pharmacy_summary(l, drug_detail, months) for l in cols}
            r_cheap = [Paragraph("Cheapest nearby", lab)]
            r_mail = [Paragraph("Mail order", lab)]
            r_ramp = [[Paragraph("First months", lab), Paragraph("deductible phase", lab_sub)]]
            for l in cols:
                sm = summaries[l]
                if not sm:
                    r_cheap.append(C("No data")); r_mail.append(C("—")); r_ramp.append(C("—"))
                    continue
                where_sub = "same at all nearby" if sm["all_same"] else f"{sm['name']} · {sm['distance']}"
                r_cheap.append(C(f"{_money(sm['steady'])}/mo", where_sub))
                if sm["mail_monthly"] is not None:
                    saves = f"saves {_money(sm['mail_saves'], cents=False)}/yr" if sm["mail_saves"] > 1 else None
                    r_mail.append(C(f"{_money(sm['mail_monthly'])}/mo", saves))
                else:
                    r_mail.append(C("—"))
                tr = sm["transitions"]
                if len(tr) <= 1:
                    r_ramp.append(C("Same every month"))
                else:
                    head_tr = " · ".join(f"{m} {_money(c, cents=False)}" for m, c in tr[:-1][:3])
                    r_ramp.append(C(head_tr, f"then {_money(tr[-1][1])}"))
            rows += [r_cheap, r_mail, r_ramp]

        # doctors
        used_keys = set()
        if provider_results:
            group("DOCTORS — NETWORK STATUS (2026 DIRECTORIES; VERIFY WITH CARRIER)")
            for pr in provider_results:
                spec = (pr.get("specialty") or "").strip()
                lab_parts = [Paragraph(escape(_written_name(pr)), lab)]
                if spec:
                    lab_parts.append(Paragraph(escape(spec[:40]), lab_sub))
                row = [lab_parts]
                r = len(rows)
                for ci, l in enumerate(cols, start=1):
                    if l in pd:
                        row.append(C("—", "drug plan"))
                        continue
                    key = CONTRACT_PROVIDER_KEY.get(plan_summaries[l].get("contract_id", ""))
                    status = pr.get(f"{key}_status") if key else None
                    if status == "In Network" and _match_conflict(pr):
                        used_keys.add(key)
                        row.append(C("Possible match", f"{_doctor_name(pr)} — verify"))
                        tint(r, ci, AMBER_BG)
                    elif status == "In Network":
                        used_keys.add(key)
                        detail = (pr.get(f"{key}_detail") or "").strip()
                        if pr.get(f"{key}_accepting") == "N":
                            detail = (detail + " · not accepting new").strip(" ·")
                        row.append(C("In network", detail[:48] or None))
                        tint(r, ci, GREEN_BG)
                    elif status == "Not Found":
                        used_keys.add(key)
                        row.append(C("Not found", "verify with carrier"))
                        tint(r, ci, AMBER_BG)
                    else:
                        row.append(C("Not checked", "no directory loaded"))
                rows.append(row)

        t = Table(rows, colWidths=[label_w] + [col_w] * len(cols), repeatRows=1)
        pad = max(1.5, fs * 0.35)
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, FRAME),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ] + ts))
        return t, used_keys

    # ---------------------------------------------------------------- footer + fit
    footer_s = S("f", 7, textColor=FAINT)

    def footer_text(used_keys):
        parts = ["Tiers are plain text; only Tier 4–5, not covered, unidentified and injectable drugs, "
                 "and doctors not found are highlighted. Costs are estimates."]
        phones = [CARRIER_PHONES[k] for k in ("medica", "bcbs", "hp", "humana", "uhc", "aetna") if k in used_keys]
        if phones:
            parts.append("Verify networks: " + " · ".join(phones) + ".")
        return " ".join(parts)

    def on_page(canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(FAINT)
        canv.drawRightString(page_w - margin, margin * 0.55,
                             f"Internal use only · Agent reference · Verify before presenting · Page {doc.page}")
        canv.restoreState()

    head = header_flowables()
    head_h = sum(f.wrap(avail_w, page_h)[1] for f in head)
    frame_h = page_h - 2 * margin - 6          # SimpleDocTemplate frame padding
    chosen = None
    for fs in (9, 8.5, 8, 7.5, 7, 6.5, 6):
        table, used = build_table(fs)
        foot = Paragraph(escape(footer_text(used)), footer_s)
        need = head_h + table.wrap(avail_w, page_h * 5)[1] + 3 * mm + foot.wrap(avail_w, page_h)[1]
        if need <= frame_h - 4:
            chosen = (table, foot)
            break
    if chosen is None:                          # too much for one page: continue on page 2
        table, used = build_table(6)
        chosen = (table, Paragraph(escape(footer_text(used)), footer_s))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(page_w, page_h), leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin, title=f"{client_name} - internal report")
    doc.build(head + [chosen[0], Spacer(1, 3 * mm), chosen[1]], onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
