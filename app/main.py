"""
Medicare Drug Cost API v5
Endpoints:
- POST /process-soa: accepts flat fields from Make, normalizes drugs via Claude,
  looks up drug costs, returns PDF report
- POST /drug-costs: JSON endpoint for testing
- GET /health: health check
"""

from flask import Flask, request, jsonify, Response
import sqlite3
import os
import json
import base64
import requests
import re
from datetime import datetime, date

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "medicare_mn.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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


def normalize_drugs(drugs):
    """
    Uses Claude API to normalize drug names before RxNav lookup.
    Handles misspellings, generic/brand confusion, nicknames like 'water pill'.
    Returns list of {"original": str, "normalized": str, "dosage": str, "confidence": float, "flag": str}
    """
    if not drugs:
        return []

    drug_list = "\n".join([
        f"- {d.get('name', '')} {d.get('dosage', '')}".strip()
        for d in drugs if d.get('name', '').strip()
    ])

    if not drug_list:
        return []

    prompt = f"""You are a Medicare drug formulary expert. I have a list of medications from a handwritten Medicare SOA form. Some may be misspelled, use nicknames, generic names, or brand names.

For each drug, return the most commonly used name in Medicare Part D formularies.

Drug list:
{drug_list}

CRITICAL: Return only a valid JSON array. No explanation, no markdown, no code blocks. Start with [ and end with ].

Return this exact format:
[
  {{
    "original": "exact name as written",
    "normalized": "correct formulary name",
    "dosage": "dosage if provided or empty string",
    "confidence": 0.95,
    "flag": "any concern or empty string",
    "is_injectable": false
  }}
]

Rules:
- Fix misspellings (Xarelts -> Xarelto)
- Map nicknames (water pill -> Furosemide, blood thinner -> use context or flag)
- Map generics to their most common formulary name
- Keep dosage separate from name
- If completely unrecognizable, set confidence below 0.5 and explain in flag
- Never guess wildly — if unsure set confidence low and flag it
- Set is_injectable to true if the drug is administered by injection, infusion, or subcutaneous pen (e.g. insulin, biologics, vaccines, IV drugs). Set false for oral tablets, capsules, patches, drops, and topical creams."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"].strip()
        return json.loads(text)
    except Exception:
        # If normalization fails, return originals unchanged
        return [{"original": d.get("name", ""), "normalized": d.get("name", ""),
                 "dosage": d.get("dosage", ""), "confidence": 1.0, "flag": ""} for d in drugs]


def lookup_rxcuis(drug_name, dosage=""):
    """Look up product-level RXCUIs, trying name+dosage first then name only."""
    def fetch(search_str):
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

    rxcuis = fetch(f"{drug_name} {dosage}") if dosage else []
    if not rxcuis:
        rxcuis = fetch(drug_name)
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

    for month_num in months_remaining:
        month_name = datetime(2026, month_num, 1).strftime("%B")
        # CMS pricing data contains negotiated plan costs, not retail drug prices,
        # so we cannot accurately calculate deductible phase costs. Show flat cost.
        if cost_type == 0:
            # cost_type=0 means cost is based on unit_cost from pricing table (no fixed copay)
            monthly_cost = round(unit_cost, 2) if unit_cost else 0.0
        elif cost_type == 1:
            # cost_type=1 means fixed copay amount
            monthly_cost = float(cost_amt)
        elif cost_type == 2:
            # cost_type=2 means coinsurance (% of unit_cost)
            monthly_cost = round((unit_cost or 0) * float(cost_amt), 2)
        else:
            monthly_cost = round(unit_cost, 2) if unit_cost else float(cost_amt)
        monthly_costs.append({"month": month_name, "cost": monthly_cost})

    annual_total = round(sum(m["cost"] for m in monthly_costs), 2)
    return {
        "tier": tier, "covered": True, "ndc": ndc,
        "monthly_costs": monthly_costs, "annual_total": annual_total,
        "steady_state_copay": float(cost_amt) if cost_type == 1 else None,
        "ded_applies": ded_applies,
    }


def compute_drug_costs(drugs, zip_code, soa_date):
    """
    drugs: list of {"name": str, "dosage": str}
    Normalizes drug names via Claude first, then looks up costs.
    """
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

    # Normalize drug names via Claude
    normalized = normalize_drugs(drugs)

    results = []
    warnings = []

    for item in normalized:
        drug_name = item.get("normalized", "").strip()
        original_name = item.get("original", "").strip()
        dosage = item.get("dosage", "").strip()
        flag = item.get("flag", "")
        norm_confidence = item.get("confidence", 1.0)
        is_injectable = item.get("is_injectable", False)

        if not drug_name:
            continue

        # INJECTABLE EXCLUSION — skip cost lookup, mark for display only
        # To enable injectables in the future, remove or comment out this block
        if is_injectable:
            drug_result = {
                "drug_name": drug_name,
                "original_name": original_name,
                "dosage": dosage,
                "flag": flag,
                "normalization_confidence": norm_confidence,
                "is_injectable": True,
                "plans": {}
            }
            results.append(drug_result)
            continue

        # Collect warnings for low confidence normalizations
        if norm_confidence < 0.7 or flag:
            warnings.append({
                "drug": original_name,
                "normalized_to": drug_name,
                "flag": flag or f"Low normalization confidence ({norm_confidence:.0%})"
            })

        rxcuis = lookup_rxcuis(drug_name, dosage)

        drug_result = {
            "drug_name": drug_name,
            "original_name": original_name,
            "dosage": dosage,
            "flag": flag,
            "normalization_confidence": norm_confidence,
            "plans": {}
        }

        if not rxcuis:
            drug_result["error"] = "Drug not found in formulary"
            warnings.append({
                "drug": original_name,
                "normalized_to": drug_name,
                "flag": "Not found in RxNav — verify drug name"
            })
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
        "plan_summaries": plan_summaries,
        "drug_detail": results,
        "warnings": warnings,
    }


def build_pdf(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining, confidence=None, warnings=None):
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io

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
    WARN_BG    = colors.HexColor("#fff7ed")
    WARN_TEXT  = colors.HexColor("#9a3412")

    def S(name, **kw):
        defaults = dict(fontName="Helvetica", fontSize=8, textColor=DARK_GRAY, leading=10)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    h1        = S("h1",  fontSize=11, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=13)
    h2        = S("h2",  fontSize=6,  textColor=colors.HexColor("#64748b"), leading=7)
    sec_title = S("sec", fontSize=8,  textColor=CHARCOAL, fontName="Helvetica-Bold", leading=10)
    col_hdr   = S("ch",  fontSize=6,  textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)
    row_lbl   = S("rl",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=8)
    cell      = S("c",   fontSize=6,  textColor=DARK_GRAY, alignment=TA_CENTER, leading=8)
    badge_txt = S("bt",  fontSize=6,  textColor=colors.HexColor("#dc2626"), fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=8)
    gen_txt   = S("gt",  fontSize=6,  textColor=colors.HexColor("#64748b"), alignment=TA_RIGHT, leading=8)
    footer    = S("ft",  fontSize=6,  textColor=colors.HexColor("#94a3b8"), alignment=TA_CENTER, leading=8)
    drug_lbl  = S("dl",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=8)
    nc_style  = S("nc",  fontSize=6,  textColor=colors.HexColor("#dc2626"), alignment=TA_CENTER, leading=8)
    green_val = S("gv",  fontSize=6,  textColor=GREEN_TEXT, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)
    bold_cell = S("bc",  fontSize=6,  textColor=CHARCOAL, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)
    month_lbl = S("ml",  fontSize=6,  textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=8)
    ph_hdr    = S("ph",  fontSize=6,  textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)
    warn_s    = S("ws",  fontSize=6,  textColor=WARN_TEXT, leading=8)

    def tier_badge(tier):
        configs = {
            1: ("#166534", "Tier 1"),
            2: ("#1e40af", "Tier 2"),
            3: ("#854d0e", "Tier 3"),
            4: ("#991b1b", "Tier 4"),
        }
        color, label = configs.get(tier, ("#334155", f"Tier {tier}"))
        return Paragraph(f'<font color="{color}">{label}</font>',
                         S(f"t{tier}", fontSize=6, fontName="Helvetica-Bold",
                           alignment=TA_CENTER, textColor=colors.HexColor(color), leading=8))

    def tier_bg(tier):
        return {1: GREEN_BG, 2: BLUE_BG, 3: AMBER_BG, 4: RED_BG}.get(tier, WHITE)

    def best_plan(plans):
        def score(c):
            return (plans[c].get("total_drug_plus_premium", 9999),
                    plans[c].get("deductible", 9999))
        return min(plans.keys(), key=score)

    def clean_name(name):
        for s in ["(PPO)", "(HMO-POS)", "(HMO)", "(PDP)", "(PFFS)"]:
            name = name.replace(s, "")
        return name.strip()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                            rightMargin=8*mm, leftMargin=8*mm,
                            topMargin=2*mm, bottomMargin=6*mm)
    elements = []

    # Header — compact single-row layout
    conf_text = f"Extraction confidence: {confidence:.0%}" if confidence else ""
    # Left: name on one line, details on the next
    header_left = [
        [Paragraph(client_name, h1)],
        [Paragraph(f"DOB: {dob}  ·  Zip: {zip_code}  ·  SOA Date: {soa_date}", h2)],
    ]
    # Right: all meta info stacked tightly
    right_lines = [
        [Paragraph("INTERNAL USE ONLY", badge_txt)],
        [Paragraph(f"Generated: {datetime.today().strftime('%m/%d/%Y')}  ·  Data: CMS Medicare Formulary Q1 2026", gen_txt)],
    ]
    if conf_text:
        right_lines.append([Paragraph(conf_text, S("ct", fontSize=6, textColor=colors.HexColor("#0d9488"), alignment=TA_RIGHT, leading=8))])
    tl = Table([[Table(header_left, colWidths=[200*mm]),
                 Table(right_lines, colWidths=[80*mm])]],
               colWidths=[200*mm, 80*mm])
    tl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    elements.append(tl)
    elements.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceBefore=0.5*mm, spaceAfter=1*mm))

    # Warnings banner
    if warnings:
        warn_rows = [[Paragraph("⚠ Drug Verification Required", S("wh", fontSize=6, fontName="Helvetica-Bold", textColor=WARN_TEXT, leading=8)),
                      Paragraph("Please verify the following before client meeting:", warn_s)]]
        for w in warnings:
            drug = w.get("drug", "")
            normalized = w.get("normalized_to", "")
            flag = w.get("flag", "")
            note = f"{drug}"
            if normalized and normalized.lower() != drug.lower():
                note += f" → interpreted as {normalized}"
            if flag:
                note += f" — {flag}"
            warn_rows.append([Paragraph("", warn_s), Paragraph(note, warn_s)])
        wt = Table(warn_rows, colWidths=[50*mm, 230*mm])
        wt.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), WARN_BG),
            ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#fed7aa")),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("SPAN", (0,0), (0,0)),
        ]))
        elements.append(wt)
        elements.append(Spacer(1, 1*mm))

    ma_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "MA"}
    pd_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "PD"}

    def make_plan_table(plans, section_label):
        carriers = list(plans.keys())
        best = best_plan(plans)
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)

        def carrier_header(c):
            name = clean_name(plans[c]["plan_name"])
            if len(name) > 28:
                mid = len(name)//2
                split = name.rfind(" ", 0, mid+10)
                if split > 0:
                    name = name[:split] + "\n" + name[split+1:]
            star = " ★" if c == best else ""
            return Paragraph(f"{c}{star}<br/><font size='5'>{name}</font>", col_hdr)

        rows = [[Paragraph(section_label, col_hdr)] + [carrier_header(c) for c in carriers]]
        for label, fn, is_total in [
            ("Monthly Premium",           lambda c: f"${plans[c]['premium_monthly']:.2f}", False),
            ("Drug Deductible",           lambda c: f"${plans[c]['deductible']:.0f}", False),
            ("Est. Annual Drug Cost",     lambda c: f"${plans[c]['total_drug_cost']:.2f}", False),
            ("Est. Total (Drug+Premium)", lambda c: f"${plans[c]['total_drug_plus_premium']:.2f}", True),
        ]:
            lbl_s = S("rlb", fontSize=6, textColor=TEAL, fontName="Helvetica-Bold", leading=8) if is_total else row_lbl
            row = [Paragraph(label, lbl_s)]
            for c in carriers:
                val = fn(c)
                if is_total:
                    row.append(Paragraph(val, green_val if c == best else bold_cell))
                else:
                    row.append(Paragraph(val, cell))
            rows.append(row)

        t = Table(rows, colWidths=[label_w] + [col_w]*len(carriers))
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

    if ma_plans:
        elements.append(Paragraph("SECTION 1 — MEDICARE ADVANTAGE PLAN OVERVIEW", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        t, ma_best = make_plan_table(ma_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 1.5*mm))

    if ma_plans and drug_detail:
        elements.append(Paragraph("SECTION 2 — DRUG FORMULARY TIERS", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        carriers = list(ma_plans.keys())
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)
        rows = [[Paragraph("Medication", col_hdr)] +
                [Paragraph(c + (" ★" if c == ma_best else ""), col_hdr) for c in carriers]]
        for drug in drug_detail:
            name = drug.get("drug_name","")
            dosage = drug.get("dosage","")
            original = drug.get("original_name", "")
            is_injectable = drug.get("is_injectable", False)
            label = f"{name} {dosage}".strip() if dosage else name
            # Only show "(written: ...)" when the drug name genuinely differs
            # Strip dosage from original before comparing (original includes dosage, name does not)
            orig_name_only = re.sub(r'[\s,]+[\d\.]+\s*(mg|mcg|ml|units?\/ml|units?|g|iu|%|meq).*$', '', original, flags=re.IGNORECASE).strip()
            if original and orig_name_only.strip().lower() != name.strip().lower():
                label += f'<br/><font size="5" color="#64748b">(written: {original})</font>'
            if is_injectable:
                label += f'<br/><font size="5" color="#b45309">(injectable)</font>'
            row = [Paragraph(label, drug_lbl)]
            for c in carriers:
                if is_injectable:
                    # Injectable — no formulary lookup performed
                    # To enable injectable cost lookup, remove the is_injectable block in compute_drug_costs
                    row.append(Paragraph("Verify coverage", S("inj", fontSize=5, textColor=colors.HexColor("#b45309"), alignment=TA_CENTER, leading=7)))
                else:
                    pd = drug.get("plans",{}).get(c,{})
                    if not pd.get("covered", False):
                        row.append(Paragraph("Not Covered", nc_style))
                    else:
                        tier = pd.get("tier")
                        row.append(tier_badge(tier) if tier else Paragraph("—", cell))
            rows.append(row)
        t = Table(rows, colWidths=[label_w] + [col_w]*len(carriers))
        ts = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 1),
            ("BOTTOMPADDING", (0,0), (-1,-1), 1),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        ]
        for ri, drug in enumerate(drug_detail, start=1):
            for ci, c in enumerate(carriers, start=1):
                pd = drug.get("plans",{}).get(c,{})
                tier = pd.get("tier")
                if tier and pd.get("covered"):
                    ts.append(("BACKGROUND", (ci,ri), (ci,ri), tier_bg(tier)))
        if ma_best in carriers:
            ci = carriers.index(ma_best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
            ]
        t.setStyle(TableStyle(ts))
        elements.append(t)
        elements.append(Spacer(1, 1.5*mm))

    if ma_plans and drug_detail and months_remaining:
        carriers = list(ma_plans.keys())
        elements.append(Paragraph("SECTION 3 — ESTIMATED MONTHLY TOTAL DRUG COST BY PLAN (PLAN COPAY RATES)", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        elements.append(Paragraph(
            "Costs shown are plan negotiated copay rates at preferred retail pharmacy — not retail/cash prices. Costs during deductible phase and at non-preferred pharmacies may differ. Verify with plan before client meeting.",
            S("note", fontSize=5, textColor=colors.HexColor("#64748b"), leading=6)))
        elements.append(Spacer(1, 0.5*mm))
        month_col_w = 20*mm
        col_w = (274*mm - month_col_w) / len(carriers)
        rows = [[Paragraph("Month", ph_hdr)] +
                [Paragraph(c + (" ★" if c == ma_best else ""), ph_hdr) for c in carriers]]
        for month in months_remaining:
            row = [Paragraph(month, month_lbl)]
            for c in carriers:
                total = sum(
                    next((m["cost"] for m in drug.get("plans",{}).get(c,{}).get("monthly_costs",[]) if m["month"]==month), 0) or 0
                    for drug in drug_detail
                )
                row.append(Paragraph(f"${total:.2f}", cell))
            rows.append(row)
        annual_row = [Paragraph("Annual Total", S("at", fontSize=6, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=8))]
        for c in carriers:
            gt = sum((drug.get("plans",{}).get(c,{}).get("annual_total") or 0) for drug in drug_detail)
            annual_row.append(Paragraph(f"${gt:.2f}", S("av", fontSize=6, textColor=CHARCOAL, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)))
        rows.append(annual_row)
        t = Table(rows, colWidths=[month_col_w] + [col_w]*len(carriers))
        ts = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 1),
            ("BOTTOMPADDING", (0,0), (-1,-1), 1),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [WHITE, LIGHT_GRAY]),
            ("BACKGROUND", (0,-1), (-1,-1), LIGHT_GRAY),
            ("LINEABOVE", (0,-1), (-1,-1), 1, TEAL),
        ]
        if ma_best in carriers:
            ci = carriers.index(ma_best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
            ]
        t.setStyle(TableStyle(ts))
        elements.append(t)
        elements.append(Spacer(1, 1.5*mm))

    if pd_plans:
        elements.append(Paragraph("SECTION 4 — PART D STANDALONE PLANS", sec_title))
        elements.append(Spacer(1, 0.5*mm))
        t, _ = make_plan_table(pd_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 1*mm))

    elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceBefore=0.5*mm, spaceAfter=0.5*mm))
    elements.append(Paragraph(
        "Internal Use Only — Not for Distribution  |  Generated for agent reference only  |  "
        "Data sourced from CMS Medicare Formulary Files Q1 2026  |  "
        "Verify current pricing before presenting to client", footer))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": os.path.exists(DB_PATH)})


@app.route("/process-soa", methods=["POST"])
def process_soa():
    """
    Accepts flat fields from Make. Normalizes drugs via Claude,
    looks up costs, returns PDF binary.
    """
    data = request.get_json(force=True, silent=True) or {}

    client_name = data.get("client_name", "Client")
    dob = data.get("dob", "")
    zip_code = data.get("zip_code", "55441")
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))
    drug_names = data.get("drug_names", "")
    drug_dosages = data.get("drug_dosages", "")
    confidence = data.get("confidence")
    try:
        confidence = float(confidence) if confidence else None
    except Exception:
        confidence = None

    names = [n.strip() for n in drug_names.split(",") if n.strip()]
    dosages = [d.strip() for d in drug_dosages.split(",")] if drug_dosages else []
    drugs = [{"name": names[i], "dosage": dosages[i] if i < len(dosages) else ""}
             for i in range(len(names))]

    if not drugs:
        return jsonify({"error": "No drugs provided"}), 400

    try:
        result = compute_drug_costs(drugs, zip_code, soa_date)
    except Exception as e:
        return jsonify({"error": f"Drug cost computation failed: {str(e)}"}), 500

    try:
        pdf_bytes_out = build_pdf(
            client_name, dob, zip_code, soa_date,
            result["plan_summaries"],
            result["drug_detail"],
            result["months_remaining"],
            confidence=confidence,
            warnings=result.get("warnings", [])
        )
    except Exception as e:
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500

    filename = f"{client_name.replace(' ', '_')}_Drug_Comparison.pdf"
    return Response(
        pdf_bytes_out,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/drug-costs", methods=["POST"])
def drug_costs():
    """JSON endpoint for testing."""
    data = request.get_json(force=True, silent=True) or {}
    drugs_input = data.get("drugs", "")
    if isinstance(drugs_input, str):
        drugs = [{"name": n.strip(), "dosage": ""} for n in drugs_input.split(",") if n.strip()]
    elif isinstance(drugs_input, list):
        drugs = drugs_input
    else:
        drugs = []
    if not drugs:
        return jsonify({"error": "No drugs provided"}), 400
    result = compute_drug_costs(drugs, data.get("zip_code", "55441"),
                                data.get("soa_date", datetime.today().strftime("%m/%d/%Y")))
    return jsonify(result)


@app.route("/html-to-pdf", methods=["POST"])
def html_to_pdf():
    """Legacy endpoint kept for compatibility."""
    data = request.get_json(force=True, silent=True) or {}
    client_name = data.get("client_name", "Client")
    dob = data.get("dob", "")
    zip_code = data.get("zip_code", "55441")
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))
    drugs_input = data.get("drugs", "")
    if isinstance(drugs_input, str):
        drugs = [{"name": n.strip(), "dosage": ""} for n in drugs_input.split(",") if n.strip()]
    else:
        drugs = drugs_input or []
    result = compute_drug_costs(drugs, zip_code, soa_date)
    pdf_bytes = build_pdf(client_name, dob, zip_code, soa_date,
                          result["plan_summaries"], result["drug_detail"], result["months_remaining"])
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={client_name.replace(' ','_')}_Drug_Comparison.pdf"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
