"""
Medicare Drug Cost API v3
Two endpoints:
- POST /drug-costs: returns drug cost JSON (for testing/Make logging)
- POST /html-to-pdf: accepts simple flat fields, calls drug-costs internally, returns PDF
"""

from flask import Flask, request, jsonify, Response
import sqlite3
import os
from datetime import datetime, date
import requests

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "medicare_mn.db")

PLANS = [
    {"carrier": "HealthPartners", "contract_id": "H4882", "plan_id": "009", "type": "MA"},
    {"carrier": "Blue Cross",     "contract_id": "H5959", "plan_id": "009", "type": "MA"},
    {"carrier": "Medica",         "contract_id": "H6154", "plan_id": "001", "type": "MA"},
    {"carrier": "Humana",         "contract_id": "H5216", "plan_id": "275", "type": "MA"},
    {"carrier": "Aetna",          "contract_id": "H3219", "plan_id": "001", "type": "MA"},
    {"carrier": "Humana Part D",  "contract_id": "S5884", "plan_id": "190", "type": "PD"},
    {"carrier": "WellCare Part D","contract_id": "S4802", "plan_id": "146", "type": "PD"},
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_remaining_months(soa_date_str):
    try:
        soa = datetime.strptime(soa_date_str, "%m/%d/%Y").date()
    except Exception:
        soa = date.today()
    return list(range(soa.month, 13))


def parse_drugs(drugs_input, dosages_input=""):
    """
    Accepts:
    - Array of objects: [{"name": "Metformin", "dosage": "500mg"}]
    - Comma string of names: "Eliquis,Metformin,Lisinopril"
    - Plus optional separate dosages string: "5mg,500mg,10mg"
    """
    if isinstance(drugs_input, list):
        result = []
        for d in drugs_input:
            if isinstance(d, dict):
                result.append({"name": d.get("name", "").strip(), "dosage": d.get("dosage", "").strip()})
            elif isinstance(d, str):
                result.append({"name": d.strip(), "dosage": ""})
        return result
    elif isinstance(drugs_input, str):
        names = [n.strip() for n in drugs_input.split(",") if n.strip()]
        dosages = [d.strip() for d in dosages_input.split(",")] if dosages_input else []
        result = []
        for i, name in enumerate(names):
            dosage = dosages[i] if i < len(dosages) else ""
            result.append({"name": name, "dosage": dosage})
        return result
    return []


def lookup_rxcuis(drug_name, dosage=""):
    """
    Look up product-level RXCUIs for a drug name + optional dosage.
    When dosage is provided, searches with full string (e.g. "metformin 500 mg")
    for dosage-specific pricing. Falls back to name-only if no results.
    """
    rxcuis = []

    def fetch_rxcuis(search_str):
        found = []
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/drugs.json?name={requests.utils.quote(search_str)}"
            resp = requests.get(url, timeout=8)
            data = resp.json()
            for group in data.get("drugGroup", {}).get("conceptGroup", []):
                if group.get("tty", "") in ["SCD", "SBD", "GPCK", "BPCK"]:
                    for concept in group.get("conceptProperties", []):
                        found.append(concept["rxcui"])
        except Exception:
            pass
        return found

    # Try name + dosage first for specificity
    if dosage:
        rxcuis = fetch_rxcuis(f"{drug_name} {dosage}")

    # Fall back to name only
    if not rxcuis:
        rxcuis = fetch_rxcuis(drug_name)

    # Last resort: ingredient RXCUI
    if not rxcuis:
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={requests.utils.quote(drug_name)}&search=2"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            rxcuis = list(data.get("idGroup", {}).get("rxnormId", []))
        except Exception:
            pass

    return rxcuis


def get_drug_cost_for_plan(conn, formulary_id, contract_id, plan_id, rxcuis, deductible, months_remaining):
    plan_id_padded = plan_id.zfill(3)
    tier_row = None
    for rxcui in rxcuis:
        row = conn.execute("""
            SELECT tier, ndc FROM formulary
            WHERE formulary_id = ? AND rxcui = ?
            ORDER BY tier ASC LIMIT 1
        """, (formulary_id, rxcui)).fetchone()
        if row and (tier_row is None or row["tier"] < tier_row["tier"]):
            tier_row = row

    if not tier_row:
        return {"tier": None, "covered": False, "monthly_costs": [], "annual_total": None}

    tier = tier_row["tier"]
    ndc = tier_row["ndc"]

    cost_row = conn.execute("""
        SELECT cost_type_pref, cost_amt_pref, ded_applies
        FROM beneficiary_cost
        WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
        ORDER BY coverage_level ASC LIMIT 1
    """, (contract_id, plan_id_padded, tier)).fetchone()

    if not cost_row:
        return {"tier": tier, "covered": True, "monthly_costs": [], "annual_total": None}

    cost_type = cost_row["cost_type_pref"]
    cost_amt = cost_row["cost_amt_pref"]
    ded_applies = cost_row["ded_applies"]

    pricing_row = conn.execute("""
        SELECT unit_cost FROM pricing
        WHERE contract_id = ? AND plan_id = ? AND ndc = ? AND days_supply = 30
        LIMIT 1
    """, (contract_id, plan_id_padded, ndc)).fetchone()
    unit_cost = pricing_row["unit_cost"] if pricing_row else None

    monthly_costs = []
    deductible_remaining = deductible

    for month_num in months_remaining:
        month_name = datetime(2026, month_num, 1).strftime("%B")
        if ded_applies == "N" or deductible_remaining <= 0:
            if cost_type == 0:
                monthly_cost = 0.0
            elif cost_type == 1:
                monthly_cost = float(cost_amt)
            elif cost_type == 2:
                monthly_cost = round((unit_cost or 0) * float(cost_amt), 2)
            else:
                monthly_cost = float(cost_amt)
        else:
            if unit_cost:
                if unit_cost >= deductible_remaining:
                    monthly_cost = round(deductible_remaining + (float(cost_amt) if cost_type == 1 else 0), 2)
                    deductible_remaining = 0
                else:
                    monthly_cost = round(unit_cost, 2)
                    deductible_remaining -= unit_cost
            else:
                monthly_cost = float(cost_amt)
                deductible_remaining = 0
        monthly_costs.append({"month": month_name, "cost": monthly_cost})

    annual_total = round(sum(m["cost"] for m in monthly_costs), 2)
    return {
        "tier": tier, "covered": True, "ndc": ndc,
        "monthly_costs": monthly_costs, "annual_total": annual_total,
        "steady_state_copay": float(cost_amt) if cost_type == 1 else None,
    }


def compute_drug_costs(drugs_input, zip_code, soa_date, dosages_input=""):
    """Core logic — shared by /drug-costs and /html-to-pdf"""
    drugs = parse_drugs(drugs_input, dosages_input)
    if not drugs:
        return None, "No drugs provided"

    months_remaining = get_remaining_months(soa_date)
    month_names = [datetime(2026, m, 1).strftime("%B") for m in months_remaining]

    conn = get_db()
    plan_details = {}
    for plan in PLANS:
        row = conn.execute("SELECT * FROM plans WHERE contract_id = ? AND plan_id = ?",
                           (plan["contract_id"], plan["plan_id"].zfill(3))).fetchone()
        if row:
            plan_details[plan["carrier"]] = {
                "carrier": plan["carrier"], "plan_type": plan["type"],
                "plan_name": row["plan_name"], "formulary_id": row["formulary_id"],
                "contract_id": plan["contract_id"], "plan_id": plan["plan_id"],
                "premium": row["premium"], "deductible": row["deductible"],
            }

    results = []
    for drug in drugs:
        drug_name = drug["name"]
        rxcuis = lookup_rxcuis(drug_name, drug.get("dosage", ""))
        drug_result = {"drug_name": drug_name, "dosage": drug["dosage"], "plans": {}}
        if not rxcuis:
            drug_result["error"] = "Not found"
            results.append(drug_result)
            continue
        for carrier, plan in plan_details.items():
            drug_result["plans"][carrier] = get_drug_cost_for_plan(
                conn, plan["formulary_id"], plan["contract_id"],
                plan["plan_id"], rxcuis, plan["deductible"], months_remaining)
        results.append(drug_result)

    plan_summaries = {}
    for carrier, plan in plan_details.items():
        total_drug_cost = 0
        all_covered = True
        for dr in results:
            pc = dr["plans"].get(carrier, {})
            if pc.get("annual_total") is not None:
                total_drug_cost += pc["annual_total"]
            else:
                all_covered = False
        premium_annual = round(plan["premium"] * len(months_remaining), 2)
        plan_summaries[carrier] = {
            "plan_name": plan["plan_name"], "plan_type": plan["plan_type"],
            "premium_monthly": plan["premium"], "premium_remaining_year": premium_annual,
            "deductible": plan["deductible"],
            "total_drug_cost": round(total_drug_cost, 2),
            "total_drug_plus_premium": round(total_drug_cost + premium_annual, 2),
            "all_drugs_covered": all_covered,
        }

    conn.close()
    return {
        "zip_code": zip_code, "soa_date": soa_date,
        "months_remaining": month_names,
        "plan_summaries": plan_summaries, "drug_detail": results,
    }, None




def build_pdf(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining):
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io
    from datetime import datetime

    # ── Color palette ──────────────────────────────────────────────────
    CHARCOAL   = colors.HexColor("#1c1917")
    TEAL       = colors.HexColor("#0d9488")
    TEAL_LIGHT = colors.HexColor("#ccfbf1")
    LIGHT_GRAY = colors.HexColor("#f8fafc")
    MID_GRAY   = colors.HexColor("#e2e8f0")
    DARK_GRAY  = colors.HexColor("#292524")
    WHITE      = colors.white
    GREEN_BG   = colors.HexColor("#dcfce7")
    GREEN_TEXT = colors.HexColor("#166534")
    BLUE_BG    = colors.HexColor("#dbeafe")
    BLUE_TEXT  = colors.HexColor("#1e40af")
    AMBER_BG   = colors.HexColor("#fef9c3")
    AMBER_TEXT = colors.HexColor("#854d0e")
    RED_BG     = colors.HexColor("#fee2e2")
    RED_TEXT   = colors.HexColor("#991b1b")

    def S(name, **kw):
        defaults = dict(fontName="Helvetica", fontSize=8, textColor=DARK_GRAY, leading=10)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    h1        = S("h1",  fontSize=13, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=20)
    h2        = S("h2",  fontSize=6,  textColor=colors.HexColor("#64748b"), leading=10)
    sec_title = S("sec", fontSize=8,  textColor=CHARCOAL, fontName="Helvetica-Bold", leading=12)
    col_hdr   = S("ch",  fontSize=6,  textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=9)
    row_lbl   = S("rl",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
    cell      = S("c",   fontSize=6,  textColor=DARK_GRAY, alignment=TA_CENTER, leading=9)
    badge_txt = S("bt",  fontSize=6,  textColor=colors.HexColor("#dc2626"), fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=8)
    gen_txt   = S("gt",  fontSize=6,  textColor=colors.HexColor("#64748b"), alignment=TA_RIGHT, leading=8)
    footer    = S("ft",  fontSize=6,  textColor=colors.HexColor("#94a3b8"), alignment=TA_CENTER, leading=8)
    drug_lbl  = S("dl",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
    nc_style  = S("nc",  fontSize=6,  textColor=colors.HexColor("#dc2626"), alignment=TA_CENTER, leading=8)
    green_val = S("gv",  fontSize=6,  textColor=GREEN_TEXT, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=9)
    bold_cell = S("bc",  fontSize=6,  textColor=CHARCOAL, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=9)
    month_lbl = S("ml",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
    ph_hdr    = S("ph",  fontSize=6,  textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=9)

    def tier_badge(tier):
        configs = {
            1: ("#166534", GREEN_BG,   "Tier 1"),
            2: ("#1e40af", BLUE_BG,    "Tier 2"),
            3: ("#854d0e", AMBER_BG,   "Tier 3"),
            4: ("#991b1b", RED_BG,     "Tier 4"),
        }
        color, bg, label = configs.get(tier, ("#334155", WHITE, f"Tier {tier}"))
        return Paragraph(f'<font color="{color}">{label}</font>',
                         S(f"t{tier}", fontSize=6, fontName="Helvetica-Bold",
                           alignment=TA_CENTER, textColor=colors.HexColor(color), leading=8))

    def tier_bg(tier):
        return {1: GREEN_BG, 2: BLUE_BG, 3: AMBER_BG, 4: RED_BG}.get(tier, WHITE)

    def best_plan(plans):
        """Pick best plan: lowest total_drug_plus_premium, tiebreak by lowest deductible."""
        def score(c):
            return (plans[c].get("total_drug_plus_premium", 9999),
                    plans[c].get("deductible", 9999))
        return min(plans.keys(), key=score)

    def clean_plan_name(name):
        for s in ["(PPO)", "(HMO-POS)", "(HMO)", "(PDP)", "(PFFS)"]:
            name = name.replace(s, "")
        return name.strip()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                            rightMargin=8*mm, leftMargin=8*mm,
                            topMargin=6*mm, bottomMargin=6*mm)
    elements = []

    # ══════════════════════════════════════════════════════════════════
    # HEADER
    # ══════════════════════════════════════════════════════════════════
    header_left = [[Paragraph(client_name, h1)],
                   [Paragraph(f"DOB: {dob}  ·  Zip: {zip_code}  ·  SOA Date: {soa_date}", h2)]]
    header_right = [[Paragraph("INTERNAL USE ONLY", badge_txt)],
                    [Paragraph(f"Generated: {datetime.today().strftime('%m/%d/%Y')}", gen_txt)],
                    [Paragraph("Data: CMS Medicare Formulary Q1 2026", gen_txt)]]
    tl = Table([[Table(header_left, colWidths=[200*mm]),
                 Table(header_right, colWidths=[80*mm])]],
               colWidths=[200*mm, 80*mm])
    tl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))
    elements.append(tl)
    elements.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=2*mm))

    ma_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "MA"}
    pd_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "PD"}

    def make_plan_table(plans, section_label):
        carriers = list(plans.keys())
        best = best_plan(plans)
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)

        def carrier_header(c):
            plan = plans[c]
            name = clean_plan_name(plan["plan_name"])
            if len(name) > 30:
                # Split at space near middle
                mid = len(name) // 2
                split = name.rfind(" ", 0, mid + 10)
                if split > 0:
                    name = name[:split] + "\n" + name[split+1:]
            star = " ★" if c == best else ""
            return Paragraph(f"{c}{star}<br/><font size='5'>{name}</font>", col_hdr)

        rows = []
        rows.append([Paragraph(section_label, col_hdr)] + [carrier_header(c) for c in carriers])

        data_rows = [
            ("Monthly Premium",           lambda c: f"${plans[c]['premium_monthly']:.2f}"),
            ("Drug Deductible",           lambda c: f"${plans[c]['deductible']:.0f}"),
            ("Est. Annual Drug Cost",     lambda c: f"${plans[c]['total_drug_cost']:.2f}"),
            ("Est. Total (Drug+Premium)", lambda c: f"${plans[c]['total_drug_plus_premium']:.2f}"),
        ]
        for label, fn in data_rows:
            is_total = "Total" in label
            lbl_style = S("rlb", fontSize=7, textColor=TEAL, fontName="Helvetica-Bold", leading=9) if is_total else row_lbl
            row = [Paragraph(label, lbl_style)]
            for c in carriers:
                val = fn(c)
                if is_total:
                    row.append(Paragraph(val, green_val if c == best else bold_cell))
                else:
                    row.append(Paragraph(val, cell))
            rows.append(row)

        col_widths = [label_w] + [col_w] * len(carriers)
        t = Table(rows, colWidths=col_widths)
        ts = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [WHITE, LIGHT_GRAY]),
            ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f0fdf4")),
            ("LINEABOVE", (0,-1), (-1,-1), 1, TEAL),
        ]
        if best in carriers:
            ci = carriers.index(best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
                ("BACKGROUND", (ci,-1), (ci,-1), GREEN_BG),
            ]
        t.setStyle(TableStyle(ts))
        return t, best

    # ══════════════════════════════════════════════════════════════════
    # SECTION 1 — MA PLAN OVERVIEW
    # ══════════════════════════════════════════════════════════════════
    if ma_plans:
        elements.append(Paragraph("SECTION 1 — MEDICARE ADVANTAGE PLAN OVERVIEW", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        t, ma_best = make_plan_table(ma_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.5*mm))

    # ══════════════════════════════════════════════════════════════════
    # SECTION 2 — DRUG FORMULARY TIERS
    # ══════════════════════════════════════════════════════════════════
    if ma_plans and drug_detail:
        elements.append(Paragraph("SECTION 2 — DRUG FORMULARY TIERS", sec_title))
        elements.append(Spacer(1, 0.5*mm))

        carriers = list(ma_plans.keys())
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)

        rows = []
        rows.append([Paragraph("Medication", S("mh", fontSize=7, textColor=WHITE, fontName="Helvetica-Bold", leading=9))] +
                    [Paragraph(c + (" ★" if c == ma_best else ""), col_hdr) for c in carriers])

        for drug in drug_detail:
            drug_name = drug.get("drug_name", "")
            dosage = drug.get("dosage", "")
            label = f"{drug_name} {dosage}".strip() if dosage else drug_name
            row = [Paragraph(label, drug_lbl)]
            for c in carriers:
                pd = drug.get("plans", {}).get(c, {})
                if not pd.get("covered", False):
                    row.append(Paragraph("Not Covered", nc_style))
                else:
                    tier = pd.get("tier")
                    row.append(tier_badge(tier) if tier else Paragraph("—", cell))
            rows.append(row)

        col_widths = [label_w] + [col_w] * len(carriers)
        t = Table(rows, colWidths=col_widths)
        ts = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        ]
        for ri, drug in enumerate(drug_detail, start=1):
            for ci, c in enumerate(carriers, start=1):
                pd = drug.get("plans", {}).get(c, {})
                tier = pd.get("tier")
                if tier and pd.get("covered"):
                    ts.append(("BACKGROUND", (ci, ri), (ci, ri), tier_bg(tier)))
        if ma_best in carriers:
            ci = carriers.index(ma_best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
            ]
        t.setStyle(TableStyle(ts))
        elements.append(t)
        elements.append(Spacer(1, 0.5*mm))

    # ══════════════════════════════════════════════════════════════════
    # SECTION 3 — MONTHLY DRUG COST BREAKDOWN (all MA plans)
    # ══════════════════════════════════════════════════════════════════
    if ma_plans and drug_detail and months_remaining:
        carriers = list(ma_plans.keys())
        elements.append(Paragraph("SECTION 3 — ESTIMATED MONTHLY TOTAL DRUG COST BY PLAN", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        elements.append(Paragraph(
            "Monthly totals shown at preferred retail pharmacy. Costs may vary by deductible phase.",
            S("note", fontSize=6, textColor=colors.HexColor("#64748b"), leading=8)))
        elements.append(Spacer(1, 0.5*mm))

        month_col_w = 22*mm
        col_w = (274*mm - month_col_w) / len(carriers)

        # Header row
        hdr_row = [Paragraph("Month", ph_hdr)] + [
            Paragraph(c + (" ★" if c == ma_best else ""), ph_hdr) for c in carriers
        ]
        rows = [hdr_row]

        # Monthly rows
        for month in months_remaining:
            row = [Paragraph(month, month_lbl)]
            for c in carriers:
                monthly_total = 0
                for drug in drug_detail:
                    plan_costs = drug.get("plans", {}).get(c, {})
                    month_costs = plan_costs.get("monthly_costs", [])
                    cost = next((m["cost"] for m in month_costs if m["month"] == month), 0)
                    monthly_total += cost or 0
                row.append(Paragraph(f"${monthly_total:.2f}", cell))
            rows.append(row)

        # Annual total row
        annual_row = [Paragraph("Annual Total", S("at", fontSize=7, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=9))]
        for c in carriers:
            grand_total = sum(
                (drug.get("plans", {}).get(c, {}).get("annual_total") or 0)
                for drug in drug_detail
            )
            annual_row.append(Paragraph(f"${grand_total:.2f}",
                S("av", fontSize=7, textColor=CHARCOAL, fontName="Helvetica-Bold",
                  alignment=TA_CENTER, leading=9)))
        rows.append(annual_row)

        col_widths = [month_col_w] + [col_w] * len(carriers)
        t = Table(rows, colWidths=col_widths)
        ts = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [WHITE, LIGHT_GRAY]),
            ("BACKGROUND", (0,-1), (-1,-1), LIGHT_GRAY),
            ("LINEABOVE", (0,-1), (-1,-1), 1, TEAL),
        ]
        # Highlight recommended plan column
        if ma_best in carriers:
            ci = carriers.index(ma_best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
            ]
        t.setStyle(TableStyle(ts))
        elements.append(t)
        elements.append(Spacer(1, 0.5*mm))

    # ══════════════════════════════════════════════════════════════════
    # SECTION 4 — PART D STANDALONE
    # ══════════════════════════════════════════════════════════════════
    if pd_plans:
        elements.append(Paragraph("SECTION 4 — PART D STANDALONE PLANS", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        t, _ = make_plan_table(pd_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.5*mm))

    # ══════════════════════════════════════════════════════════════════
    # FOOTER
    # ══════════════════════════════════════════════════════════════════
    elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY,
                                spaceBefore=1*mm, spaceAfter=1*mm))
    elements.append(Paragraph(
        "Internal Use Only — Not for Distribution  |  Generated for agent reference only  |  "
        "Data sourced from CMS Medicare Formulary Files Q1 2026  |  "
        "Verify current pricing before presenting to client",
        footer))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()



@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": os.path.exists(DB_PATH)})


@app.route("/drug-costs", methods=["POST"])
def drug_costs():
    data = request.get_json(force=True, silent=True) or {}
    result, err = compute_drug_costs(
        data.get("drugs", ""),
        data.get("zip_code", "55441"),
        data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify(result)


@app.route("/html-to-pdf", methods=["POST"])
def html_to_pdf():
    """
    Accepts simple flat fields from Make, computes drug costs internally,
    builds and returns a PDF. No nested JSON arrays needed from Make.
    """
    data = request.get_json(force=True, silent=True) or {}

    client_name = data.get("client_name", "Client")
    dob = data.get("dob", "")
    zip_code = data.get("zip_code", "55441")
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))

    # Accept raw_json from Claude module output and parse drugs from it
    import json as _json
    raw_json = data.get("raw_json", "")
    drugs_input = data.get("drugs", "")
    dosages_input = data.get("dosages", "")

    if raw_json:
        try:
            parsed = _json.loads(raw_json)
            drugs_list = parsed.get("drugs", [])
            if drugs_list:
                drugs_input = ",".join(d.get("name", "") for d in drugs_list)
                dosages_input = ",".join(d.get("dosage", "") or "" for d in drugs_list)
        except Exception:
            pass

    result, err = compute_drug_costs(drugs_input, zip_code, soa_date, dosages_input)
    if err:
        return jsonify({"error": err}), 400

    pdf_bytes = build_pdf(
        client_name, dob, zip_code, soa_date,
        result["plan_summaries"],
        result["drug_detail"],
        result["months_remaining"]
    )

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={client_name.replace(' ', '_')}_Drug_Comparison.pdf"}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
