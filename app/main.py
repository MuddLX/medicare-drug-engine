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



# ===== CMS NEGOTIATED MAXIMUM FAIR PRICES (MFP) FOR 2026 =====
# These are the federally negotiated prices that plans must use for these drugs
# Source: CMS Medicare Drug Price Negotiation Program, effective January 1, 2026
# Patient pays coinsurance % × MFP (plan-specific coinsurance in beneficiary_cost table)
MFP_2026 = {
    # Drug name (lowercase) -> MFP per 30-day supply
    "apixaban": 231.00,       # Eliquis
    "eliquis": 231.00,
    "rivaroxaban": 197.00,    # Xarelto
    "xarelto": 197.00,
    "empagliflozin": 197.00,  # Jardiance
    "jardiance": 197.00,
    "sitagliptin": 113.00,    # Januvia
    "januvia": 113.00,
    "dapagliflozin": 178.50,  # Farxiga
    "farxiga": 178.50,
    "etanercept": 2355.00,    # Enbrel
    "enbrel": 2355.00,
    "ustekinumab": 4695.00,   # Stelara
    "stelara": 4695.00,
    "insulin aspart": 119.00, # NovoLog/Fiasp
    "novolog": 119.00,
    "fiasp": 119.00,
}

def get_mfp(drug_name):
    """Return CMS negotiated MFP for a drug, or None if not in program."""
    if not drug_name:
        return None
    return MFP_2026.get(drug_name.lower().strip())

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


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in miles between two lat/lon points."""
    import math
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def get_client_coords(conn, zip_code):
    """Get lat/lon for client zip code."""
    row = conn.execute(
        "SELECT lat, lon, city FROM zip_coords WHERE zip = ?", (zip_code,)
    ).fetchone()
    if row and row[0] and row[1]:
        return row[0], row[1], row[2]
    return None, None, None


def get_nearby_pharmacies(conn, contract_id, plan_id, client_zip, max_results=4, max_miles=30):
    """
    Find closest preferred retail pharmacies to client zip for a specific plan.
    Matches pharmacies by zip code proximity rather than NPI (avoids NCPDP/NPI mismatch).
    Returns list of dicts with name, address, distance, fees.
    """
    plan_id_padded = plan_id.zfill(3)

    # Get client coordinates
    client_lat, client_lon, client_city = get_client_coords(conn, client_zip)
    if not client_lat:
        return []

    # Get preferred zip codes for this plan within radius
    pref_rows = conn.execute("""
        SELECT DISTINCT pharmacy_zip, preferred_retail,
               generic_fee_30, brand_fee_30, selected_fee_30
        FROM pharmacy_network
        WHERE contract_id = ? AND plan_id = ? AND is_retail = 1
    """, (contract_id, plan_id_padded)).fetchall()

    # Build zip -> fees + preferred lookup
    zip_info = {}
    for row in pref_rows:
        pharm_zip, pref, gen_fee, brand_fee, sel_fee = row
        pharm_zip = pharm_zip.zfill(5)
        if pharm_zip not in zip_info or (pref == "Y" and zip_info[pharm_zip]["preferred"] != "Y"):
            zip_info[pharm_zip] = {
                "preferred": pref,
                "generic_fee": float(gen_fee or 0),
                "brand_fee": float(brand_fee or 0),
                "selected_fee": float(sel_fee or 0),
            }

    # Find pharmacy names in nearby zips
    candidates = []
    for pharm_zip, info in zip_info.items():
        # Get zip coordinates
        coords = conn.execute(
            "SELECT lat, lon FROM zip_coords WHERE zip = ?", (pharm_zip,)
        ).fetchone()
        if not coords or not coords[0]:
            continue

        distance = haversine_distance(client_lat, client_lon, coords[0], coords[1])
        if distance > max_miles:
            continue

        # Get pharmacies in this zip
        pharms = conn.execute("""
            SELECT npi, name, address, city, is_chain
            FROM pharmacy_names
            WHERE zip = ? AND name != 'Unknown Pharmacy'
        """, (pharm_zip,)).fetchall()

        for npi, name, address, city, is_chain in pharms:
            if not name:
                continue
            candidates.append({
                "npi": npi,
                "name": name,
                "address": address or "",
                "city": city or "",
                "zip": pharm_zip,
                "distance_miles": round(distance, 1),
                "preferred": info["preferred"] == "Y",
                "is_chain": bool(is_chain),
                "generic_fee": info["generic_fee"],
                "brand_fee": info["brand_fee"],
                "selected_fee": info["selected_fee"],
            })

    if not candidates:
        return []

    # Deduplicate by base name, keep closest
    seen = {}
    for p in candidates:
        base = p["name"].split("#")[0].strip().upper()
        if base not in seen or p["distance_miles"] < seen[base]["distance_miles"]:
            seen[base] = p

    # Sort: preferred chains first, then by distance
    sorted_pharms = sorted(
        seen.values(),
        key=lambda x: (not x["preferred"], not x["is_chain"], x["distance_miles"])
    )
    return sorted_pharms[:max_results]


def get_drug_cost_at_pharmacy(conn, contract_id, plan_id, ndc, tier,
                               unit_cost,
                               pharmacy, deductible_remaining, drug_name=""):
    """
    Calculate drug cost at a specific pharmacy.
    Uses preferred vs non-preferred copay rates from beneficiary_cost table.
    For MFP drugs, forces 25% coinsurance on negotiated price.
    """
    plan_id_padded = plan_id.zfill(3)
    is_preferred = pharmacy.get("preferred", True)
    disp_fee = pharmacy.get("generic_fee", 0) or pharmacy.get("selected_fee", 0)

    # Get correct cost row based on preferred status
    cost_row = conn.execute("""
        SELECT cost_type_pref, cost_amt_pref,
               cost_type_nonpref, cost_amt_nonpref,
               ded_applies
        FROM beneficiary_cost
        WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
        ORDER BY coverage_level ASC LIMIT 1
    """, (contract_id, plan_id_padded, tier)).fetchone()

    if not cost_row:
        return 0.0, 0

    if is_preferred:
        cost_type = cost_row[0]
        cost_amt = cost_row[1]
    else:
        cost_type = cost_row[2]
        cost_amt = cost_row[3]
    
    ded_applies_db = cost_row[4]

    # Apply MFP override for federally negotiated drugs (2026)
    drug_name_base = drug_name.split()[0] if drug_name else ""
    mfp = get_mfp(drug_name_base) or get_mfp(drug_name)
    if mfp is not None:
        unit_cost = mfp
        cost_type = 2
        cost_amt = 0.25

    if ded_applies_db == "N" or deductible_remaining <= 0:
        if cost_type == 0:
            patient_cost = 0.0
        elif cost_type == 1:
            patient_cost = float(cost_amt)
        elif cost_type == 2:
            patient_cost = round((unit_cost or 0) * float(cost_amt), 2)
        else:
            patient_cost = float(cost_amt)
        return round(patient_cost + disp_fee, 2), 0
    else:
        if unit_cost:
            drug_cost = unit_cost + disp_fee
            if drug_cost >= deductible_remaining:
                post_ded = float(cost_amt) if cost_type == 1 else 0
                return round(deductible_remaining + disp_fee + post_ded, 2), deductible_remaining
            else:
                return round(drug_cost, 2), drug_cost - disp_fee
        else:
            return round(float(cost_amt) + disp_fee, 2), 0



def get_mail_order_cost(conn, contract_id, plan_id, tier, cost_type_mail, cost_amt_mail, unit_cost, ded_applies, months_remaining):
    """Calculate mail order costs (90-day supply)."""
    monthly_costs = []
    deductible_remaining = 0  # Mail order typically post-deductible pricing
    
    for month_num in months_remaining:
        month_name = datetime(2026, month_num, 1).strftime("%B")
        if cost_type_mail == 0:
            monthly_cost = 0.0
        elif cost_type_mail == 1:
            monthly_cost = float(cost_amt_mail) / 3  # 90-day divided by 3
        elif cost_type_mail == 2:
            monthly_cost = round((unit_cost or 0) * float(cost_amt_mail) / 3, 2)
        else:
            monthly_cost = float(cost_amt_mail) / 3
        monthly_costs.append({"month": month_name, "cost": monthly_cost})
    
    return monthly_costs



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
    "flag": "any concern or empty string"
  }}
]

Rules:
- Fix misspellings (Xarelts -> Xarelto)
- Map nicknames (water pill -> Furosemide, blood thinner -> use context or flag)
- Map generics to their most common formulary name
- Keep dosage separate from name
- If completely unrecognizable, set confidence below 0.5 and explain in flag
- Never guess wildly — if unsure set confidence low and flag it"""

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


def get_drug_cost_for_plan(conn, formulary_id, contract_id, plan_id, rxcuis, deductible, months_remaining, drug_name=''):
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
    
    # Override with CMS negotiated MFP if available (more accurate for 2026)
    # MFP drugs use 25% coinsurance per 2026 standard benefit design
    # Strip dosage from drug_name before lookup (e.g. "rivaroxaban 20mg" -> "rivaroxaban")
    drug_name_base = drug_name.split()[0] if drug_name else ""
    mfp = get_mfp(drug_name_base) or get_mfp(drug_name)
    if mfp is not None:
        unit_cost = mfp
        # Force 25% coinsurance for MFP drugs regardless of plan's filed cost structure
        cost_type = 2
        cost_amt = 0.25

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

    # Get nearby pharmacies per plan (keyed by carrier)
    pharmacy_map = {}  # carrier -> list of nearby pharmacies
    for carrier, plan in plan_details.items():
        nearby = get_nearby_pharmacies(
            conn, plan["contract_id"], plan["plan_id"], zip_code,
            max_results=4, max_miles=30
        )
        pharmacy_map[carrier] = nearby

    for item in normalized:
        drug_name = item.get("normalized", "").strip()
        original_name = item.get("original", "").strip()
        dosage = item.get("dosage", "").strip()
        flag = item.get("flag", "")
        norm_confidence = item.get("confidence", 1.0)
        is_injectable = item.get("is_injectable", False)

        if not drug_name:
            continue

        if norm_confidence < 0.7 or flag:
            warnings.append({
                "drug": original_name,
                "normalized_to": drug_name,
                "flag": flag or "Low normalization confidence ({:.0%})".format(norm_confidence)
            })

        drug_result = {
            "drug_name": drug_name,
            "original_name": original_name,
            "dosage": dosage,
            "flag": flag,
            "normalization_confidence": norm_confidence,
            "is_injectable": is_injectable,
            "plans": {}
        }

        if is_injectable:
            for carrier in plan_details:
                drug_result["plans"][carrier] = {
                    "covered": False, "injectable": True,
                    "tier": None, "monthly_costs": [], "annual_total": None
                }
            results.append(drug_result)
            continue

        rxcuis = lookup_rxcuis(drug_name, dosage)

        if not rxcuis:
            drug_result["error"] = "Drug not found in formulary"
            warnings.append({
                "drug": original_name,
                "normalized_to": drug_name,
                "flag": "Not found in RxNav — verify drug name"
            })
            results.append(drug_result)
            continue

        # Determine if brand name drug
        is_brand = drug_name.lower() != original_name.lower() and bool(original_name)

        for carrier, plan in plan_details.items():
            plan_cost = get_drug_cost_for_plan(
                conn, plan["formulary_id"], plan["contract_id"],
                plan["plan_id"], rxcuis, plan["deductible"], months_remaining)
            
            # Add pharmacy-specific costs if we found nearby pharmacies
            nearby_pharmacies = pharmacy_map.get(carrier, [])
            if nearby_pharmacies and plan_cost.get("covered") and plan_cost.get("tier"):
                tier = plan_cost["tier"]
                # Get cost structure for this tier
                cost_row = conn.execute("""
                    SELECT cost_type_pref, cost_amt_pref, ded_applies,
                           cost_type_mail_pref, cost_amt_mail_pref
                    FROM beneficiary_cost
                    WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
                    ORDER BY coverage_level ASC LIMIT 1
                """, (plan["contract_id"], plan["plan_id"].zfill(3), tier)).fetchone()
                
                ndc = plan_cost.get("ndc")
                unit_cost = None
                if ndc:
                    pricing = conn.execute("""
                        SELECT unit_cost FROM pricing
                        WHERE contract_id = ? AND plan_id = ? AND ndc = ? AND days_supply = 30
                        LIMIT 1
                    """, (plan["contract_id"], plan["plan_id"].zfill(3), ndc)).fetchone()
                    if pricing:
                        unit_cost = pricing[0]
                
                pharmacy_costs = []
                if cost_row:
                    for pharmacy in nearby_pharmacies:
                        pharm_monthly = []
                        ded_remaining = plan["deductible"]
                        for month_num in months_remaining:
                            month_name = datetime(2026, month_num, 1).strftime("%B")
                            cost, ded_used = get_drug_cost_at_pharmacy(
                                conn, plan["contract_id"], plan["plan_id"],
                                ndc, tier, unit_cost,
                                pharmacy, ded_remaining, drug_name=drug_name
                            )
                            ded_remaining = max(0, ded_remaining - ded_used)
                            pharm_monthly.append({"month": month_name, "cost": cost})
                        
                        pharmacy_costs.append({
                            "name": pharmacy["name"],
                            "address": pharmacy["address"],
                            "city": pharmacy["city"],
                            "distance_miles": pharmacy["distance_miles"],
                            "preferred": pharmacy["preferred"],
                            "monthly_costs": pharm_monthly,
                            "annual_total": round(sum(m["cost"] for m in pharm_monthly), 2)
                        })
                
                plan_cost["pharmacy_costs"] = pharmacy_costs
            
            drug_result["plans"][carrier] = plan_cost
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


def build_pdf(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining, confidence=None, warnings=None, drug_detail_full=None):
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

    h1        = S("h1",  fontSize=13, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=16)
    h2        = S("h2",  fontSize=6,  textColor=colors.HexColor("#64748b"), leading=8)
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
                            topMargin=6*mm, bottomMargin=6*mm)
    elements = []

    # Header
    conf_text = f"Extraction confidence: {confidence:.0%}" if confidence else ""
    header_left = [[Paragraph(client_name, h1)],
                   [Paragraph(f"DOB: {dob}  ·  Zip: {zip_code}  ·  SOA Date: {soa_date}", h2)]]
    header_right = [[Paragraph("INTERNAL USE ONLY", badge_txt)],
                    [Paragraph(f"Generated: {datetime.today().strftime('%m/%d/%Y')}", gen_txt)],
                    [Paragraph("Data: CMS Medicare Formulary Q1 2026", gen_txt)],
                    [Paragraph(conf_text, S("ct", fontSize=6, textColor=colors.HexColor("#0d9488"), alignment=TA_RIGHT, leading=8))]]
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
        elements.append(Spacer(1, 1*mm))
        t, ma_best = make_plan_table(ma_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 2*mm))

    if ma_plans and drug_detail:
        elements.append(Paragraph("SECTION 2 — DRUG FORMULARY TIERS", sec_title))
        elements.append(Spacer(1, 1*mm))
        carriers = list(ma_plans.keys())
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)
        rows = [[Paragraph("Medication", col_hdr)] +
                [Paragraph(c + (" ★" if c == ma_best else ""), col_hdr) for c in carriers]]
        for drug in drug_detail:
            name = drug.get("drug_name","")
            dosage = drug.get("dosage","")
            original = drug.get("original_name", "")
            label = f"{name} {dosage}".strip() if dosage else name
            # Show original name only if it meaningfully differs
            # Strip dosage from original for comparison
            orig_base = original.split()[0].lower() if original else ""
            norm_base = name.split()[0].lower() if name else ""
            if original and orig_base != norm_base:
                label += "\n(written: " + original + ")"
            row = [Paragraph(label, drug_lbl)]
            for c in carriers:
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
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
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
        elements.append(Spacer(1, 2*mm))

    if ma_plans and drug_detail and months_remaining:
        # Get pharmacies for recommended plan
        best_drug = next((d for d in drug_detail if d.get("plans", {}).get(ma_best, {}).get("pharmacy_costs")), None)
        best_pharmacies = []
        if best_drug:
            best_pharmacies = best_drug["plans"][ma_best].get("pharmacy_costs", [])
        
        if best_pharmacies:
            elements.append(Paragraph(
                "SECTION 3 — ESTIMATED MONTHLY DRUG COSTS — " + ma_best.upper() + " (RECOMMENDED PLAN)",
                sec_title))
            elements.append(Spacer(1, 0.5*mm))
            # Show pharmacy names and distances
            pharm_info = "  ·  ".join([
                p["name"] + " (" + str(p["distance_miles"]) + " mi)"
                for p in best_pharmacies
            ])
            elements.append(Paragraph(
                "Nearest preferred pharmacies to ZIP " + zip_code + ":  " + pharm_info,
                S("note", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)))
            elements.append(Spacer(1, 1*mm))

            # Layout: Month | Drug1@Pharm1 | Drug1@Pharm2 | ... | Total@Pharm1 | Total@Pharm2
            # Simpler: one table per pharmacy showing all drugs as columns, months as rows
            # Even simpler: Month | Pharm1 Total | Pharm2 Total | Pharm3 Total | Pharm4 Total

            month_col_w = 20*mm
            pharm_col_w = (274*mm - month_col_w) / len(best_pharmacies)

            # Header row with pharmacy names
            hdr = [Paragraph("Month", ph_hdr)]
            for p in best_pharmacies:
                short_name = p["name"].split("#")[0].strip()
                if len(short_name) > 20:
                    short_name = short_name[:18] + "…"
                hdr.append(Paragraph(short_name, S("ph2", fontSize=6, textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)))
            rows = [hdr]

            # Monthly total rows (sum all drugs at each pharmacy)
            for month in months_remaining:
                row = [Paragraph(month, month_lbl)]
                for pharm in best_pharmacies:
                    monthly_total = 0
                    for drug in drug_detail:
                        pharm_costs = drug.get("plans", {}).get(ma_best, {}).get("pharmacy_costs", [])
                        matched = next((pc for pc in pharm_costs if pc["name"] == pharm["name"]), None)
                        if matched:
                            cost = next((m["cost"] for m in matched["monthly_costs"] if m["month"] == month), 0)
                            monthly_total += cost or 0
                    row.append(Paragraph("$" + "{:.2f}".format(monthly_total), cell))
                rows.append(row)

            # Annual total row
            annual_row = [Paragraph("Annual Total", S("at", fontSize=6, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=8))]
            for pharm in best_pharmacies:
                annual_total = 0
                for drug in drug_detail:
                    pharm_costs = drug.get("plans", {}).get(ma_best, {}).get("pharmacy_costs", [])
                    matched = next((pc for pc in pharm_costs if pc["name"] == pharm["name"]), None)
                    if matched:
                        annual_total += matched.get("annual_total", 0) or 0
                annual_row.append(Paragraph("$" + "{:.2f}".format(annual_total),
                    S("av", fontSize=6, textColor=GREEN_TEXT, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)))
            rows.append(annual_row)

            col_widths = [month_col_w] + [pharm_col_w] * len(best_pharmacies)
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
            t.setStyle(TableStyle(ts))
            elements.append(t)
        else:
            # Fallback: plan-level comparison if no pharmacy data
            elements.append(Paragraph("SECTION 3 — ESTIMATED MONTHLY TOTAL DRUG COST BY PLAN", sec_title))
            elements.append(Spacer(1, 0.5*mm))
            elements.append(Paragraph(
                "Monthly totals at preferred retail pharmacy. Pharmacy-specific data not available for this zip code.",
                S("note", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)))
            elements.append(Spacer(1, 1*mm))
            carriers = list(ma_plans.keys())
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
                    row.append(Paragraph("${:.2f}".format(total), cell))
                rows.append(row)
            annual_row = [Paragraph("Annual Total", S("at", fontSize=6, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=8))]
            for c in carriers:
                gt = sum((drug.get("plans",{}).get(c,{}).get("annual_total") or 0) for drug in drug_detail)
                annual_row.append(Paragraph("${:.2f}".format(gt), S("av", fontSize=6, textColor=CHARCOAL, fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)))
            rows.append(annual_row)
            t = Table(rows, colWidths=[month_col_w] + [col_w]*len(carriers))
            t.setStyle(TableStyle([
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
            ]))
            elements.append(t)
        elements.append(Spacer(1, 2*mm))

    if pd_plans:
        elements.append(Paragraph("SECTION 4 — PART D STANDALONE PLANS", sec_title))
        elements.append(Spacer(1, 1*mm))
        t, _ = make_plan_table(pd_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 2*mm))

    elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceBefore=1*mm, spaceAfter=1*mm))
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
