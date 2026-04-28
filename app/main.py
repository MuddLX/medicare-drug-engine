"""
Medicare Drug Cost API v2
Accepts drugs as either:
- Array of objects: [{"name": "Metformin", "dosage": "500mg"}]
- Simple string: "Metformin,Lisinopril,Eliquis"
"""

from flask import Flask, request, jsonify
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
    """
    Accepts multiple formats:
    1. Array of objects: [{"name": "Metformin", "dosage": "500mg"}]
    2. Simple string: "Metformin,Lisinopril,Eliquis"
    3. String with dosages: "Metformin 500mg,Lisinopril 10mg"
    Returns list of {"name": str, "dosage": str}
    """
    if isinstance(drugs_input, list):
        result = []
        for d in drugs_input:
            if isinstance(d, dict):
                result.append({
                    "name": d.get("name", "").strip(),
                    "dosage": d.get("dosage", "").strip()
                })
            elif isinstance(d, str):
                result.append({"name": d.strip(), "dosage": ""})
        return result
    elif isinstance(drugs_input, str):
        result = []
        for part in drugs_input.split(","):
            part = part.strip()
            if part:
                result.append({"name": part, "dosage": ""})
        return result
    return []


def lookup_rxcuis(drug_name):
    rxcuis = []
    try:
        url = f"https://rxnav.nlm.nih.gov/REST/drugs.json?name={requests.utils.quote(drug_name)}"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        groups = data.get("drugGroup", {}).get("conceptGroup", [])
        for group in groups:
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
        if row:
            if tier_row is None or row["tier"] < tier_row["tier"]:
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
        "tier": tier,
        "covered": True,
        "ndc": ndc,
        "monthly_costs": monthly_costs,
        "annual_total": annual_total,
        "steady_state_copay": float(cost_amt) if cost_type == 1 else None,
    }


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": os.path.exists(DB_PATH)})


@app.route("/drug-costs", methods=["POST"])
def drug_costs():
    data = request.get_json(force=True, silent=True)
    if not data:
        # Try form data fallback
        data = request.form.to_dict()
    if not data:
        return jsonify({"error": "No data received"}), 400

    drugs_input = data.get("drugs", "")
    zip_code = data.get("zip_code", "55441")
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))

    drugs = parse_drugs(drugs_input)

    if not drugs:
        return jsonify({"error": "No drugs provided"}), 400

    months_remaining = get_remaining_months(soa_date)
    month_names = [datetime(2026, m, 1).strftime("%B") for m in months_remaining]

    conn = get_db()

    plan_details = {}
    for plan in PLANS:
        row = conn.execute("""
            SELECT * FROM plans WHERE contract_id = ? AND plan_id = ?
        """, (plan["contract_id"], plan["plan_id"].zfill(3))).fetchone()
        if row:
            plan_details[plan["carrier"]] = {
                "carrier": plan["carrier"],
                "plan_type": plan["type"],
                "plan_name": row["plan_name"],
                "formulary_id": row["formulary_id"],
                "contract_id": plan["contract_id"],
                "plan_id": plan["plan_id"],
                "premium": row["premium"],
                "deductible": row["deductible"],
            }

    results = []
    for drug in drugs:
        drug_name = drug["name"]
        dosage = drug["dosage"]
        rxcuis = lookup_rxcuis(drug_name)

        drug_result = {
            "drug_name": drug_name,
            "dosage": dosage,
            "rxcui_count": len(rxcuis),
            "plans": {}
        }

        if not rxcuis:
            drug_result["error"] = "Drug not found in RxNav"
            results.append(drug_result)
            continue

        for carrier, plan in plan_details.items():
            drug_result["plans"][carrier] = get_drug_cost_for_plan(
                conn, plan["formulary_id"], plan["contract_id"],
                plan["plan_id"], rxcuis, plan["deductible"], months_remaining
            )

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
            "plan_name": plan["plan_name"],
            "plan_type": plan["plan_type"],
            "premium_monthly": plan["premium"],
            "premium_remaining_year": premium_annual,
            "deductible": plan["deductible"],
            "total_drug_cost": round(total_drug_cost, 2),
            "total_drug_plus_premium": round(total_drug_cost + premium_annual, 2),
            "all_drugs_covered": all_covered,
        }

    conn.close()

    return jsonify({
        "zip_code": zip_code,
        "soa_date": soa_date,
        "months_remaining": month_names,
        "plan_summaries": plan_summaries,
        "drug_detail": results,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)


@app.route("/html-to-pdf", methods=["POST"])
def html_to_pdf():
    """
    Convert HTML report data to PDF using ReportLab.
    Accepts the full drug cost JSON and builds a structured PDF directly.
    """
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import io
    import json

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Accept either raw drug cost JSON or html field
    # We rebuild the PDF from structured data for best results
    client_name = data.get("client_name", "Client")
    dob = data.get("dob", "")
    zip_code = data.get("zip_code", "")
    soa_date = data.get("soa_date", "")
    plan_summaries = data.get("plan_summaries", {})
    drug_detail = data.get("drug_detail", [])
    months_remaining = data.get("months_remaining", [])

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10*mm,
        leftMargin=10*mm,
        topMargin=10*mm,
        bottomMargin=10*mm
    )

    styles = getSampleStyleSheet()
    NAVY = colors.HexColor("#1a3a5c")
    GREEN = colors.HexColor("#92D050")
    LIGHT_BLUE = colors.HexColor("#e8f0fe")
    WHITE = colors.white

    title_style = ParagraphStyle("title", fontSize=14, textColor=WHITE, alignment=TA_CENTER, fontName="Helvetica-Bold")
    header_style = ParagraphStyle("header", fontSize=8, textColor=WHITE, alignment=TA_CENTER, fontName="Helvetica-Bold")
    cell_style = ParagraphStyle("cell", fontSize=7, alignment=TA_CENTER, fontName="Helvetica")
    label_style = ParagraphStyle("label", fontSize=7, alignment=TA_LEFT, fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("footer", fontSize=6, textColor=colors.grey, alignment=TA_CENTER)

    elements = []

    # Header
    header_data = [[
        Paragraph(f"Medicare Drug Comparison Report", title_style),
        Paragraph(f"Client: {client_name} | DOB: {dob} | ZIP: {zip_code} | SOA: {soa_date}", header_style),
        Paragraph("INTERNAL USE ONLY", ParagraphStyle("badge", fontSize=8, textColor=colors.red, alignment=TA_CENTER, fontName="Helvetica-Bold"))
    ]]
    header_table = Table(header_data, colWidths=[100*mm, 130*mm, 60*mm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [NAVY]),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 4*mm))

    # Section 1: MA Plans
    ma_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "MA"}
    pd_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") == "PD"}

    if ma_plans:
        elements.append(Paragraph("Medicare Advantage Plan Comparison", ParagraphStyle("sec", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)))
        elements.append(Spacer(1, 2*mm))

        carriers = list(ma_plans.keys())
        # Find lowest total for green highlight
        lowest_carrier = min(carriers, key=lambda c: ma_plans[c].get("total_drug_plus_premium", 9999))

        col_width = 40*mm
        label_width = 50*mm

        # Build header row
        header_row = [Paragraph("Plan Feature", header_style)] + [Paragraph(c, header_style) for c in carriers]

        rows = [header_row]

        # Plan name row
        rows.append([Paragraph("Plan Name", label_style)] + [
            Paragraph(ma_plans[c]["plan_name"].replace("(PPO)", "").replace("(HMO-POS)", "").strip(), cell_style)
            for c in carriers
        ])

        # Premium
        rows.append([Paragraph("Monthly Premium", label_style)] + [
            Paragraph(f"${ma_plans[c]['premium_monthly']:.2f}", cell_style) for c in carriers
        ])

        # Deductible
        rows.append([Paragraph("Drug Deductible", label_style)] + [
            Paragraph(f"${ma_plans[c]['deductible']:.0f}", cell_style) for c in carriers
        ])

        # Each drug
        for drug in drug_detail:
            drug_name = drug.get("drug_name", "")
            row = [Paragraph(f"{drug_name}", label_style)]
            for c in carriers:
                plan_data = drug.get("plans", {}).get(c, {})
                if not plan_data.get("covered", False):
                    row.append(Paragraph("Not Covered", ParagraphStyle("nc", fontSize=7, textColor=colors.red, alignment=TA_CENTER)))
                else:
                    tier = plan_data.get("tier", "")
                    copay = plan_data.get("steady_state_copay")
                    if copay is not None:
                        row.append(Paragraph(f"${copay:.2f} (T{tier})", cell_style))
                    else:
                        annual = plan_data.get("annual_total")
                        if annual is not None and len(months_remaining) > 0:
                            monthly = annual / len(months_remaining)
                            row.append(Paragraph(f"~${monthly:.2f} (T{tier})", cell_style))
                        else:
                            row.append(Paragraph(f"T{tier}", cell_style))
            rows.append(row)

        # Total drug cost
        rows.append([Paragraph("Total Drug Cost (yr)", label_style)] + [
            Paragraph(f"${ma_plans[c]['total_drug_cost']:.2f}", cell_style) for c in carriers
        ])

        # Total drug + premium
        total_row = [Paragraph("Total Drug + Premium", ParagraphStyle("bold_label", fontSize=7, fontName="Helvetica-Bold"))]
        for c in carriers:
            style = ParagraphStyle("green_cell", fontSize=7, alignment=TA_CENTER, fontName="Helvetica-Bold") if c == lowest_carrier else cell_style
            total_row.append(Paragraph(f"${ma_plans[c]['total_drug_plus_premium']:.2f}", style))
        rows.append(total_row)

        col_widths = [label_width] + [col_width] * len(carriers)
        ma_table = Table(rows, colWidths=col_widths)

        # Build style
        ts = [
            ("BACKGROUND", (0,0), (-1,0), NAVY),
            ("TEXTCOLOR", (0,0), (-1,0), WHITE),
            ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("BACKGROUND", (0,1), (-1,1), LIGHT_BLUE),
            ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#e8f5e9")),
        ]
        # Highlight lowest column
        if lowest_carrier in carriers:
            col_idx = carriers.index(lowest_carrier) + 1
            ts.append(("BACKGROUND", (col_idx, -1), (col_idx, -1), GREEN))

        ma_table.setStyle(TableStyle(ts))
        elements.append(ma_table)
        elements.append(Spacer(1, 4*mm))

    # Section 2: Part D Plans
    if pd_plans:
        elements.append(Paragraph("Part D Standalone Plans", ParagraphStyle("sec", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)))
        elements.append(Spacer(1, 2*mm))

        carriers = list(pd_plans.keys())
        lowest_pd = min(carriers, key=lambda c: pd_plans[c].get("total_drug_plus_premium", 9999))

        header_row = [Paragraph("Plan Feature", header_style)] + [Paragraph(c, header_style) for c in carriers]
        rows = [header_row]

        rows.append([Paragraph("Plan Name", label_style)] + [
            Paragraph(pd_plans[c]["plan_name"], cell_style) for c in carriers
        ])
        rows.append([Paragraph("Monthly Premium", label_style)] + [
            Paragraph(f"${pd_plans[c]['premium_monthly']:.2f}", cell_style) for c in carriers
        ])
        rows.append([Paragraph("Drug Deductible", label_style)] + [
            Paragraph(f"${pd_plans[c]['deductible']:.0f}", cell_style) for c in carriers
        ])

        for drug in drug_detail:
            drug_name = drug.get("drug_name", "")
            row = [Paragraph(f"{drug_name}", label_style)]
            for c in carriers:
                plan_data = drug.get("plans", {}).get(c, {})
                if not plan_data.get("covered", False):
                    row.append(Paragraph("Not Covered", ParagraphStyle("nc2", fontSize=7, textColor=colors.red, alignment=TA_CENTER)))
                else:
                    tier = plan_data.get("tier", "")
                    copay = plan_data.get("steady_state_copay")
                    if copay is not None:
                        row.append(Paragraph(f"${copay:.2f} (T{tier})", cell_style))
                    else:
                        annual = plan_data.get("annual_total")
                        if annual and len(months_remaining) > 0:
                            row.append(Paragraph(f"~${annual/len(months_remaining):.2f} (T{tier})", cell_style))
                        else:
                            row.append(Paragraph(f"T{tier}", cell_style))
            rows.append(row)

        rows.append([Paragraph("Total Drug + Premium", label_style)] + [
            Paragraph(f"${pd_plans[c]['total_drug_plus_premium']:.2f}", cell_style) for c in carriers
        ])

        col_widths = [80*mm] + [80*mm] * len(carriers)
        pd_table = Table(rows, colWidths=col_widths)
        ts = [
            ("BACKGROUND", (0,0), (-1,0), NAVY),
            ("TEXTCOLOR", (0,0), (-1,0), WHITE),
            ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
        ]
        if lowest_pd in carriers:
            col_idx = carriers.index(lowest_pd) + 1
            ts.append(("BACKGROUND", (col_idx, -1), (col_idx, -1), GREEN))
        pd_table.setStyle(TableStyle(ts))
        elements.append(pd_table)
        elements.append(Spacer(1, 4*mm))

    # Footer
    elements.append(Paragraph(
        "Internal Use Only — Not for Distribution | Generated for agent reference only | Data sourced from CMS Medicare formulary files",
        footer_style
    ))

    doc.build(elements)
    buffer.seek(0)

    from flask import Response
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=drug_comparison.pdf"}
    )
