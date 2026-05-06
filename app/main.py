"""
Medicare Drug Cost API v7
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

# ===== FALLBACK PLANS (used when service_area table not available) =====
FALLBACK_PLANS = [
    {"carrier": "HealthPartners", "contract_id": "H4882", "plan_id": "009", "type": "MA"},
    {"carrier": "Blue Cross",     "contract_id": "H5959", "plan_id": "009", "type": "MA"},
    {"carrier": "Medica",         "contract_id": "H6154", "plan_id": "001", "type": "MA"},
    {"carrier": "Humana",         "contract_id": "H5216", "plan_id": "275", "type": "MA"},
    {"carrier": "Aetna",          "contract_id": "H3219", "plan_id": "001", "type": "MA"},
    {"carrier": "Humana Part D",  "contract_id": "S5884", "plan_id": "190", "type": "PD"},
    {"carrier": "WellCare Part D","contract_id": "S4802", "plan_id": "146", "type": "PD"},
]

# SNP types to exclude from standard reports (specialty plans)
EXCLUDE_PLAN_TYPES = {"HMO D-SNP", "PPO D-SNP", "HMO C-SNP", "PPO C-SNP",
                      "HMO I-SNP", "PPO I-SNP", "PACE", "MSA"}

def get_plans_for_zip(conn, zip_code):
    """
    Dynamically load plans available for a client zip code.
    Uses service_area + zip_county tables if available.
    Falls back to hardcoded FALLBACK_PLANS.
    """
    # Check if service_area and zip_county tables exist
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if "service_area" not in tables or "zip_county" not in tables:
        return FALLBACK_PLANS

    # Get county for this zip
    row = conn.execute(
        "SELECT county_name FROM zip_county WHERE zip = ?", (zip_code,)
    ).fetchone()

    if not row:
        # Try nearby zips by incrementing/decrementing
        for delta in [1, -1, 2, -2, 3, -3]:
            alt_zip = str(int(zip_code) + delta).zfill(5)
            row = conn.execute(
                "SELECT county_name FROM zip_county WHERE zip = ?", (alt_zip,)
            ).fetchone()
            if row:
                break

    if not row:
        return FALLBACK_PLANS

    county = row[0]

    # Get all MA/Cost plans available in this county
    # Exclude SNP/specialty plans from standard reports
    ma_rows = conn.execute("""
        SELECT sa.contract_id, sa.plan_id, sa.plan_name, sa.org_name,
               MIN(sa.premium_total) as premium_total, sa.deductible, sa.plan_type,
               p.formulary_id
        FROM service_area sa
        LEFT JOIN plans p ON p.contract_id = sa.contract_id
                          AND p.plan_id = sa.plan_id
        WHERE sa.county_name IN (?, 'All Counties')
        AND sa.plan_type NOT IN ('PDP', 'PACE')
        AND sa.plan_type NOT LIKE '%SNP%'
        AND sa.plan_type NOT LIKE '%D-SNP%'
        AND sa.plan_type NOT LIKE '%C-SNP%'
        AND sa.plan_type NOT LIKE '%I-SNP%'
        AND p.formulary_id IS NOT NULL
        GROUP BY sa.contract_id, sa.plan_id
        ORDER BY MIN(sa.premium_total) ASC, sa.plan_name ASC
    """, (county,)).fetchall()

    # Get Part D plans available statewide
    pd_rows = conn.execute("""
        SELECT sa.contract_id, sa.plan_id, sa.plan_name, sa.org_name,
               MIN(sa.premium_total) as premium_total, sa.deductible, sa.plan_type,
               p.formulary_id
        FROM service_area sa
        LEFT JOIN plans p ON p.contract_id = sa.contract_id
                          AND p.plan_id = sa.plan_id
        WHERE sa.county_name IN (?, 'All Counties')
        AND sa.plan_type = 'PDP'
        AND p.formulary_id IS NOT NULL
        GROUP BY sa.contract_id, sa.plan_id
        ORDER BY MIN(sa.premium_total) ASC
    """, (county,)).fetchall()

    if not ma_rows and not pd_rows:
        return FALLBACK_PLANS

    plans = []
    seen = set()

    # Friendly name lookup — use short carrier names for known plans
    FRIENDLY_NAMES = {
        ("H4882", "009"): "HealthPartners Journey Pace",
        ("H4882", "003"): "HealthPartners Journey Steady",
        ("H4882", "011"): "HealthPartners Journey Stride",
        ("H4882", "014"): "HealthPartners Journey Smart",
        ("H6309", "001"): "HealthPartners Birch",
        ("H6309", "002"): "HealthPartners Cedar",
        ("H5959", "009"): "Blue Cross Choice",
        ("H5959", "010"): "Blue Cross Complete",
        ("H5959", "011"): "Blue Cross Complete",
        ("H5959", "012"): "Blue Cross Core",
        ("H5959", "013"): "Blue Cross Core",
        ("H5959", "014"): "Blue Cross Choice",
        ("H5959", "015"): "Blue Cross Comfort",
        ("H5959", "016"): "Blue Cross Comfort",
        ("H6154", "001"): "Medica Advantage",
        ("H8889", "001"): "Medica Advantage",
        ("H8889", "002"): "Medica Advantage",
        ("H8889", "003"): "Medica Advantage ($103)",
        ("H8889", "004"): "Medica Advantage ($140)",
        ("H8889", "005"): "Medica Advantage ($0)",
        ("H8889", "008"): "Medica Advantage ($44)",
        ("H8889", "009"): "Medica Advantage (No Rx)",
        ("H8889", "010"): "Medica Value",
        ("H8889", "011"): "Medica Preferred ($23)",
        ("H8889", "012"): "Medica Select ($0)",
        ("H8889", "013"): "Medica Preferred ($29)",
        ("H8889", "014"): "Medica Value ($0)",
        ("H8889", "015"): "Medica Select ($0/355)",
        ("H8889", "017"): "Medica Value ($0/617)",
        ("H8889", "018"): "Medica Select ($0/355b)",
        ("H2450", "002"): "Medica Cost Enhanced",
        ("H2450", "007"): "Medica Cost Thrift",
        ("H2450", "016"): "Medica Cost Basic",
        ("H2450", "035"): "Medica Cost Core",
        ("H2450", "037"): "Medica Cost Premier",
        ("H2450", "039"): "Medica Cost Focus",
        ("H2450", "049"): "Medica Cost Standard",
        ("H5216", "275"): "Humana Choice ($0)",
        ("H5216", "063"): "Humana Choice ($18)",
        ("H5216", "092"): "Humana Choice ($22)",
        ("H5216", "359"): "Humana Choice ($0/615b)",
        ("H3219", "001"): "Aetna Signature",
        ("H3219", "002"): "Aetna Enhanced ($19)",
        ("H3219", "003"): "Aetna Grand ($40)",
        ("H3219", "004"): "Aetna Grand Extra ($69)",
        ("H3219", "005"): "Aetna Eagle",
        ("H3219", "008"): "Aetna Signature Fit",
        ("H3219", "012"): "Aetna Signature ($12)",
        ("H3219", "014"): "Aetna Enhanced ($46)",
        ("H2001", "116"): "UHC AARP ($0/600)",
        ("H2001", "117"): "UHC AARP ($0/520)",
        ("H2001", "123"): "UHC AARP ($0/520b)",
        ("H3186", "001"): "Align ChoiceElite ($63)",
        ("H3186", "002"): "Align ChoicePlus ($0)",
        ("H8145", "006"): "Humana Gold Choice",
        ("H9834", "001"): "Gundersen Quartz Elite",
        ("H9834", "003"): "Gundersen Quartz Value",
        ("H9834", "006"): "Gundersen Quartz Core",
        ("H9834", "007"): "Gundersen Quartz Basic",
        ("S5884", "190"): "Humana Value Rx",
        ("S5884", "171"): "Humana Premier Rx",
        ("S5884", "204"): "Humana Value Rx ($0/601)",
        ("S5884", "145"): "Humana Basic Rx",
        ("S4802", "146"): "WellCare Value Script",
        ("S4802", "158"): "WellCare Value Script b",
        ("S4802", "089"): "WellCare Classic",
        ("S5601", "050"): "SilverScript Choice",
        ("S5743", "001"): "MedicareBlue Rx",
        ("S5921", "370"): "AARP Rx Saver",
        ("S5921", "406"): "AARP Rx Preferred",
    }

    # Group by carrier family — show only the lowest-premium plan per carrier
    # e.g. HealthPartners shows Journey Pace ($0), not all 4 plans
    CARRIER_FAMILY = {
        "H4882": "HealthPartners", "H6309": "HealthPartners",
        "H5959": "Blue Cross",
        "H6154": "Medica", "H8889": "Medica", "H2450": "Medica Cost",
        "H5216": "Humana", "H8145": "Humana",
        "H3219": "Aetna",
        "H2001": "UHC AARP",
        "H3186": "Align",
        "H9834": "Quartz",
    }

    # Pick best plan per carrier family (lowest premium from plans table, prefer $0)
    # service_area tells us WHICH plans are available, plans table has correct premiums
    best_per_family = {}
    for row in ma_rows:
        cid, pid = row[0], row[1].zfill(3)
        key = (cid, pid)
        family = CARRIER_FAMILY.get(cid, cid)
        # Get premium from plans table (correct Landscape premium)
        plan_row = conn.execute(
            "SELECT premium, deductible FROM plans WHERE contract_id=? AND plan_id=?",
            (cid, pid)
        ).fetchone()
        premium = float(plan_row[0]) if plan_row else (row[4] or 999)
        deductible = float(plan_row[1]) if plan_row else (row[5] or 0)
        current = best_per_family.get(family)
        is_better = (
            current is None or
            premium < current["premium"] or
            (premium == current["premium"] and deductible < current["deductible"])
        )
        if is_better:
            best_per_family[family] = {
                "contract_id": cid, "plan_id": pid,
                "plan_name": row[2], "org_name": row[3],
                "premium": premium, "deductible": deductible,
                "plan_type": row[6], "key": key
            }

    # First pass: best plan per carrier family
    for family, row in sorted(best_per_family.items(), key=lambda x: x[1]["premium"]):
        key = row["key"]
        if key in seen:
            continue
        seen.add(key)
        carrier = FRIENDLY_NAMES.get(key, row["plan_name"][:35] if row["plan_name"] else row["contract_id"])
        plans.append({
            "carrier": carrier,
            "contract_id": row["contract_id"],
            "plan_id": row["plan_id"],
            "type": "Cost" if "Cost" in (row["plan_type"] or "") else "MA",
            "landscape_premium": row["premium"],
            "landscape_deductible": row["deductible"],
        })

    # Second pass: if fewer than 5 MA plans, fill with next premium tier up
    # Skip $0 plans (already shown), pick lowest-cost paid plans not yet shown
    if len([p for p in plans if p["type"] in ("MA", "Cost")]) < 5:
        paid_candidates = []
        for row in ma_rows:
            cid, pid = row[0], row[1].zfill(3)
            key = (cid, pid)
            if key in seen:
                continue
            plan_row = conn.execute(
                "SELECT premium, deductible FROM plans WHERE contract_id=? AND plan_id=?",
                (cid, pid)
            ).fetchone()
            premium = float(plan_row[0]) if plan_row else (row[4] or 999)
            deductible = float(plan_row[1]) if plan_row else (row[5] or 0)
            # Only include paid plans (premium > 0) for the filler spots
            if premium > 0:
                paid_candidates.append({
                    "contract_id": cid, "plan_id": pid,
                    "plan_name": row[2], "org_name": row[3],
                    "premium": premium, "deductible": deductible,
                    "plan_type": row[6], "key": key
                })
        # Sort by lowest premium first so agent sees most affordable upgrade
        paid_candidates.sort(key=lambda x: (x["premium"], x["deductible"]))
        for row in paid_candidates:
            if len([p for p in plans if p["type"] in ("MA", "Cost")]) >= 5:
                break
            key = row["key"]
            if key in seen:
                continue
            seen.add(key)
            carrier = FRIENDLY_NAMES.get(key, row["plan_name"][:35] if row["plan_name"] else row["contract_id"])
            plans.append({
                "carrier": carrier,
                "contract_id": row["contract_id"],
                "plan_id": row["plan_id"],
                "type": "Cost" if "Cost" in (row["plan_type"] or "") else "MA",
                "landscape_premium": row["premium"],
                "landscape_deductible": row["deductible"],
            })

    # Part D: one plan per carrier family, pick 3 cheapest from different carriers
    PD_CARRIER_FAMILY = {
        "S5884": "Humana", "S4802": "WellCare", "S5601": "SilverScript",
        "S5743": "MedicareBlue", "S5921": "UHC AARP", "S5660": "Cigna",
    }
    pd_best_per_carrier = {}
    for row in pd_rows:
        cid, pid = row[0], row[1].zfill(3)
        key = (cid, pid)
        if key in seen:
            continue
        # Get actual premium from plans table
        plan_row = conn.execute(
            "SELECT premium FROM plans WHERE contract_id=? AND plan_id=?", (cid, pid)
        ).fetchone()
        premium = float(plan_row[0]) if plan_row else (row[4] or 999)
        pd_family = PD_CARRIER_FAMILY.get(cid, cid)
        if pd_family not in pd_best_per_carrier or premium < pd_best_per_carrier[pd_family]["premium"]:
            pd_best_per_carrier[pd_family] = {
                "key": key, "contract_id": cid, "plan_id": pid,
                "plan_name": row[2], "premium": premium, "deductible": row[5],
            }

    # Sort by premium, take 3 cheapest from different carriers
    pd_sorted = sorted(pd_best_per_carrier.values(), key=lambda x: x["premium"])
    for d in pd_sorted[:3]:
        key = d["key"]
        if key in seen:
            continue
        seen.add(key)
        carrier = FRIENDLY_NAMES.get(key, d["plan_name"][:35] if d["plan_name"] else d["contract_id"])
        plans.append({
            "carrier": carrier,
            "contract_id": d["contract_id"],
            "plan_id": d["plan_id"],
            "type": "PD",
            "landscape_premium": d["premium"],
            "landscape_deductible": d["deductible"],
        })

    return plans if plans else FALLBACK_PLANS



def resolve_custom_plans(conn, custom_plans_str, existing_plan_keys):
    if not custom_plans_str or not custom_plans_str.strip():
        return [], []
    ALIASES = {
        "bc": "blue cross", "bcbs": "blue cross", "blue cross": "blue cross",
        "hp": "healthpartners", "health partners": "healthpartners", "healthpartners": "healthpartners",
        "medica": "medica", "humana": "humana", "aetna": "aetna", "allina": "aetna",
        "uhc": "uhc", "aarp": "uhc", "united": "uhc", "align": "align",
        "wellcare": "wellcare", "silverscript": "silverscript", "medicareblue": "medicareblue",
    }
    PLAN_KEYWORDS = {
        "core", "comfort", "choice", "complete", "pace", "stride", "steady", "smart",
        "birch", "cedar", "value", "preferred", "select", "solution", "basic", "premier",
        "saver", "classic", "signature", "enhanced", "grand", "eagle", "fit", "freedom",
        "elite", "plus", "standard", "focus", "thrift",
    }
    FN = {
        ("H4882","009"): "HealthPartners Journey Pace",
        ("H4882","003"): "HealthPartners Journey Steady",
        ("H4882","011"): "HealthPartners Journey Stride",
        ("H4882","014"): "HealthPartners Journey Smart",
        ("H6309","001"): "HealthPartners Birch",
        ("H6309","002"): "HealthPartners Cedar",
        ("H5959","009"): "Blue Cross Choice",
        ("H5959","010"): "Blue Cross Complete",
        ("H5959","011"): "Blue Cross Complete",
        ("H5959","012"): "Blue Cross Core",
        ("H5959","013"): "Blue Cross Core",
        ("H5959","014"): "Blue Cross Choice",
        ("H5959","015"): "Blue Cross Comfort",
        ("H5959","016"): "Blue Cross Comfort",
        ("H6154","001"): "Medica Advantage",
        ("H8889","001"): "Medica Advantage",
        ("H8889","002"): "Medica Advantage",
        ("H8889","003"): "Medica Advantage",
        ("H8889","004"): "Medica Advantage",
        ("H8889","005"): "Medica Advantage",
        ("H8889","008"): "Medica Advantage",
        ("H8889","010"): "Medica Value",
        ("H8889","011"): "Medica Preferred",
        ("H8889","012"): "Medica Select",
        ("H8889","013"): "Medica Preferred",
        ("H8889","014"): "Medica Value",
        ("H8889","015"): "Medica Select",
        ("H8889","017"): "Medica Value",
        ("H8889","018"): "Medica Select",
        ("H2450","002"): "Medica Cost Enhanced",
        ("H2450","007"): "Medica Cost Thrift",
        ("H2450","016"): "Medica Cost Basic",
        ("H2450","035"): "Medica Cost Core",
        ("H2450","037"): "Medica Cost Premier",
        ("H2450","039"): "Medica Cost Focus",
        ("H2450","049"): "Medica Cost Standard",
        ("H5216","275"): "Humana Choice",
        ("H5216","063"): "Humana Choice",
        ("H5216","092"): "Humana Choice",
        ("H5216","359"): "Humana Choice",
        ("H3219","001"): "Aetna Signature",
        ("H3219","002"): "Aetna Enhanced",
        ("H3219","003"): "Aetna Grand",
        ("H3219","004"): "Aetna Grand Extra",
        ("H3219","008"): "Aetna Signature Fit",
        ("H3219","012"): "Aetna Signature",
        ("H3219","014"): "Aetna Enhanced",
        ("H2001","116"): "UHC AARP",
        ("H2001","117"): "UHC AARP",
        ("H2001","123"): "UHC AARP",
        ("H3186","001"): "Align ChoiceElite",
        ("H3186","002"): "Align ChoicePlus",
        ("H8145","006"): "Humana Gold Choice",
        ("S5884","190"): "Humana Value Rx",
        ("S5884","145"): "Humana Basic Rx",
        ("S5884","171"): "Humana Premier Rx",
        ("S4802","146"): "WellCare Value Script",
        ("S4802","089"): "WellCare Classic",
        ("S5601","050"): "SilverScript Choice",
        ("S5743","001"): "MedicareBlue Rx",
        ("S5921","370"): "AARP Rx Saver",
        ("S5921","406"): "AARP Rx Preferred",
    }
    CARRIER_FAMILY = {
        "H4882": "HealthPartners", "H6309": "HealthPartners",
        "H5959": "Blue Cross",
        "H6154": "Medica", "H8889": "Medica", "H2450": "Medica Cost",
        "H5216": "Humana", "H8145": "Humana",
        "H3219": "Aetna", "H2001": "UHC AARP", "H3186": "Align", "H9834": "Quartz",
    }
    all_plans = conn.execute("SELECT contract_id, plan_id, plan_name, premium, deductible FROM plans").fetchall()

    def score_plan(req_str, cid, pid, plan_name):
        req = req_str.lower().strip()
        pname = plan_name.lower() if plan_name else ""
        friendly = FN.get((cid, pid.zfill(3)), "").lower()
        score = 0
        for alias, expanded in ALIASES.items():
            if alias in req:
                req = req.replace(alias, expanded)
        cf = CARRIER_FAMILY.get(cid, "").lower()
        if cf and cf in req:
            score += 10
        for kw in PLAN_KEYWORDS:
            if kw in req and (kw in pname or kw in friendly):
                score += 8
        for word in friendly.split():
            if len(word) > 3 and word in req:
                score += 3
        return score

    requests_list = [r.strip() for r in custom_plans_str.split(",") if r.strip()]
    resolved = []
    unmatched = []

    for req_str in requests_list[:2]:
        best_score = 0
        best_plan = None
        for row in all_plans:
            cid, pid = row[0], row[1].zfill(3)
            if (cid, pid) in existing_plan_keys:
                continue
            s = score_plan(req_str, cid, pid, row[2])
            if s > best_score:
                best_score = s
                best_plan = row
        if best_plan and best_score >= 8:
            cid, pid = best_plan[0], best_plan[1].zfill(3)
            key = (cid, pid)
            if key in existing_plan_keys:
                unmatched.append(f"Requested plan '{req_str}' is already included in the comparison")
                continue
            existing_plan_keys.add(key)
            friendly = FN.get(key, best_plan[2][:35] if best_plan[2] else cid)
            pt_row = conn.execute("SELECT plan_type FROM service_area WHERE contract_id=? AND plan_id=? LIMIT 1", (cid, pid)).fetchone()
            ptype = "Cost" if (pt_row and "Cost" in pt_row[0]) else "MA"
            resolved.append({
                "carrier": friendly, "contract_id": cid, "plan_id": pid, "type": ptype,
                "landscape_premium": float(best_plan[3]) if best_plan[3] is not None else 0.0,
                "landscape_deductible": float(best_plan[4]) if best_plan[4] is not None else 0.0,
                "custom": True,
            })
        else:
            unmatched.append(f"Requested plan '{req_str}' is not available in this service area")

    return resolved, unmatched


# ===== CMS NEGOTIATED MAXIMUM FAIR PRICES (MFP) FOR 2026 =====
# Source: CMS Medicare Drug Price Negotiation Program, effective January 1, 2026
# Patient pays 25% coinsurance × MFP
MFP_2026 = {
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

# ===== INSULIN KEYWORDS FOR $35 CAP =====
# IRA 2022: All insulins under Medicare Part D capped at $35/month
# No deductible applies to insulins - flat $35 max regardless of plan
INSULIN_KEYWORDS = [
    "insulin", "glargine", "lantus", "basaglar", "toujeo", "semglee", "rezvoglar",
    "lispro", "humalog", "admelog", "aspart", "novolog", "fiasp", "novorapid",
    "detemir", "levemir", "degludec", "tresiba", "glulisine", "apidra",
    "nph insulin", "regular insulin", "humulin", "novolin"
]

def is_insulin(drug_name):
    """Check if a drug is an insulin - subject to $35/month Medicare cap."""
    if not drug_name:
        return False
    name_lower = drug_name.lower()
    return any(kw in name_lower for kw in INSULIN_KEYWORDS)

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


def geocode_address_live(address, city, state, zipcode):
    """
    Geocode a street address using Nominatim (OpenStreetMap).
    Returns (lat, lon, city) or (None, None, None) on failure.
    """
    if not address or not city:
        return None, None, None
    try:
        query = f"{address}, {city}, {state or 'MN'} {zipcode}, USA"
        params = requests.utils.requote_uri(
            "https://nominatim.openstreetmap.org/search?q=" +
            requests.utils.quote(query) +
            "&format=json&limit=1&countrycodes=us"
        )
        resp = requests.get(
            params,
            timeout=8,
            headers={"User-Agent": "MedicareDrugEngine/1.0 contact@medicare-tool.com"}
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"]), city
    except Exception:
        pass
    return None, None, None


def get_client_coords(conn, zip_code, address=None, city=None, state=None):
    """
    Get lat/lon for client location.
    Uses actual street address geocoding if available (more accurate).
    Falls back to zip code centroid.
    """
    # Try real address geocoding first
    if address and city:
        lat, lon, city_name = geocode_address_live(address, city, state, zip_code)
        if lat and lon:
            return lat, lon, city_name
    
    # Fall back to zip centroid
    row = conn.execute(
        "SELECT lat, lon, city FROM zip_coords WHERE zip = ?", (zip_code,)
    ).fetchone()
    if row and row[0] and row[1]:
        return row[0], row[1], row[2]
    return None, None, None


def get_nearby_pharmacies(conn, contract_id, plan_id, client_zip, max_results=4, max_miles=30,
                          client_address=None, client_city=None, client_state=None):
    """
    Find closest preferred retail pharmacies to client location.
    Uses real street address geocoding when available for precise distances.
    Falls back to zip centroid.
    """
    plan_id_padded = plan_id.zfill(3)

    # Get client coordinates - prefer real address over zip centroid
    client_lat, client_lon, client_city = get_client_coords(
        conn, client_zip, address=client_address, city=client_city, state=client_state
    )
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
    # Track both preferred and non-preferred fees per zip
    zip_info = {}
    for row in pref_rows:
        pharm_zip, pref, gen_fee, brand_fee, sel_fee = row
        pharm_zip = pharm_zip.zfill(5)
        if pharm_zip not in zip_info:
            zip_info[pharm_zip] = {
                "preferred": pref,
                "has_preferred": pref == "Y",
                "has_nonpreferred": pref == "N",
                "generic_fee": float(gen_fee or 0),
                "brand_fee": float(brand_fee or 0),
                "selected_fee": float(sel_fee or 0),
            }
        else:
            # Track if zip has both preferred and non-preferred pharmacies
            if pref == "Y":
                zip_info[pharm_zip]["has_preferred"] = True
                zip_info[pharm_zip]["preferred"] = "Y"
            else:
                zip_info[pharm_zip]["has_nonpreferred"] = True

    # Find pharmacy names in nearby zips
    # Only include pharmacies that are confirmed in-network for this plan
    # We verify by checking if their zip is in pharmacy_network for this plan
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

        # Get pharmacies in this zip that are confirmed in-network
        # Use pharmacy's actual lat/lon if available for precise distance
        pharms = conn.execute("""
            SELECT DISTINCT pn.npi, pn.name, pn.address, pn.city, pn.is_chain,
                   pn.lat, pn.lon
            FROM pharmacy_names pn
            INNER JOIN pharmacy_network net ON net.pharmacy_zip = pn.zip
            WHERE pn.zip = ?
            AND net.contract_id = ?
            AND net.plan_id = ?
            AND net.is_retail = 1
            AND pn.name != 'Unknown Pharmacy'
            AND pn.name IS NOT NULL
        """, (pharm_zip, contract_id, plan_id_padded)).fetchall()

        for npi, name, address, city, is_chain, pharm_lat, pharm_lon in pharms:
            if not name:
                continue
            # Use pharmacy's actual coordinates if available, else use zip centroid
            if pharm_lat and pharm_lon:
                distance = haversine_distance(client_lat, client_lon, pharm_lat, pharm_lon)
            # else distance already calculated from zip centroid above
            # Determine preferred status
            if info.get("has_preferred") and info.get("has_nonpreferred"):
                is_preferred = bool(is_chain)
            else:
                is_preferred = info["preferred"] == "Y"
            
            candidates.append({
                "npi": npi,
                "name": name,
                "address": address or "",
                "city": city or "",
                "zip": pharm_zip,
                "distance_miles": round(distance, 1),
                "preferred": is_preferred,
                "is_chain": bool(is_chain),
                "generic_fee": info["generic_fee"],
                "brand_fee": info["brand_fee"],
                "selected_fee": info["selected_fee"],
                "in_network": True,
            })

    if not candidates:
        return []

    # Deduplicate by base name, keep closest
    # Add small address-based offset to distinguish same-zip pharmacies
    seen = {}
    for p in candidates:
        base = p["name"].split("#")[0].strip().upper()
        # Add tiny distance variation based on address hash so same-zip pharmacies differ slightly
        addr_hash = sum(ord(c) for c in p.get("address", "")) % 100
        p["distance_miles"] = round(p["distance_miles"] + addr_hash * 0.001, 1)
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
        # Non-preferred: use nonpref rates if available, fall back to pref
        cost_type = cost_row[2] if cost_row[2] is not None else cost_row[0]
        cost_amt = cost_row[3] if cost_row[3] is not None and float(cost_row[3] or 0) > 0 else cost_row[1]
    
    ded_applies_db = cost_row[4]

    # Apply insulin $35 cap first (overrides everything)
    drug_name_base = drug_name.split()[0] if drug_name else ""
    if is_insulin(drug_name) or is_insulin(drug_name_base):
        return 35.00, 0  # Flat $35, no deductible

    # Apply MFP override for federally negotiated drugs (2026)
    mfp = get_mfp(drug_name_base) or get_mfp(drug_name)
    is_mfp_drug = mfp is not None
    if is_mfp_drug:
        unit_cost = mfp
        cost_type = 2
        cost_amt = 0.25
        disp_fee = 0  # MFP coinsurance is the full patient cost, no additional dispensing fee

    if ded_applies_db == "N" or deductible_remaining <= 0:
        if cost_type == 0:
            patient_cost = 0.0
        elif cost_type == 1:
            patient_cost = float(cost_amt)
        elif cost_type == 2:
            patient_cost = round((unit_cost or 0) * float(cost_amt), 2)
        else:
            patient_cost = float(cost_amt)
        # Only add dispensing fee if there is an actual drug cost (not $0 copay generics)
        if patient_cost > 0:
            patient_cost = round(patient_cost + disp_fee, 2)
        return patient_cost, 0
    else:
        if unit_cost:
            drug_cost = unit_cost + disp_fee
            if drug_cost >= deductible_remaining:
                post_ded = float(cost_amt) if cost_type == 1 else 0
                return round(deductible_remaining + disp_fee + post_ded, 2), deductible_remaining
            else:
                return round(drug_cost, 2), drug_cost - disp_fee
        else:
            patient_cost = float(cost_amt)
            if patient_cost > 0:
                patient_cost = round(patient_cost + disp_fee, 2)
            return patient_cost, 0



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
    
    # Check for insulin $35 cap (IRA 2022 - applies to all Medicare Part D insulins)
    drug_name_base = drug_name.split()[0] if drug_name else ""
    if is_insulin(drug_name) or is_insulin(drug_name_base):
        # Insulin: flat $35/month cap, no deductible applies
        monthly_costs = []
        for month_num in months_remaining:
            month_name = datetime(2026, month_num, 1).strftime("%B")
            monthly_costs.append({"month": month_name, "cost": 35.00})
        return {
            "tier": tier, "covered": True, "ndc": ndc,
            "monthly_costs": monthly_costs,
            "annual_total": round(35.00 * len(months_remaining), 2),
            "steady_state_copay": 35.00,
            "insulin_cap": True
        }

    # Override with CMS negotiated MFP if available (more accurate for 2026)
    # MFP drugs use 25% coinsurance per 2026 standard benefit design
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


def compute_drug_costs(drugs, zip_code, soa_date, client_address=None, client_city=None, client_state=None, custom_plans_str=None):
    """
    drugs: list of {"name": str, "dosage": str}
    Normalizes drug names via Claude first, then looks up costs.
    Uses real client address for pharmacy distance calculations when available.
    custom_plans_str: optional comma-separated agent-requested plan names
    """
    months_remaining = get_remaining_months(soa_date)
    month_names = [datetime(2026, m, 1).strftime("%B") for m in months_remaining]

    conn = get_db()

    # Dynamically load plans available for this zip code
    available_plans = get_plans_for_zip(conn, zip_code)

    # Append any agent-requested custom plans (max 2, skip dupes)
    custom_warnings = []
    if custom_plans_str and custom_plans_str.strip():
        existing_keys = {(p["contract_id"], p["plan_id"].zfill(3)) for p in available_plans}
        custom, custom_warnings = resolve_custom_plans(conn, custom_plans_str, existing_keys)
        available_plans = available_plans + custom

    plan_details = {}
    for plan in available_plans:
        row = conn.execute("SELECT * FROM plans WHERE contract_id = ? AND plan_id = ?",
                           (plan["contract_id"], plan["plan_id"].zfill(3))).fetchone()
        if row:
            # Use landscape premium if available (more accurate consumer-facing price)
            premium = plan.get("landscape_premium", row["premium"])
            if premium is None or premium == 0:
                premium = row["premium"]
            deductible = plan.get("landscape_deductible", row["deductible"])
            if deductible is None or deductible == 0:
                deductible = row["deductible"]
            plan_details[plan["carrier"]] = {
                "carrier": plan["carrier"], "plan_type": plan["type"],
                "plan_name": row["plan_name"], "formulary_id": row["formulary_id"],
                "contract_id": plan["contract_id"], "plan_id": plan["plan_id"],
                "premium": float(premium), "deductible": float(deductible),
                "premium_monthly": float(premium),
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
            max_results=4, max_miles=30,
            client_address=client_address,
            client_city=client_city,
            client_state=client_state
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
        drug_name_base_outer = drug_name.split()[0] if drug_name else ""
        mfp_value = get_mfp(drug_name_base_outer) or get_mfp(drug_name) or 0
        is_mfp_drug_flag = mfp_value > 0

        for carrier, plan in plan_details.items():
            plan_cost = get_drug_cost_for_plan(
                conn, plan["formulary_id"], plan["contract_id"],
                plan["plan_id"], rxcuis, plan["deductible"], months_remaining,
                drug_name=drug_name)
            
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
                
                # Check if this is an insulin drug - flat $35 at all pharmacies
                drug_name_base = drug_name.split()[0] if drug_name else ""
                drug_is_insulin = is_insulin(drug_name) or is_insulin(drug_name_base)
                
                for pharmacy in nearby_pharmacies:
                    pharm_monthly = []
                    ded_remaining = plan["deductible"]
                    
                    if drug_is_insulin:
                        # Insulin: flat $35/month, no deductible
                        for month_num in months_remaining:
                            month_name = datetime(2026, month_num, 1).strftime("%B")
                            pharm_monthly.append({"month": month_name, "cost": 35.00})
                    elif cost_row:
                        for month_num in months_remaining:
                            month_name = datetime(2026, month_num, 1).strftime("%B")
                            cost, ded_used = get_drug_cost_at_pharmacy(
                                conn, plan["contract_id"], plan["plan_id"],
                                ndc, tier, unit_cost,
                                pharmacy, ded_remaining, drug_name=drug_name
                            )
                            ded_remaining = max(0, ded_remaining - ded_used)
                            pharm_monthly.append({"month": month_name, "cost": cost})
                    
                    if pharm_monthly:
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

                # Calculate mail order costs
                mail_cost_row = conn.execute("""
                    SELECT cost_type_mail_pref, cost_amt_mail_pref, ded_applies
                    FROM beneficiary_cost
                    WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 3
                    ORDER BY coverage_level ASC LIMIT 1
                """, (plan["contract_id"], plan["plan_id"].zfill(3), plan_cost.get("tier", 0))).fetchone()

                if mail_cost_row or drug_is_insulin or is_mfp_drug_flag:
                    mail_monthly = []
                    for month_num in months_remaining:
                        month_name = datetime(2026, month_num, 1).strftime("%B")
                        if drug_is_insulin:
                            mail_cost = 35.00
                        elif is_mfp_drug_flag:
                            # MFP drugs: mail order 90-day = same monthly equivalent
                            mail_cost = round(mfp_value * 0.25, 2)
                        elif mail_cost_row:
                            mt = mail_cost_row[0]
                            ma = mail_cost_row[1]
                            if mt == 0:
                                mail_cost = 0.0
                            elif mt == 1:
                                mail_cost = float(ma) / 3  # 90-day divided by 3
                            elif mt == 2:
                                mail_cost = round((unit_cost or 0) * float(ma) / 3, 2)
                            else:
                                mail_cost = float(ma) / 3
                        else:
                            mail_cost = 0.0
                        mail_monthly.append({"month": month_name, "cost": mail_cost})
                    plan_cost["mail_order_costs"] = {
                        "monthly_costs": mail_monthly,
                        "annual_total": round(sum(m["cost"] for m in mail_monthly), 2)
                    }
            
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
        premium_annual = round(plan["premium_monthly"] * len(months_remaining), 2)
        plan_summaries[carrier] = {
            "plan_name": plan["plan_name"], "plan_type": plan["plan_type"],
            "premium_monthly": plan["premium_monthly"], "premium_remaining_year": premium_annual,
            "deductible": plan["deductible"],
            "total_drug_cost": round(total_drug_cost, 2),
            "total_drug_plus_premium": round(total_drug_cost + premium_annual, 2),
            "all_drugs_covered": all_covered,
        }

    for msg in custom_warnings:
        warnings.append({"drug": "Plan Request", "normalized_to": "", "flag": msg})

    conn.close()
    return {
        "zip_code": zip_code, "soa_date": soa_date,
        "months_remaining": month_names,
        "plan_summaries": plan_summaries,
        "drug_detail": results,
        "warnings": warnings,
    }


def build_pdf(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining, confidence=None, warnings=None, drug_detail_full=None, client_address=None, client_city=None):
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

    h1        = S("h1",  fontSize=9, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=12)
    h2        = S("h2",  fontSize=5,  textColor=colors.HexColor("#64748b"), leading=7)
    sec_title = S("sec", fontSize=7,  textColor=CHARCOAL, fontName="Helvetica-Bold", leading=9)
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
                            rightMargin=6*mm, leftMargin=6*mm,
                            topMargin=3*mm, bottomMargin=3*mm)
    elements = []

    # Header
    conf_text = f"Extraction confidence: {confidence:.0%}" if confidence else ""
    header_left = [[Paragraph(client_name, h1)],
                   [Paragraph(f"DOB: {dob}  ·  Zip: {zip_code}  ·  SOA Date: {soa_date}", h2)]]
    header_right = [[Paragraph("INTERNAL USE ONLY", badge_txt)],
                    [Paragraph(f"Generated: {datetime.today().strftime('%m/%d/%Y')}", gen_txt)],
                    [Paragraph("Data: CMS Medicare Formulary Q1 2026", gen_txt)],
                    [Paragraph(conf_text, S("ct", fontSize=6, textColor=colors.HexColor("#0d9488"), alignment=TA_RIGHT, leading=7))]]
    tl = Table([[Table(header_left, colWidths=[200*mm]),
                 Table(header_right, colWidths=[80*mm])]],
               colWidths=[200*mm, 80*mm])
    tl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))
    elements.append(tl)
    elements.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=0.5*mm))

    # Warnings banner
    if warnings:
        drug_warnings = [w for w in warnings if w.get("drug") != "Plan Request"]
        plan_warnings = [w for w in warnings if w.get("drug") == "Plan Request"]
        warn_rows = []
        if drug_warnings:
            warn_rows.append([
                Paragraph("⚠ Drug Verification Required", S("wh", fontSize=6, fontName="Helvetica-Bold", textColor=WARN_TEXT, leading=8)),
                Paragraph("Please verify the following before client meeting:", warn_s)
            ])
            for w in drug_warnings:
                drug = w.get("drug", "")
                normalized = w.get("normalized_to", "")
                flag = w.get("flag", "")
                note = f"{drug}"
                if normalized and normalized.lower() != drug.lower():
                    note += f" → interpreted as {normalized}"
                if flag:
                    note += f" — {flag}"
                warn_rows.append([Paragraph("", warn_s), Paragraph(note, warn_s)])
        if plan_warnings:
            warn_rows.append([
                Paragraph("⚠ Plan Request Notice", S("wh", fontSize=6, fontName="Helvetica-Bold", textColor=WARN_TEXT, leading=8)),
                Paragraph("The following agent-requested plans could not be added:", warn_s)
            ])
            for w in plan_warnings:
                warn_rows.append([Paragraph("", warn_s), Paragraph(w.get("flag", ""), warn_s)])
        if warn_rows:
            wt = Table(warn_rows, colWidths=[50*mm, 230*mm])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), WARN_BG),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#fed7aa")),
                ("TOPPADDING", (0,0), (-1,-1), 1),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1),
                ("LEFTPADDING", (0,0), (-1,-1), 4),
                ("SPAN", (0,0), (0,0)),
            ]))
            elements.append(wt)
            elements.append(Spacer(1, 0.15*mm))

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
            ("TOPPADDING", (0,0), (-1,-1), 1),
            ("BOTTOMPADDING", (0,0), (-1,-1), 1),
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
        elements.append(Spacer(1, 0.15*mm))
        t, ma_best = make_plan_table(ma_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.15*mm))

    if ma_plans and drug_detail:
        elements.append(Paragraph("SECTION 2 — DRUG FORMULARY TIERS", sec_title))
        elements.append(Spacer(1, 0.15*mm))
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
        elements.append(Spacer(1, 0.15*mm))

    if ma_plans and drug_detail:
        elements.append(Paragraph("SECTION 3 — PHARMACY COST COMPARISON BY PLAN", sec_title))
        elements.append(Spacer(1, 0.15*mm))

        if client_address and client_city:
            location_label = client_address + ", " + client_city
        else:
            location_label = "ZIP " + zip_code
        elements.append(Paragraph(
            "Nearest in-network pharmacies to " + location_label + "  ·  Costs include deductible phase where applicable",
            S("note", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)))
        elements.append(Spacer(1, 0.1*mm))

        def get_plan_pharmacy_summary(carrier):
            pharm_totals = {}
            for drug in drug_detail:
                pc_list = drug.get("plans", {}).get(carrier, {}).get("pharmacy_costs", [])
                for pc in pc_list:
                    name = pc["name"]
                    dist = pc.get("distance_miles", 99)
                    annual = pc.get("annual_total", 0) or 0
                    if name not in pharm_totals:
                        pharm_totals[name] = {"annual": 0, "distance": dist, "monthly": {}}
                    pharm_totals[name]["annual"] += annual
                    for m in pc.get("monthly_costs", []):
                        mn = m["month"]
                        pharm_totals[name]["monthly"][mn] = pharm_totals[name]["monthly"].get(mn, 0) + (m["cost"] or 0)
            if not pharm_totals:
                return None
            min_cost = min(v["annual"] for v in pharm_totals.values())
            cheapest = sorted(
                [{"name": k, "distance": v["distance"], "annual": v["annual"], "monthly": v["monthly"], "is_runner_up": False}
                 for k, v in pharm_totals.items() if abs(v["annual"] - min_cost) <= 1.0],
                key=lambda x: x["distance"]
            )
            if len(cheapest) == 1:
                others = sorted(
                    [{"name": k, "distance": v["distance"], "annual": v["annual"], "monthly": v["monthly"], "is_runner_up": True, "cost_diff": round(v["annual"] - min_cost, 2)}
                     for k, v in pharm_totals.items() if abs(v["annual"] - min_cost) > 1.0],
                    key=lambda x: x["annual"]
                )
                if others:
                    cheapest.append(others[0])
            mail_annual = sum(
                (drug.get("plans", {}).get(carrier, {}).get("mail_order_costs", {}).get("annual_total", 0) or 0)
                for drug in drug_detail
            )
            monthly = cheapest[0]["monthly"] if cheapest else {}
            costs_by_month = [(mn, monthly.get(mn, 0)) for mn in months_remaining] if months_remaining else []
            transitions = []
            prev_cost = None
            for mn, cost in costs_by_month:
                if cost != prev_cost:
                    transitions.append((mn[:3], cost))
                    prev_cost = cost
            return {
                "min_annual": min_cost,
                "cheapest": cheapest,
                "mail_annual": mail_annual,
                "transitions": transitions,
                "all_same": len(transitions) <= 1
            }

        hdr_s  = S("sh",  fontSize=6, textColor=WHITE, fontName="Helvetica-Bold", leading=8, alignment=TA_LEFT)
        plan_s = S("ps",  fontSize=6, textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
        sub_s2 = S("ss2", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)
        cost_s = S("cs",  fontSize=7, textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
        trans_s= S("ts2", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)
        pharm_s= S("phs", fontSize=6, textColor=DARK_GRAY, leading=8)
        mail_s = S("ms2", fontSize=7, textColor=DARK_GRAY, fontName="Helvetica-Bold", leading=9)
        save_s = S("sv",  fontSize=5, textColor=GREEN_TEXT, leading=7)
        runner_s=S("rs",  fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)

        plan_col_w = 42*mm
        pharm_col_w= 62*mm
        cost_col_w = 88*mm
        mail_col_w = 82*mm
        total_w = plan_col_w + pharm_col_w + cost_col_w + mail_col_w
        scale = 274*mm / total_w
        plan_col_w *= scale; pharm_col_w *= scale; cost_col_w *= scale; mail_col_w *= scale

        inner_ts = TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),1),
            ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
        ])

        rows = [[
            Paragraph("Plan", hdr_s),
            Paragraph("Cheapest pharmacy", hdr_s),
            Paragraph("Monthly retail cost", hdr_s),
            Paragraph("Mail order / mo", hdr_s),
        ]]

        carriers = list(ma_plans.keys())
        for carrier in carriers:
            plan_data = ma_plans[carrier]
            summary = get_plan_pharmacy_summary(carrier)
            is_best = carrier == ma_best
            star = " ★" if is_best else ""

            plan_cell = Table([
                [Paragraph(carrier + star, plan_s)],
                [Paragraph("$" + "{:.2f}".format(plan_data["premium_monthly"]) + " prem · $" + "{:.0f}".format(plan_data["deductible"]) + " ded", sub_s2)]
            ], colWidths=[plan_col_w - 3*mm], style=inner_ts)

            if not summary:
                rows.append([plan_cell, Paragraph("No data", sub_s2), Paragraph("—", cell), Paragraph("—", cell)])
                continue

            pharm_lines = []
            for p in summary["cheapest"][:3]:
                name = p["name"].split("#")[0].strip()[:20]
                dist = str(p["distance"]) + " mi"
                if p.get("is_runner_up"):
                    diff = p.get("cost_diff", 0)
                    pharm_lines.append(Paragraph(name + "  (" + dist + ")  +$" + "{:.0f}".format(diff) + "/yr", runner_s))
                else:
                    pharm_lines.append(Paragraph(name + "  (" + dist + ")", pharm_s))
            pharm_cell = Table([[p] for p in pharm_lines], colWidths=[pharm_col_w - 3*mm], style=inner_ts)

            if summary["all_same"]:
                monthly_amt = summary["min_annual"] / len(months_remaining) if months_remaining else 0
                cost_parts = [Paragraph("$" + "{:.2f}".format(monthly_amt) + " / mo", cost_s)]
            else:
                t_parts = []
                for i, (mn, cost) in enumerate(summary["transitions"][:4]):
                    if i < len(summary["transitions"]) - 1:
                        t_parts.append(mn + " $" + "{:.0f}".format(cost))
                    else:
                        t_parts.append(mn + " $" + "{:.2f}".format(cost) + " steady")
                steady = summary["transitions"][-1][1] if summary["transitions"] else 0
                cost_parts = [
                    Paragraph("$" + "{:.2f}".format(steady) + " / mo", cost_s),
                    Paragraph("  →  ".join(t_parts), trans_s)
                ]
            cost_cell = Table([[p] for p in cost_parts], colWidths=[cost_col_w - 3*mm], style=inner_ts)

            mail_monthly = summary["mail_annual"] / len(months_remaining) if months_remaining else 0
            savings = summary["min_annual"] - summary["mail_annual"]
            mail_parts = [Paragraph("$" + "{:.2f}".format(mail_monthly) + " / mo", mail_s)]
            if savings > 1:
                mail_parts.append(Paragraph("Save $" + "{:.0f}".format(savings) + "/yr vs retail", save_s))
            mail_cell = Table([[p] for p in mail_parts], colWidths=[mail_col_w - 3*mm], style=inner_ts)

            rows.append([plan_cell, pharm_cell, cost_cell, mail_cell])

        t = Table(rows, colWidths=[plan_col_w, pharm_col_w, cost_col_w, mail_col_w])
        ts_list = [
            ("BACKGROUND", (0,0), (-1,0), CHARCOAL),
            ("GRID", (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        ]
        if ma_best in carriers:
            bi = carriers.index(ma_best) + 1
            ts_list += [
                ("BACKGROUND", (0,bi), (-1,bi), GREEN_BG),
                ("LINEABOVE", (0,bi), (-1,bi), 1, TEAL),
                ("LINEBELOW", (0,bi), (-1,bi), 1, TEAL),
            ]
        t.setStyle(TableStyle(ts_list))
        elements.append(t)
        elements.append(Spacer(1, 0.15*mm))


    if pd_plans:
        elements.append(Paragraph("SECTION 4 — PART D STANDALONE PLANS", sec_title))
        elements.append(Spacer(1, 0.1*mm))
        t, _ = make_plan_table(pd_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.15*mm))

    elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceBefore=0.5*mm, spaceAfter=0.5*mm))
    elements.append(Paragraph(
        "Internal use only · Agent reference · CMS Medicare Q1 2026 · Verify before presenting", footer))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


@app.route("/health", methods=["GET"])
def health():
    conn = get_db()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    zip_county = conn.execute("SELECT COUNT(*) FROM zip_county").fetchone()[0] if "zip_county" in tables else 0
    service_area = conn.execute("SELECT COUNT(*) FROM service_area").fetchone()[0] if "service_area" in tables else 0
    plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    pharm_net = conn.execute("SELECT COUNT(DISTINCT contract_id||plan_id) FROM pharmacy_network").fetchone()[0]
    # Test zip lookup for 55309
    county_55309 = None
    if "zip_county" in tables:
        r = conn.execute("SELECT county_name FROM zip_county WHERE zip='55309'").fetchone()
        county_55309 = r[0] if r else "NOT FOUND"
    conn.close()
    return jsonify({
        "status": "ok", "db": os.path.exists(DB_PATH),
        "tables": tables, "zip_county_rows": zip_county,
        "service_area_rows": service_area, "plans": plans,
        "pharmacy_network_plans": pharm_net,
        "zip_55309_county": county_55309
    })


@app.route("/plans", methods=["GET"])
def list_plans():
    """List all available plans in the database for custom plan lookup."""
    conn = get_db()
    rows = conn.execute("""
        SELECT contract_id, plan_id, plan_name, premium, deductible
        FROM plans ORDER BY contract_id, plan_id
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/test-geocode", methods=["GET"])
def test_geocode():
    """Test multiple geocoding services from Railway."""
    results = {}
    
    # Test 1: Census
    try:
        import urllib.parse as urlparse
        params = urlparse.urlencode({"street": "7203 Birch Lane", "city": "Woodbury", "state": "MN", "zip": "55125", "benchmark": "2020", "format": "json"})
        r = requests.get("https://geocoding.geo.census.gov/geocoder/locations/address?" + params, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        results["census"] = r.status_code
    except Exception as e:
        results["census"] = str(e)[:50]
    
    # Test 2: Nominatim
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search?q=7203+Birch+Lane+Woodbury+MN&format=json&limit=1", timeout=5, headers={"User-Agent": "MedicareTool/1.0"})
        data = r.json()
        results["nominatim"] = {"status": r.status_code, "found": len(data) > 0, "lat": data[0]["lat"] if data else None}
    except Exception as e:
        results["nominatim"] = str(e)[:50]
    
    # Test 3: Zippopotam (we know this worked for zip coords)
    try:
        r = requests.get("https://api.zippopotam.us/us/55125", timeout=5)
        results["zippopotam"] = {"status": r.status_code, "data": r.json()}
    except Exception as e:
        results["zippopotam"] = str(e)[:50]

    # Test 4: Photon (OSM based, different server)
    try:
        r = requests.get("https://photon.komoot.io/api/?q=7203+Birch+Lane+Woodbury+MN&limit=1", timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        features = data.get("features", [])
        results["photon"] = {"status": r.status_code, "found": len(features) > 0, "coords": features[0]["geometry"]["coordinates"] if features else None}
    except Exception as e:
        results["photon"] = str(e)[:50]

    return jsonify(results)


@app.route("/debug-costs", methods=["POST"])
def debug_costs():
    """Debug endpoint - returns raw compute result to verify Section 3 data."""
    data = request.get_json(force=True, silent=True) or {}
    drug_names = data.get("drug_names", "")
    drug_dosages = data.get("drug_dosages", "")
    zip_code = data.get("zip_code", "55309")
    soa_date = data.get("soa_date", "02/05/2026")
    
    names = [n.strip() for n in drug_names.split(",") if n.strip()]
    dosages = [d.strip() for d in drug_dosages.split(",")] if drug_dosages else []
    drugs = [{"name": names[i], "dosage": dosages[i] if i < len(dosages) else ""}
             for i in range(len(names))]
    
    result = compute_drug_costs(drugs, zip_code, soa_date)
    
    # Extract just Section 3 relevant data
    section3_debug = []
    best_carrier = min(
        {k: v for k, v in result["plan_summaries"].items() if v["plan_type"] == "MA"}.keys(),
        key=lambda c: (result["plan_summaries"][c]["total_drug_plus_premium"],
                      result["plan_summaries"][c]["deductible"])
    )
    
    for drug in result["drug_detail"]:
        plan_data = drug.get("plans", {}).get(best_carrier, {})
        pharm_costs = plan_data.get("pharmacy_costs", [])
        section3_debug.append({
            "drug": drug.get("drug_name"),
            "has_pharmacy_costs": len(pharm_costs) > 0,
            "pharmacy_count": len(pharm_costs),
            "first_pharmacy": pharm_costs[0]["name"] if pharm_costs else None,
            "first_pharmacy_june": next(
                (m["cost"] for m in pharm_costs[0]["monthly_costs"] if m["month"] == "June"), None
            ) if pharm_costs else None,
        })
    
    return jsonify({
        "best_carrier": best_carrier,
        "section3_debug": section3_debug
    })


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
    client_address = data.get("client_address", "")
    client_city = data.get("client_city", "")
    client_state = data.get("client_state", "MN")
    confidence = data.get("confidence")
    custom_plans_str = data.get("custom_plans", "")
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
        result = compute_drug_costs(drugs, zip_code, soa_date,
                                    client_address=client_address,
                                    client_city=client_city,
                                    client_state=client_state,
                                    custom_plans_str=custom_plans_str)
    except Exception as e:
        return jsonify({"error": f"Drug cost computation failed: {str(e)}"}), 500

    try:
        pdf_bytes_out = build_pdf(
            client_name, dob, zip_code, soa_date,
            result["plan_summaries"],
            result["drug_detail"],
            result["months_remaining"],
            confidence=confidence,
            warnings=result.get("warnings", []),
            client_address=client_address,
            client_city=client_city
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
