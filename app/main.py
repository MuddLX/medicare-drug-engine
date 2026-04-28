"""
Medicare Drug Cost API
Receives drug names + zip code from Make.com
Returns cost comparison across 7 MN plans (5 MA + 2 Part D)

Endpoint: POST /drug-costs
Body: {
    "drugs": [{"name": "Metformin", "dosage": "500mg"}, ...],
    "zip_code": "55441",
    "soa_date": "05/03/2026"
}
"""

from flask import Flask, request, jsonify
import sqlite3
import os
from datetime import datetime, date
import requests

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "medicare_mn.db")

# MN plans config — update each AEP season
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


def lookup_rxcuis(drug_name):
    """
    Returns a list of product-level RXCUIs (SCD/SBD) for a drug name.
    These match what the CMS formulary file stores.
    Falls back to ingredient RXCUI if nothing found.
    """
    rxcuis = []
    try:
        url = f"https://rxnav.nlm.nih.gov/REST/drugs.json?name={requests.utils.quote(drug_name)}"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        groups = data.get("drugGroup", {}).get("conceptGroup", [])
        for group in groups:
            tty = group.get("tty", "")
            if tty in ["SCD", "SBD", "GPCK", "BPCK"]:
                for concept in group.get("conceptProperties", []):
                    rxcuis.append(concept["rxcui"])
    except Exception:
        pass

    # Fallback: ingredient-level RXCUI
    if not rxcuis:
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={requests.utils.quote(drug_name)}&search=2"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            ids = data.get("idGroup", {}).get("rxnormId", [])
            rxcuis = list(ids)
        except Exception:
            pass

    return rxcuis


def get_drug_cost_for_plan(conn, formulary_id, contract_id, plan_id, rxcuis, deductible, months_remaining):
    """
    Calculate monthly drug costs for a specific drug on a specific plan.
    rxcuis is now a LIST — we check all of them against the formulary.
    """
    plan_id_padded = plan_id.zfill(3)

    # Step 1: Find tier — check all rxcuis, take lowest tier found
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

    # Step 2: Get copay for this tier (30-day supply, preferred retail)
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

    # Step 3: Get unit cost for deductible phase math
    pricing_row = conn.execute("""
        SELECT unit_cost FROM pricing
        WHERE contract_id = ? AND plan_id = ? AND ndc = ? AND days_supply = 30
        LIMIT 1
    """, (contract_id, plan_id_padded, ndc)).fetchone()

    unit_cost = pricing_row["unit_cost"] if pricing_row else None

    # Step 4: Calculate monthly costs
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
                drug_cost_this_month = unit_cost
                if drug_cost_this_month >= deductible_remaining:
                    monthly_cost = round(deductible_remaining + (float(cost_amt) if cost_type == 1 else 0), 2)
                    deductible_remaining = 0
                else:
                    monthly_cost = round(drug_cost_this_month, 2)
                    deductible_remaining -= drug_cost_this_month
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
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    drugs = data.get("drugs", [])
    zip_code = data.get("zip_code", "55441")
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))

    if not drugs:
        return jsonify({"error": "No drugs provided"}), 400

    months_remaining = get_remaining_months(soa_date)
    month_names = [datetime(2026, m, 1).strftime("%B") for m in months_remaining]

    conn = get_db()

    # Load plan details
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

    # Process each drug
    results = []
    for drug in drugs:
        drug_name = drug.get("name", "").strip()
        dosage = drug.get("dosage", "").strip()

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
            cost_data = get_drug_cost_for_plan(
                conn,
                plan["formulary_id"],
                plan["contract_id"],
                plan["plan_id"],
                rxcuis,
                plan["deductible"],
                months_remaining
            )
            drug_result["plans"][carrier] = cost_data

        results.append(drug_result)

    # Build plan summary
    plan_summaries = {}
    for carrier, plan in plan_details.items():
        total_drug_cost = 0
        all_covered = True
        for drug_result in results:
            plan_cost = drug_result["plans"].get(carrier, {})
            if plan_cost.get("annual_total") is not None:
                total_drug_cost += plan_cost["annual_total"]
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
