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


def parse_drugs(drugs_input):
    if isinstance(drugs_input, list):
        result = []
        for d in drugs_input:
            if isinstance(d, dict):
                result.append({"name": d.get("name", "").strip(), "dosage": d.get("dosage", "").strip()})
            elif isinstance(d, str):
                result.append({"name": d.strip(), "dosage": ""})
        return result
    elif isinstance(drugs_input, str):
        return [{"name": p.strip(), "dosage": ""} for p in drugs_input.split(",") if p.strip()]
    return []


def lookup_rxcuis(drug_name):
    rxcuis = []
    try:
        url = f"https://rxnav.nlm.nih.gov/REST/drugs.json?name={requests.utils.quote(drug_name)}"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        for group in data.get("drugGroup", {}).get("conceptGroup", []):
            if group.get("tty", "") in ["SCD", "SBD", "GPCK", "BPCK"]:
                for concept in group.get("conceptProperties", []):
                    rxcuis.append(concept["rxcui"])
    except Exception:
        pass
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


def compute_drug_costs(drugs_input, zip_code, soa_date):
    """Core logic — shared by /drug-costs and /html-to-pdf"""
    drugs = parse_drugs(drugs_input)
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
        rxcuis = lookup_rxcuis(drug_name)
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
    """Build PDF using ReportLab from structured drug cost data."""
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import io

    NAVY = colors.HexColor("#1a3a5c")
    GREEN = colors.HexColor("#92D050")
    LIGHT_BLUE = colors.HexColor("#e8f0fe")

    title_style = ParagraphStyle("title", fontSize=11, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica-Bold")
    header_style = ParagraphStyle("hdr", fontSize=7, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica-Bold")
    cell_style = ParagraphStyle("cell", fontSize=7, alignment=TA_CENTER, fontName="Helvetica")
    label_style = ParagraphStyle("lbl", fontSize=7, alignment=TA_LEFT, fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("ftr", fontSize=6, textColor=colors.grey, alignment=TA_CENTER)
    nc_style = ParagraphStyle("nc", fontSize=7, textColor=colors.red, alignment=TA_CENTER)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                            rightMargin=8*mm, leftMargin=8*mm,
                            topMargin=8*mm, bottomMargin=8*mm)
    elements = []

    # Header banner
    header_data = [[
        Paragraph(f"Medicare Drug Comparison — {client_name}", title_style),
        Paragraph(f"DOB: {dob}  |  ZIP: {zip_code}  |  SOA Date: {soa_date}  |  Generated: {datetime.today().strftime('%m/%d/%Y')}", header_style),
        Paragraph("⚠ INTERNAL USE ONLY", ParagraphStyle("badge", fontSize=8, textColor=colors.HexColor("#FFD700"), alignment=TA_CENTER, fontName="Helvetica-Bold"))
    ]]
    ht = Table(header_data, colWidths=[110*mm, 130*mm, 50*mm])
    ht.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    elements.append(ht)
    elements.append(Spacer(1, 3*mm))

    def build_plan_table(plans_dict, plan_type_label):
        carriers = list(plans_dict.keys())
        if not carriers:
            return None
        lowest = min(carriers, key=lambda c: plans_dict[c].get("total_drug_plus_premium", 9999))

        label_w = 48*mm
        col_w = (290*mm - label_w) / len(carriers)

        rows = []
        # Header row
        rows.append([Paragraph(plan_type_label, header_style)] +
                    [Paragraph(c, header_style) for c in carriers])
        # Plan name
        rows.append([Paragraph("Plan Name", label_style)] +
                    [Paragraph(plans_dict[c]["plan_name"].replace("(PPO)","").replace("(HMO-POS)","").replace("(PDP)","").strip(), cell_style) for c in carriers])
        # Premium
        rows.append([Paragraph("Monthly Premium", label_style)] +
                    [Paragraph(f"${plans_dict[c]['premium_monthly']:.2f}", cell_style) for c in carriers])
        # Deductible
        rows.append([Paragraph("Drug Deductible", label_style)] +
                    [Paragraph(f"${plans_dict[c]['deductible']:.0f}", cell_style) for c in carriers])

        # Each drug
        for drug in drug_detail:
            drug_name = drug.get("drug_name", "")
            row = [Paragraph(drug_name, label_style)]
            for c in carriers:
                pd = drug.get("plans", {}).get(c, {})
                if not pd.get("covered", False):
                    row.append(Paragraph("Not Covered", nc_style))
                else:
                    tier = pd.get("tier", "")
                    copay = pd.get("steady_state_copay")
                    if copay is not None:
                        row.append(Paragraph(f"${copay:.2f} (T{tier})", cell_style))
                    else:
                        annual = pd.get("annual_total")
                        n = len(months_remaining) or 1
                        row.append(Paragraph(f"~${annual/n:.2f} (T{tier})", cell_style) if annual else Paragraph(f"T{tier}", cell_style))
            rows.append(row)

        # Total drug cost
        rows.append([Paragraph("Total Drug Cost (yr)", label_style)] +
                    [Paragraph(f"${plans_dict[c]['total_drug_cost']:.2f}", cell_style) for c in carriers])
        # Total drug + premium — highlight lowest
        total_row = [Paragraph("Total Drug + Premium ★", ParagraphStyle("tb", fontSize=7, fontName="Helvetica-Bold"))]
        for c in carriers:
            s = ParagraphStyle("gbold", fontSize=7, alignment=TA_CENTER, fontName="Helvetica-Bold") if c == lowest else cell_style
            total_row.append(Paragraph(f"${plans_dict[c]['total_drug_plus_premium']:.2f}", s))
        rows.append(total_row)

        col_widths = [label_w] + [col_w] * len(carriers)
        t = Table(rows, colWidths=col_widths)

        ts = [
            ("BACKGROUND", (0,0), (-1,0), NAVY),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("GRID", (0,0), (-1,-1), 0.3, colors.grey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, LIGHT_BLUE]),
            ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#e8f5e9")),
        ]
        if lowest in carriers:
            ci = carriers.index(lowest) + 1
            ts.append(("BACKGROUND", (ci, -1), (ci, -1), GREEN))
        t.setStyle(TableStyle(ts))
        return t

    ma_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "MA"}
    pd_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "PD"}

    if ma_plans:
        elements.append(Paragraph("Section 1 — Medicare Advantage Plans",
                                   ParagraphStyle("sec", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)))
        elements.append(Spacer(1, 2*mm))
        t = build_plan_table(ma_plans, "Plan Feature")
        if t:
            elements.append(t)
        elements.append(Spacer(1, 4*mm))

    if pd_plans:
        elements.append(Paragraph("Section 2 — Part D Standalone Plans",
                                   ParagraphStyle("sec2", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)))
        elements.append(Spacer(1, 2*mm))
        t = build_plan_table(pd_plans, "Plan Feature")
        if t:
            elements.append(t)
        elements.append(Spacer(1, 4*mm))

    elements.append(Paragraph(
        "Internal Use Only — Not for Distribution | Generated for agent reference only | Data sourced from CMS Medicare formulary files Q1 2026",
        footer_style))

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
    drugs_input = data.get("drugs", "")

    result, err = compute_drug_costs(drugs_input, zip_code, soa_date)
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
