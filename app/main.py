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
import re
import base64
import requests
from datetime import datetime, date

app = Flask(__name__)

from app import data_meta
from app import drug_resolver   # local RxNorm drug-name reader (2026-10-07)
DB_PATH           = data_meta.DB_PATH
# Doctor/clinic networks: 2027 sources only (app/providers_2027.py, providers_2027.db). The 2026
# carrier directory databases were retired 2026-10-06.
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

# Friendly name lookup — short labels for known plans (module level since 2026-10-02 so the
# agent-chosen-plans path can use the same labels as the automatic path).
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
    ("H8889", "003"): "Medica Advantage",
    ("H8889", "004"): "Medica Advantage",
    ("H8889", "005"): "Medica Advantage",
    ("H8889", "008"): "Medica Advantage",
    ("H8889", "009"): "Medica Advantage (No Rx)",
    ("H8889", "010"): "Medica Value",
    ("H8889", "011"): "Medica Preferred",
    ("H8889", "012"): "Medica Select",
    ("H8889", "013"): "Medica Preferred",
    ("H8889", "014"): "Medica Value",
    ("H8889", "015"): "Medica Select",
    ("H8889", "017"): "Medica Value",
    ("H8889", "018"): "Medica Select",
    ("H2450", "002"): "Medica Cost Enhanced",
    ("H2450", "007"): "Medica Cost Thrift",
    ("H2450", "016"): "Medica Cost Basic",
    ("H2450", "035"): "Medica Cost Core",
    ("H2450", "037"): "Medica Cost Premier",
    ("H2450", "039"): "Medica Cost Focus",
    ("H2450", "049"): "Medica Cost Standard",
    ("H5216", "275"): "Humana Choice",
    ("H5216", "063"): "Humana Choice",
    ("H5216", "092"): "Humana Choice",
    ("H5216", "359"): "Humana Choice",
    ("H3219", "001"): "Aetna Signature",
    ("H3219", "002"): "Aetna Enhanced",
    ("H3219", "003"): "Aetna Grand",
    ("H3219", "004"): "Aetna Grand Extra",
    ("H3219", "005"): "Aetna Eagle",
    ("H3219", "008"): "Aetna Signature Fit",
    ("H3219", "012"): "Aetna Signature",
    ("H3219", "014"): "Aetna Enhanced",
    ("H2001", "116"): "UHC",
    ("H2001", "117"): "UHC",
    ("H2001", "118"): "UHC FG",
    ("H2001", "119"): "UHC FG",
    ("H2001", "120"): "UHC FG",
    ("H2001", "123"): "UHC",
    ("H3186", "001"): "Align ChoiceElite",
    ("H3186", "002"): "Align ChoicePlus",
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

# Where the plan data comes from. Shown to agents in the plan picker and on the reports. Read from
# the database's meta table (2026-10-06); "CMS Q1 2026" for the original 2026 database.
DATA_VINTAGE = data_meta.DEFAULTS["data_vintage"]


def data_vintage():
    return data_meta.data_vintage(DB_PATH)


def plan_label(conn, cid, pid, plan_name=None):
    """Short label agents see for a plan. A plan-year database with a meta table stores its own
    labels (plans.carrier, made by the yearly builder); the original 2026 database uses the
    hand-written FRIENDLY_NAMES. Last resort: the CMS plan name, shortened."""
    pid = str(pid).zfill(3)
    if data_meta.has_meta(DB_PATH):
        row = conn.execute("SELECT carrier FROM plans WHERE contract_id=? AND plan_id=? AND carrier IS NOT NULL "
                           "AND carrier != '' LIMIT 1", (cid, pid)).fetchone()
        if row:
            return row[0]
    elif (cid, pid) in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[(cid, pid)]
    return plan_name[:35] if plan_name else cid


def calendar_month(month_num):
    """'January'..'December' for 1..12 (no year involved)."""
    import calendar
    return calendar.month_name[int(month_num)]


def _table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _place_key(name):
    """'St. Paul', 'Saint Paul city', 'st paul' -> 'st paul' (Census place names vs what people write)."""
    import re as _re
    n = (name or "").lower().replace(".", " ").replace(",", " ")
    n = _re.sub(r"\bsaint\b", "st", n)
    n = _re.sub(r"\b(city|township|cdp|village)\b", " ", n)
    return " ".join(n.split())


def county_from_address(address, city, state, zip_code):
    """County name for a street address from the Census Bureau geocoder, or None. Sends street,
    city, state and ZIP only (no name). Switch off with env COUNTY_ADDRESS_LOOKUP=off."""
    try:
        r = requests.get("https://geocoding.geo.census.gov/geocoder/geographies/address", timeout=6, params={
            "street": address, "city": city or "", "state": state or "MN", "zip": zip_code,
            "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"})
        matches = r.json()["result"]["addressMatches"]
        if matches:
            return matches[0]["geographies"]["Counties"][0]["BASENAME"]
    except Exception:
        pass
    return None


def county_choice(conn, zip_code, address=None, city=None, state=None):
    """Which county a client is in (2026-10-06). Returns
        {"county", "exact", "method", "counties": [{"county", "share"}], "note"}
    method: "only" (ZIP in one county), "address" (Census lookup of the street address), "city"
    (the city lies in just one of the ZIP's counties), "largest" (the county with most of the ZIP's
    land - an assumption the note tells the agent about), "nearby" (ZIP unknown: a neighbouring ZIP)."""
    out = {"county": None, "exact": False, "method": None, "counties": [], "note": ""}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "service_area" not in tables or "zip_county" not in tables:
        return out
    zip_code = str(zip_code or "").strip()
    has_share = "land_share" in _table_cols(conn, "zip_county")
    q = ("SELECT county_name, land_share FROM zip_county WHERE zip = ? ORDER BY land_share DESC" if has_share
         else "SELECT county_name, 1.0 FROM zip_county WHERE zip = ?")
    rows = conn.execute(q, (zip_code,)).fetchall()
    if not rows:
        if not zip_code.isdigit():
            return out
        for delta in [1, -1, 2, -2, 3, -3]:
            alt = conn.execute(q, (str(int(zip_code) + delta).zfill(5),)).fetchone()
            if alt:
                out.update(county=alt[0], method="nearby", counties=[{"county": alt[0], "share": 1.0}])
                return out
        return out
    counties = [{"county": r[0], "share": round(float(r[1] or 0), 3)} for r in rows]
    names = [c["county"] for c in counties]
    out.update(exact=True, counties=counties)
    if len(names) == 1:
        out.update(county=names[0], method="only")
        return out
    chosen = method = None
    if address and os.environ.get("COUNTY_ADDRESS_LOOKUP", "on").lower() not in ("off", "0", "false", "no"):
        c = county_from_address(address, city, state, zip_code)
        if c in names:
            chosen, method = c, "address"
    if not chosen and city and "place_county" in tables:
        hits = {r[0] for r in conn.execute("SELECT county_name FROM place_county WHERE place = ?",
                                           (_place_key(city),))} & set(names)
        if len(hits) == 1:
            chosen, method = hits.pop(), "city"
    if not chosen:
        chosen, method = names[0], "largest"
    how = {"address": "from the street address", "city": f"from the city, {city}",
           "largest": "the county covering most of the ZIP - check with the client"}[method]
    others = [n for n in names if n != chosen]
    out.update(county=chosen, method=method,
               note=f"ZIP {zip_code} is also in {' and '.join(others)} "
                    f"{'County' if len(others) == 1 else 'counties'}. Plans shown for {chosen} County ({how}).")
    return out


def resolve_county(conn, zip_code, address=None, city=None, state=None):
    """(county, exact) for a ZIP, or (None, False). exact=False means the ZIP itself wasn't in
    zip_county and a neighbouring ZIP's county was used. See county_choice for the full answer."""
    c = county_choice(conn, zip_code, address, city, state)
    return c["county"], c["exact"]


def plan_premium_deductible(conn, cid, pid, row):
    """(monthly premium, drug deductible) exactly as both reports show them: the plans table
    first, the service-area row as fallback. Shared with the plan picker (2026-10-02) so the
    picker can never show a different premium than the PDFs."""
    plan_row = conn.execute(
        "SELECT premium, deductible FROM plans WHERE contract_id=? AND plan_id=?", (cid, pid)
    ).fetchone()
    premium = float(plan_row[0]) if plan_row and plan_row[0] is not None else float(row[4] or 0)
    deductible = float(plan_row[1]) if plan_row and plan_row[1] is not None else float(row[5] or 0)
    return premium, deductible


def eligible_plan_rows(conn, county):
    """(ma_rows, pd_rows) every plan a client in this county may be shown. The ONE eligibility
    rule shared by the automatic report path, the plan picker and agent-chosen plans, so the
    picker can never offer a plan the reports can't render. MA/Cost: no SNP, no PACE, must have
    a formulary. PD: standalone PDPs with a formulary. A plan-year database may also mark plans whose
    carrier has not published its drug list yet (plans.drug_list_status) - those stay eligible and
    the reports say "drug list not out yet" instead of pricing them (2026-10-06)."""
    pending = ("OR p.drug_list_status = 'not published yet'"
               if "drug_list_status" in _table_cols(conn, "plans") else "")
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
        AND (p.formulary_id IS NOT NULL {pending})
        GROUP BY sa.contract_id, sa.plan_id
        ORDER BY MIN(sa.premium_total) ASC, sa.plan_name ASC
    """.format(pending=pending), (county,)).fetchall()

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
        AND (p.formulary_id IS NOT NULL {pending})
        GROUP BY sa.contract_id, sa.plan_id
        ORDER BY MIN(sa.premium_total) ASC
    """.format(pending=pending), (county,)).fetchall()
    return ma_rows, pd_rows


MAX_SELECTED_MA = 7     # internal report layout limit (Section 1 columns)
MAX_SELECTED_PD = 3     # internal report Section 4 limit


def resolve_selected_plans(conn, zip_code, selected, max_ma=MAX_SELECTED_MA, max_pd=MAX_SELECTED_PD,
                           address=None, city=None, state=None):
    """Agent-chosen plans (2026-10-02) -> (plans, county, error).

    `selected` is the ordered list the app sends: [{"contract_id": "H4882", "plan_id": "009"}, ...].
    Returns plan dicts shaped exactly like get_plans_for_zip's, IN PICK ORDER, with a UNIQUE
    "carrier" label each (two plans can share a friendly label, e.g. H3219-001 and H3219-012 are
    both "Aetna Signature"; compute_drug_costs keys everything by that label, so a duplicate would
    silently overwrite a plan). Never drops a plan silently: any problem -> error string (HTTP 400).
    """
    if not isinstance(selected, list) or not selected:
        return None, None, "selected_plans must be a non-empty list"
    choice = county_choice(conn, zip_code, address, city, state)
    county = choice["county"]
    if not county:
        return None, None, f"Can't find a county for ZIP {zip_code}"
    # A ZIP that crosses county lines: plans from ANY of its counties may be picked (the agent may
    # know the client's county better than the ZIP does).
    eligible, plan_counties = {}, {}
    for cty in [c["county"] for c in choice["counties"]] or [county]:
        ma_rows, pd_rows = eligible_plan_rows(conn, cty)
        for r in ma_rows:
            eligible.setdefault((r[0], str(r[1]).zfill(3)), ("Cost" if "Cost" in (r[6] or "") else "MA", r))
            plan_counties.setdefault((r[0], str(r[1]).zfill(3)), []).append(cty)
        for r in pd_rows:
            eligible.setdefault((r[0], str(r[1]).zfill(3)), ("PD", r))
            plan_counties.setdefault((r[0], str(r[1]).zfill(3)), []).append(cty)

    plans, seen, used_labels = [], set(), set()
    n_ma = n_pd = 0
    for item in selected:
        if not isinstance(item, dict):
            return None, county, "Each selected plan must be an object with contract_id and plan_id"
        cid = str(item.get("contract_id") or "").strip().upper()
        pid = str(item.get("plan_id") or "").strip().zfill(3)
        if not cid or not pid.strip("0") and pid != "000":
            return None, county, "Each selected plan needs a contract_id and plan_id"
        key = (cid, pid)
        if key in seen:
            return None, county, f"Plan {cid}-{pid} was selected more than once"
        seen.add(key)
        if key not in eligible:
            return None, county, f"Plan {cid}-{pid} is not available in {county} County"
        ptype, row = eligible[key]
        if ptype == "PD":
            n_pd += 1
        else:
            n_ma += 1
        premium, deductible = plan_premium_deductible(conn, cid, pid, row)
        label = plan_label(conn, cid, pid, row[2])
        if label in used_labels:
            label = f"{label} ({cid}-{pid})"
        used_labels.add(label)
        plans.append({
            "carrier": label, "contract_id": cid, "plan_id": pid, "type": ptype,
            "landscape_premium": premium, "landscape_deductible": deductible,
            "counties": plan_counties.get(key, []),
        })
    # The report's county: the assumed one, unless every picked MA plan is only sold in another
    # county of this ZIP - then the agent has told us where the client lives.
    ma_keys = [(p["contract_id"], p["plan_id"]) for p in plans if p["type"] != "PD"]
    if ma_keys and not any(county in plan_counties.get(k, []) for k in ma_keys):
        common = set.intersection(*[set(plan_counties.get(k, [])) for k in ma_keys])
        if len(common) == 1:
            county = common.pop()
    if n_ma > max_ma:
        return None, county, f"Too many Medicare Advantage plans selected ({n_ma}); the limit is {max_ma}"
    if n_pd > max_pd:
        return None, county, f"Too many Part D plans selected ({n_pd}); the limit is {max_pd}"
    return plans, county, None


def get_plans_for_zip(conn, zip_code, address=None, city=None, state=None):
    """
    Dynamically load plans available for a client zip code.
    Uses service_area + zip_county tables if available.
    Falls back to hardcoded FALLBACK_PLANS.
    """
    county, _exact = resolve_county(conn, zip_code, address, city, state)
    if not county:
        return FALLBACK_PLANS

    ma_rows, pd_rows = eligible_plan_rows(conn, county)
    # automatic picks only from plans we can price (a plan whose drug list isn't out yet is shown
    # in the picker for the agent to choose deliberately, never picked for them)
    ma_rows = [r for r in ma_rows if r[7]]
    pd_rows = [r for r in pd_rows if r[7]]
    if not ma_rows and not pd_rows:
        return FALLBACK_PLANS

    plans = []
    seen = set()


    # Group by carrier family — show only the lowest-premium plan per carrier
    # e.g. HealthPartners shows Journey Pace ($0), not all 4 plans
    CARRIER_FAMILY = {
        "H4882": "HealthPartners", "H6309": "HealthPartners",
        "H5959": "Blue Cross",
        "H6154": "Medica", "H8889": "Medica", "H2450": "Medica Cost",
        "H5216": "Humana", "H8145": "Humana",
        "H3219": "Aetna",
        "H2001": "UHC",
        "H3186": "Align",
        "H9834": "Quartz",
        "H2461": "Blue Cross Cost",
    }

    # Pick best plan per carrier family (lowest premium from plans table, prefer $0)
    # service_area tells us WHICH plans are available, plans table has correct premiums
    # Plan type preference order: PPO > HMO-POS > Cost > PFFS > HMO
    PLAN_TYPE_RANK = {"PPO": 0, "HMO-POS": 1, "Cost": 2, "PFFS": 3, "HMO": 4}

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
        plan_type = row[6] or ""
        type_rank = PLAN_TYPE_RANK.get(plan_type, 5)
        current = best_per_family.get(family)
        curr_rank = PLAN_TYPE_RANK.get(current["plan_type"] or "", 5) if current else 5
        is_better = (
            current is None or
            premium < current["premium"] or
            (premium == current["premium"] and type_rank < curr_rank) or
            (premium == current["premium"] and type_rank == curr_rank and deductible < current["deductible"])
        )
        if is_better:
            best_per_family[family] = {
                "contract_id": cid, "plan_id": pid,
                "plan_name": row[2], "org_name": row[3],
                "premium": premium, "deductible": deductible,
                "plan_type": plan_type, "key": key
            }

    # First pass: best plan per carrier family
    for family, row in sorted(best_per_family.items(), key=lambda x: x[1]["premium"]):
        key = row["key"]
        if key in seen:
            continue
        seen.add(key)
        carrier = plan_label(conn, key[0], key[1], row["plan_name"])
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
            carrier = plan_label(conn, key[0], key[1], row["plan_name"])
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
        "S5743": "MedicareBlue", "S5921": "UHC", "S5660": "Cigna",
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
        carrier = plan_label(conn, key[0], key[1], d["plan_name"])
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
        "elite", "plus", "standard", "focus", "thrift", "essential", "gold", "securechoice", "assurance",
    }

    def _friendly(cid, pid, plan_name):
        """Short label. plan_name '' = scoring (unknown -> ''); otherwise unknown -> the plan name / contract.
        Plan-year databases carry their own labels; the original 2026 database uses FN below."""
        if data_meta.has_meta(DB_PATH):
            label = plan_label(conn, cid, pid, plan_name or None)
            return "" if (plan_name == "" and label == cid) else label
        if plan_name == "":
            return FN.get((cid, pid), "")
        return FN.get((cid, pid), plan_name[:35] if plan_name else cid)
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
        ("H2001","116"): "UHC",
        ("H2001","117"): "UHC",
        ("H2001","118"): "UHC FG",
        ("H2001","119"): "UHC FG",
        ("H2001","120"): "UHC FG",
        ("H2001","123"): "UHC",
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
        "H3219": "Aetna", "H2001": "UHC", "H3186": "Align", "H9834": "Quartz",
    }
    all_plans = conn.execute("SELECT contract_id, plan_id, plan_name, premium, deductible FROM plans").fetchall()

    def score_plan(req_str, cid, pid, plan_name):
        req = req_str.lower().strip()
        pname = plan_name.lower() if plan_name else ""
        friendly = _friendly(cid, pid.zfill(3), "").lower()
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
            friendly = _friendly(key[0], key[1], best_plan[2])
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

drug_resolver.warm()            # build the local RxNorm index in the background (about a second)


def get_mfp(drug_name):
    """Return CMS negotiated MFP for a drug, or None if not in program. The plan-year database's
    negotiated_prices table when it has one (2027: 25 drugs), else the 2026 list above."""
    if not drug_name:
        return None
    table = data_meta.negotiated_prices(DB_PATH)
    return (table if table is not None else MFP_2026).get(drug_name.lower().strip())

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Safety net (2026-10-02): injectables the agency always verifies by hand, even when the
# Claude normalization step forgets to mark them. Insulin is deliberately NOT here: it has
# its own $35/month cap pricing. Oral forms (e.g. Rybelsus = oral semaglutide) are not listed.
KNOWN_INJECTABLE_BRANDS = (
    "ozempic", "wegovy", "trulicity", "mounjaro", "zepbound", "victoza", "saxenda", "byetta",
    "bydureon", "adlyxin", "soliqua", "xultophy", "humira", "enbrel", "prolia", "repatha",
    "praluent", "forteo", "tymlos", "dupixent", "stelara", "cosentyx", "aimovig", "emgality", "ajovy",
)
_INJECTABLE_WORDS = (" pen", "injection", "injectable", "autoinjector", "syringe", "prefilled")


def is_known_injectable(*texts):
    low = " " + " ".join((t or "") for t in texts).lower()
    if is_insulin(low):                 # all insulins (incl. brands like Lantus) keep $35-cap pricing
        return False
    return any(b in low for b in KNOWN_INJECTABLE_BRANDS) or any(w in low for w in _INJECTABLE_WORDS)


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


def load_zip_coords(conn):
    """Every ZIP centroid as {zip: (lat, lon)}, read once per request (2026-10-08). The pharmacy
    search used to look each network ZIP up one query at a time, for every plan: ~12,000 small
    queries to price 29 plans. Same values, one query."""
    return {z: (lat, lon) for z, lat, lon in conn.execute("SELECT zip, lat, lon FROM zip_coords")}


def get_nearby_pharmacies(conn, contract_id, plan_id, client_zip, max_results=4, max_miles=30,
                          client_address=None, client_city=None, client_state=None,
                          client_coords=None, zip_coords=None):
    """
    Find closest preferred retail pharmacies to client location.
    Uses real street address geocoding when available for precise distances.
    Falls back to zip centroid.

    client_coords / zip_coords (2026-10-08): a caller pricing many plans for one client passes the
    client's (lat, lon, city) and load_zip_coords() once, so the address is geocoded ONCE per
    request (not once per plan) and ZIP centroids come from memory. Results are identical.
    """
    plan_id_padded = plan_id.zfill(3)

    # Get client coordinates - prefer real address over zip centroid
    if client_coords is not None:
        client_lat, client_lon, client_city = client_coords
    else:
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

    # Does this plan have ANY preferred pharmacies anywhere in MN?
    # If not, all pharmacies are treated equally — don't show (non-pref) label
    plan_has_any_preferred = any(row[1] == "Y" for row in pref_rows)

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
                "plan_has_any_preferred": plan_has_any_preferred,
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
    # Fast path (2026-10-08, when the caller passes zip_coords): read this plan's in-network named
    # pharmacies ONCE and split them by ZIP in memory. Same rows, same order per ZIP as the per-ZIP
    # query below (the plan's network index is walked in the same order; npi is unique in
    # pharmacy_names, so DISTINCT over the plan = DISTINCT per ZIP) - proven identical by
    # _scratch/pharmacy_equivalence.py and tests/test_pharmacy_fast_path.py.
    plan_pharms_by_zip = None
    if zip_coords is not None:
        plan_pharms_by_zip = {}
        for row in conn.execute("""
            SELECT DISTINCT pn.npi, pn.name, pn.address, pn.city, pn.is_chain,
                   pn.lat, pn.lon,
                   net.generic_fee_30, net.brand_fee_30, net.selected_fee_30,
                   net.preferred_retail, pn.zip
            FROM pharmacy_names pn
            INNER JOIN pharmacy_network net ON net.npi = pn.npi
            WHERE net.contract_id = ?
            AND net.plan_id = ?
            AND net.is_retail = 1
            AND pn.name != 'Unknown Pharmacy'
            AND pn.name IS NOT NULL
        """, (contract_id, plan_id_padded)):
            plan_pharms_by_zip.setdefault(row[11], []).append(row[:11])
    for pharm_zip, info in zip_info.items():
        # Get zip coordinates
        if zip_coords is not None:
            coords = zip_coords.get(pharm_zip)
        else:
            coords = conn.execute(
                "SELECT lat, lon FROM zip_coords WHERE zip = ?", (pharm_zip,)
            ).fetchone()
        if not coords or not coords[0]:
            continue

        distance = haversine_distance(client_lat, client_lon, coords[0], coords[1])
        if distance > max_miles:
            continue

        # Get pharmacies in this zip that are confirmed in-network
        # Pull per-pharmacy dispensing fees directly (not zip-level aggregated)
        if plan_pharms_by_zip is not None:
            pharms = plan_pharms_by_zip.get(pharm_zip, [])
        else:
            pharms = conn.execute("""
            SELECT DISTINCT pn.npi, pn.name, pn.address, pn.city, pn.is_chain,
                   pn.lat, pn.lon,
                   net.generic_fee_30, net.brand_fee_30, net.selected_fee_30,
                   net.preferred_retail
            FROM pharmacy_names pn
            INNER JOIN pharmacy_network net ON net.npi = pn.npi
            WHERE pn.zip = ?
            AND net.contract_id = ?
            AND net.plan_id = ?
            AND net.is_retail = 1
            AND pn.name != 'Unknown Pharmacy'
            AND pn.name IS NOT NULL
            """, (pharm_zip, contract_id, plan_id_padded)).fetchall()

        for npi, name, address, city, is_chain, pharm_lat, pharm_lon, \
                generic_fee, brand_fee, selected_fee, pref_retail in pharms:
            if not name:
                continue
            # Use pharmacy's actual coordinates if available, else use zip centroid
            if pharm_lat and pharm_lon:
                precise_distance = haversine_distance(client_lat, client_lon, pharm_lat, pharm_lon)
                dist_approximate = False
            else:
                precise_distance = distance  # zip centroid fallback
                dist_approximate = True
            # Determine preferred status
            # Only mark as non-preferred if the plan actually distinguishes preferred/non-preferred
            plan_has_pref = info.get("plan_has_any_preferred", False)
            if not plan_has_pref:
                is_preferred = True
            elif info.get("has_preferred") and info.get("has_nonpreferred"):
                is_preferred = bool(is_chain)
            else:
                is_preferred = pref_retail == "Y"

            candidates.append({
                "npi": npi,
                "name": name,
                "address": address or "",
                "city": city or "",
                "zip": pharm_zip,
                "distance_miles": round(precise_distance, 1),
                "dist_approximate": dist_approximate,
                "preferred": is_preferred,
                "is_chain": bool(is_chain),
                "generic_fee": float(generic_fee or 0),
                "brand_fee": float(brand_fee or 0),
                "selected_fee": float(selected_fee or 0),
                "in_network": True,
                "_lat": pharm_lat,
                "_lon": pharm_lon,
            })

    if not candidates:
        return []

    # Deduplicate by base name, keep closest
    seen = {}
    for p in candidates:
        base = p["name"].split("#")[0].strip().upper()
        if base not in seen or p["distance_miles"] < seen[base]["distance_miles"]:
            seen[base] = p

    # Secondary dedup: remove pharmacies that are co-located (within 0.05 miles of each other)
    # Uses actual GPS distance between pharmacies, not distance from client
    # This catches same-campus pharmacies (e.g. Sanford Health Pharm + SANFORD CLINIC NORTH)
    # but NOT separate pharmacies that happen to be the same distance from the client
    by_distance = sorted(seen.values(), key=lambda x: x["distance_miles"])
    proximity_deduped = []
    for p in by_distance:
        p_lat = p.get("_lat")
        p_lon = p.get("_lon")
        too_close = False
        for kept in proximity_deduped:
            k_lat = kept.get("_lat")
            k_lon = kept.get("_lon")
            # If we have real coords for both, use actual distance between them
            if p_lat and p_lon and k_lat and k_lon:
                dist_between = haversine_distance(p_lat, p_lon, k_lat, k_lon)
                if dist_between < 0.1:
                    too_close = True
                    break
            else:
                # Fallback: if same distance from client (within 0.05mi) and same zip, likely co-located
                if (abs(p["distance_miles"] - kept["distance_miles"]) < 0.05 and
                        p.get("zip") == kept.get("zip")):
                    too_close = True
                    break
        if not too_close:
            proximity_deduped.append(p)

    # Sort: preferred chains first, then by distance
    sorted_pharms = sorted(
        proximity_deduped,
        key=lambda x: (not x["preferred"], not x["is_chain"], x["distance_miles"])
    )
    return sorted_pharms[:max_results]


def retail_cost_share(pref_type, pref_amt, std_type, std_amt, preferred=True):
    """(cost_type, cost_amt) for a one-month retail fill (2026-10-07).

    CMS gives every tier two retail columns: preferred pharmacies and standard pharmacies. A plan
    that has no preferred pharmacies gets cost_type 0 ("not applicable") in the preferred column,
    NOT a $0 copay - a real $0 copay is cost_type 1 with amount 0. Reading the unused column used
    to price brand drugs at $0 after the deductible on 49 of 65 plans (Margaret Ellison test).
    So: use the column for this pharmacy type; if the plan doesn't use it, use the other one."""
    first, second = ((pref_type, pref_amt), (std_type, std_amt))
    if not preferred:
        first, second = second, first
    for ct, amt in (first, second):
        if ct in (1, 2):
            return ct, float(amt or 0)
    return 0, 0.0


def pharmacy_fill_terms(conn, contract_id, plan_id, tier, unit_cost, pharmacy, drug_name="", use_mfp=True):
    """Price terms for one drug at one pharmacy (see app/drug_year.py). Same rules as
    get_drug_cost_at_pharmacy: preferred/non-preferred rates, brand vs generic dispensing fee,
    insulin $35, MFP drugs at 25% of the negotiated price."""
    plan_id_padded = plan_id.zfill(3)
    is_preferred = pharmacy.get("preferred", True)
    generic_fee = float(pharmacy.get("generic_fee", 0) or 0)
    brand_fee = float(pharmacy.get("brand_fee", 0) or 0)
    drug_name_base = drug_name.split()[0] if drug_name else ""
    if is_insulin(drug_name) or is_insulin(drug_name_base):
        return {"flat": 35.0}
    cost_row = conn.execute("""
        SELECT cost_type_pref, cost_amt_pref,
               cost_type_nonpref, cost_amt_nonpref,
               ded_applies
        FROM beneficiary_cost
        WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
        ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1
    """, (contract_id, plan_id_padded, tier)).fetchone()
    if not cost_row:
        return {"flat": 0.0}
    cost_type, cost_amt = retail_cost_share(cost_row[0], cost_row[1], cost_row[2], cost_row[3], is_preferred)
    disp_fee = (brand_fee if brand_fee > 0 else generic_fee) if (tier and tier >= 3) else generic_fee
    mfp = (get_mfp(drug_name_base) or get_mfp(drug_name)) if use_mfp else None
    if mfp is not None:
        unit_cost = mfp      # negotiated price; the plan's own tier cost-sharing still applies (2026-10-06)
        disp_fee = brand_fee if brand_fee > 0 else generic_fee
    return {"ded_applies": cost_row[4] != "N", "price": unit_cost, "fee": disp_fee,
            "cost_type": cost_type, "cost_amt": float(cost_amt or 0)}


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
    # Per-pharmacy dispensing fees — use brand fee for Tier 3+ (brand drugs), generic for Tier 1-2
    generic_fee = float(pharmacy.get("generic_fee", 0) or 0)
    brand_fee = float(pharmacy.get("brand_fee", 0) or 0)
    selected_fee = float(pharmacy.get("selected_fee", 0) or 0)
    # Default to generic fee; will be updated to brand fee after tier is known
    disp_fee = generic_fee

    # Get correct cost row based on preferred status
    cost_row = conn.execute("""
        SELECT cost_type_pref, cost_amt_pref,
               cost_type_nonpref, cost_amt_nonpref,
               ded_applies
        FROM beneficiary_cost
        WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
        ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1
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

    # Select dispensing fee based on tier — brand fee for Tier 3+, generic for Tier 1-2
    if tier and tier >= 3:
        disp_fee = brand_fee if brand_fee > 0 else generic_fee
    else:
        disp_fee = generic_fee

    # Apply insulin $35 cap first (overrides everything)
    drug_name_base = drug_name.split()[0] if drug_name else ""
    if is_insulin(drug_name) or is_insulin(drug_name_base):
        return 35.00, 0  # Flat $35, no deductible

    # Apply MFP override for federally negotiated drugs (2026)
    mfp = get_mfp(drug_name_base) or get_mfp(drug_name)
    is_mfp_drug = mfp is not None
    if is_mfp_drug:
        unit_cost = mfp      # negotiated price; the plan's own tier cost-sharing still applies (2026-10-06)
        # Use brand dispensing fee for MFP drugs (they are all brand drugs)
        disp_fee = brand_fee if brand_fee > 0 else generic_fee

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
        month_name = calendar_month(month_num)
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



NORMALIZE_MODEL = os.environ.get("NORMALIZE_MODEL", "claude-sonnet-5-5")


def _json_array(text):
    """The JSON array in a model reply, tolerating ```json fences or a sentence around it."""
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)


def normalize_drugs(drugs):
    """
    Cleans up drug names with Claude before they are looked up (misspellings, nicknames, brand vs generic).
    Returns one dict per drug:
      original, normalized, dosage, confidence, flag   (as before)
      ingredient   generic ingredient name(s), e.g. "ondansetron" for Zofran - how retired brands are read
      brand        brand name if one was written, else ""
    2026-10-07: current model (env NORMALIZE_MODEL), structured fields, temperature 0. If Claude is
    unreachable, the names go through unchanged and the local resolver still reads them.
    """
    if not drugs:
        return []

    drug_list = "\n".join([
        f"- {d.get('name', '')} {d.get('dosage', '')}".strip()
        for d in drugs if d.get('name', '').strip()
    ])

    if not drug_list:
        return []

    prompt = f"""You are a Medicare Part D pharmacist. Below are medications as written by a client or agent on an intake sheet or in an email. They may be misspelled, abbreviated, nicknamed, brand or generic, and may include the device (pen, inhaler) or directions.

Medications:
{drug_list}

For EACH line, in the same order, return:
- "original": the line exactly as written (name part only, without the dosage)
- "normalized": the drug name as it appears on Medicare drug lists. Keep a brand name if a brand was written. Fix spelling. Keep release type (ER, XL, DR). Leave out device and delivery words that are not part of the brand name (Trelegy Ellipta -> Trelegy, Lantus SoloStar -> Lantus, Humalog KwikPen -> Humalog, Ventolin HFA -> Ventolin).
- "ingredient": the generic ingredient name(s), e.g. "ondansetron" for Zofran, "sitagliptin and metformin" for Janumet, "insulin glargine" for Lantus. Required even for retired brands.
- "brand": the brand name if a brand was written, else "".
- "dosage": the strength as written (e.g. "10 mg", "100/62.5/25 mcg"); "" if none. Not the directions.
- "confidence": 0 to 1 - how sure you are which drug this is.
- "flag": a short note when unsure or when the line is not one specific drug (e.g. "water pill" - name the likely drug in "normalized" only if the line makes it clear), else "".

Never invent a drug. If a line is not identifiable, keep "normalized" as written, set confidence below 0.5 and explain in "flag".

Reply with ONLY the JSON array, no other text:
[{{"original": "", "normalized": "", "ingredient": "", "brand": "", "dosage": "", "confidence": 0.95, "flag": ""}}]"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": NORMALIZE_MODEL,
                "max_tokens": 4000,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        # The reply's TEXT block - newer models can put other blocks (e.g. thinking) first (2026-10-07).
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if data.get("stop_reason") == "max_tokens":
            raise ValueError("normalization reply was cut off (max_tokens)")
        items = _json_array(text)
        if not isinstance(items, list) or len(items) != len([d for d in drugs if d.get("name", "").strip()]):
            raise ValueError("normalization returned a different number of drugs")
        for it, d in zip(items, [d for d in drugs if d.get("name", "").strip()]):
            it.setdefault("original", d.get("name", ""))
            it.setdefault("normalized", d.get("name", ""))
            it.setdefault("dosage", d.get("dosage", ""))
            if not (it.get("dosage") or "").strip():
                it["dosage"] = d.get("dosage", "")       # never lose a strength that was written
            it.setdefault("confidence", 1.0)
            it.setdefault("flag", "")
            it.setdefault("ingredient", "")
            it.setdefault("brand", "")
        return items
    except Exception as exc:
        print(f"normalize_drugs: Claude unavailable or unreadable reply ({type(exc).__name__}); using names as written")
        return [{"original": d.get("name", ""), "normalized": d.get("name", ""), "ingredient": "", "brand": "",
                 "dosage": d.get("dosage", ""), "confidence": 1.0, "flag": ""} for d in drugs]


# Device / delivery names that are part of how people say a drug but NOT part of RxNorm's brand
# name (2026-10-07, Dorothy Halvorsen test): RxNorm knows "Trelegy" and "Lantus", not
# "Trelegy Ellipta" or "Lantus SoloStar", so those came back "couldn't identify".
DEVICE_WORDS = (
    "ellipta", "solostar", "max solostar", "flexpen", "flextouch", "kwikpen", "junior kwikpen", "tempo pen",
    "penfill", "inpen", "clickject", "sensoready", "pen-injector", "pen injector", "auto-injector",
    "autoinjector", "prefilled syringe", "prefilled pen", "pen", "pens", "inhaler", "hfa", "respimat",
    "diskus", "handihaler", "pressair", "aerosphere", "digihaler", "redihaler", "twisthaler", "neohaler",
    "vial", "cartridge", "u-100", "u100",
)
_DEVICE_RE = None


def strip_device_words(drug_name):
    """'Trelegy Ellipta' -> 'Trelegy'; 'Lantus SoloStar' -> 'Lantus'; 'Ozempic pen' -> 'Ozempic'.
    Whole words only ('Penicillin' is untouched). Returns '' if nothing but device words is left."""
    import re
    global _DEVICE_RE
    if _DEVICE_RE is None:
        words = sorted(DEVICE_WORDS, key=len, reverse=True)
        _DEVICE_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
    return re.sub(r"\s+", " ", _DEVICE_RE.sub(" ", drug_name or "")).strip()


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
        bare = strip_device_words(drug_name)          # "Trelegy Ellipta" -> "Trelegy"
        if bare and bare.lower() != drug_name.lower():
            rxcuis = fetch(bare)
    if not rxcuis:
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={requests.utils.quote(drug_name)}&search=2"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            rxcuis = list(data.get("idGroup", {}).get("rxnormId", []))
        except Exception:
            pass
    if not rxcuis:
        rxcuis = lookup_rxcuis_release_form(drug_name, dosage)
    return rxcuis


RELEASE_FORM_RE = None


def lookup_rxcuis_release_form(drug_name, dosage=""):
    """Fallback for drugs written with a release-form abbreviation (Metformin ER, Bupropion XL,
    Nifedipine ER, Metoprolol succinate ER...). RxNav's exact search doesn't understand "ER", so
    use its approximate search (RxNorm sources only), keep the top-ranked matches that are really
    this drug, and add each match's generic + brand versions (formularies list either one).
    Deliberately NOT used for plain misspellings ("Lisinpril"): those stay "verify the name"."""
    import re
    global RELEASE_FORM_RE
    if RELEASE_FORM_RE is None:
        RELEASE_FORM_RE = re.compile(r"\b(ER|XR|XL|SR|CR|DR|LA|EC|24\s?HR|12\s?HR)\b", re.I)
    text = f"{drug_name} {dosage}"
    if not drug_name or not RELEASE_FORM_RE.search(text):
        return []
    base = drug_name.split()[0].lower()
    found = []
    for term in ([f"{drug_name} {dosage}".strip(), drug_name] if dosage else [drug_name]):
        try:
            url = ("https://rxnav.nlm.nih.gov/REST/approximateTerm.json?term="
                   + requests.utils.quote(term) + "&maxEntries=20&option=1")
            cands = requests.get(url, timeout=8).json().get("approximateGroup", {}).get("candidate", []) or []
        except Exception:
            cands = []
        for c in cands:
            if str(c.get("rank")) == "1" and base in (c.get("name") or "").lower() and c["rxcui"] not in found:
                found.append(c["rxcui"])
        if found:
            break
    out = list(found)
    for rx in found[:4]:
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/rxcui/{rx}/related.json?tty=SCD+SBD"
            groups = requests.get(url, timeout=8).json().get("relatedGroup", {}).get("conceptGroup", []) or []
            for g in groups:
                for cp in g.get("conceptProperties", []) or []:
                    if cp.get("rxcui") and cp["rxcui"] not in out:
                        out.append(cp["rxcui"])
        except Exception:
            pass
    return out


def generic_equivalents(rxcuis):
    """The generic (SCD) versions of brand products (2026-10-07, Dorothy Halvorsen test).

    A brand that has gone generic (Januvia -> sitagliptin) is usually listed on a plan's drug list
    ONLY as the generic. Looking up "Januvia" gives the brand's RxCUIs, which then match nothing,
    and the plan was reported "Not covered" when it covers the generic (Aetna even at Tier 1).
    The pharmacy fills the generic automatically, so the agent needs that tier."""
    out = []
    for rx in list(rxcuis)[:6]:
        try:
            url = f"https://rxnav.nlm.nih.gov/REST/rxcui/{rx}/related.json?tty=SCD"
            groups = requests.get(url, timeout=8).json().get("relatedGroup", {}).get("conceptGroup", []) or []
            for g in groups:
                for cp in g.get("conceptProperties", []) or []:
                    r = cp.get("rxcui")
                    if r and r not in out and r not in rxcuis:
                        out.append(r)
        except Exception:
            pass
    return out


def get_drug_cost_for_plan(conn, formulary_id, contract_id, plan_id, rxcuis, deductible, months_remaining, drug_name='',
                           use_mfp=True):
    plan_id_padded = plan_id.zfill(3)
    # The plan's best tier for any of these drug IDs; on that tier, a package (NDC) the plan has a price
    # for, cheapest first (2026-10-07: generic sitagliptin's newer drug ID had no price, and the first
    # row found was used - a Tier 3 drug at 25% then cost $0).
    tier_row = None
    ids = list(dict.fromkeys(rxcuis))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        row = conn.execute(f"""
            SELECT f.tier AS tier, f.ndc AS ndc,
                   (SELECT p.unit_cost FROM pricing p WHERE p.contract_id = ? AND p.plan_id = ? AND p.ndc = f.ndc
                      AND p.days_supply = 30 LIMIT 1) AS price
            FROM formulary f
            WHERE f.formulary_id = ? AND f.rxcui IN ({",".join("?" * len(chunk))})
            ORDER BY f.tier ASC, (price IS NULL) ASC, price ASC LIMIT 1
        """, (contract_id, plan_id_padded, formulary_id, *chunk)).fetchone()
        if row and (tier_row is None or (row["tier"], row["price"] is None) < (tier_row["tier"], tier_row["price"] is None)):
            tier_row = row

    if not tier_row:
        return {"tier": None, "covered": False, "monthly_costs": [], "annual_total": None}

    tier = tier_row["tier"]
    ndc = tier_row["ndc"]

    cost_row = conn.execute("""
        SELECT cost_type_pref, cost_amt_pref, cost_type_nonpref, cost_amt_nonpref, ded_applies
        FROM beneficiary_cost
        WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 1
        ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1
    """, (contract_id, plan_id_padded, tier)).fetchone()

    if not cost_row:
        return {"tier": tier, "covered": True, "monthly_costs": [], "annual_total": None}

    cost_type, cost_amt = retail_cost_share(cost_row["cost_type_pref"], cost_row["cost_amt_pref"],
                                            cost_row["cost_type_nonpref"], cost_row["cost_amt_nonpref"])
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
            month_name = calendar_month(month_num)
            monthly_costs.append({"month": month_name, "cost": 35.00})
        return {
            "tier": tier, "covered": True, "ndc": ndc,
            "monthly_costs": monthly_costs,
            "annual_total": round(35.00 * len(months_remaining), 2),
            "steady_state_copay": 35.00,
            "insulin_cap": True,
            "_terms": {"flat": 35.0},
        }

    # Medicare-negotiated drugs: the negotiated price replaces the plan's price; the plan's own tier
    # cost-sharing still applies (2026-10-06 - it used to be forced to 25% on every plan).
    mfp = (get_mfp(drug_name_base) or get_mfp(drug_name)) if use_mfp else None   # not for the generic
    if mfp is not None:
        unit_cost = mfp

    monthly_costs = []
    deductible_remaining = deductible

    for month_num in months_remaining:
        month_name = calendar_month(month_num)
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
    # No price anywhere for this drug on this plan: when the client's cost depends on the price
    # (deductible or coinsurance), the cost is UNKNOWN - never shown as $0 without saying so.
    price_unknown = not unit_cost and (cost_type == 2 or ded_applies != "N")
    return {
        "tier": tier, "covered": True, "ndc": ndc, "price_unknown": price_unknown,
        "monthly_costs": monthly_costs, "annual_total": annual_total,
        "steady_state_copay": float(cost_amt) if cost_type == 1 else None,
        "_terms": {"ded_applies": ded_applies != "N", "price": unit_cost, "fee": 0.0,
                   "cost_type": cost_type, "cost_amt": cost_amt},
    }


def compute_drug_costs(drugs, zip_code, soa_date, client_address=None, client_city=None, client_state=None, custom_plans_str=None, plans_override=None):
    """
    drugs: list of {"name": str, "dosage": str}
    Normalizes drug names via Claude first, then looks up costs.
    Uses real client address for pharmacy distance calculations when available.
    custom_plans_str: optional comma-separated agent-requested plan names
    """
    months_remaining = get_remaining_months(soa_date)
    month_names = [calendar_month(m) for m in months_remaining]

    conn = get_db()

    # Agent-chosen plans (2026-10-02): render exactly these, in pick order. Otherwise pick
    # automatically for this zip code (the original behaviour).
    custom_warnings = []
    if plans_override:
        available_plans = list(plans_override)
        custom_plans_str = None          # margin-note requests don't apply when the agent picked
    else:
        available_plans = get_plans_for_zip(conn, zip_code, client_address, client_city, client_state)

    # Append any agent-requested custom plans (max 2, skip dupes)
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

    # Get nearby pharmacies per plan (keyed by carrier). The client is geocoded ONCE and ZIP
    # centroids are read once for all plans (2026-10-08: was once per plan - slow, and one live
    # address lookup per plan).
    pharmacy_map = {}  # carrier -> list of nearby pharmacies
    client_coords = get_client_coords(conn, zip_code, address=client_address, city=client_city,
                                      state=client_state) if plan_details else None
    zip_coords = load_zip_coords(conn) if plan_details else None
    for carrier, plan in plan_details.items():
        nearby = get_nearby_pharmacies(
            conn, plan["contract_id"], plan["plan_id"], zip_code,
            max_results=4, max_miles=30,
            client_address=client_address,
            client_city=client_city,
            client_state=client_state,
            client_coords=client_coords,
            zip_coords=zip_coords,
        )
        pharmacy_map[carrier] = nearby

    for item in normalized:
        drug_name = item.get("normalized", "").strip()
        original_name = item.get("original", "").strip()
        dosage = item.get("dosage", "").strip()
        flag = item.get("flag", "")
        norm_confidence = item.get("confidence", 1.0)
        is_injectable = item.get("is_injectable", False) or is_known_injectable(original_name, drug_name, dosage)

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

        # Local RxNorm resolver first (2026-10-07: app/drug_resolver.py, scored by tests/data/drug_cases*.psv);
        # RxNav on the internet only as a backup for names it can't read.
        resolved = drug_resolver.resolve(drug_name, dosage,
                                         alternates=[original_name, item.get("ingredient", ""), item.get("brand", "")])
        if resolved:
            rxcuis = resolved.rxcuis
            generic_rx = resolved.generic        # a brand's generic twins ([] for a generic name)
            drug_result["read_as"] = resolved.matched
            if resolved.flag:
                drug_result["resolver_flag"] = resolved.flag
                warnings.append({"drug": original_name or drug_name, "normalized_to": resolved.matched,
                                 "flag": resolved.flag})
        else:
            rxcuis = lookup_rxcuis(drug_name, dosage)
            generic_rx = None      # generic equivalents, looked up (RxNav) only if a plan lacks the brand

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
            if not plan.get("formulary_id"):
                # Carrier hasn't published this year's drug list (2026-10-06): say so, never "not covered".
                drug_result["plans"][carrier] = {"tier": None, "covered": None, "drug_list_missing": True,
                                                 "monthly_costs": [], "annual_total": None}
                continue
            plan_cost = get_drug_cost_for_plan(
                conn, plan["formulary_id"], plan["contract_id"],
                plan["plan_id"], rxcuis, plan["deductible"], months_remaining,
                drug_name=drug_name)
            if not plan_cost.get("covered"):
                # Brand not on the list: is its generic? (Januvia -> sitagliptin.) Priced as the
                # generic - the brand's Medicare-negotiated price does not apply to it.
                if generic_rx is None:
                    generic_rx = generic_equivalents(rxcuis)
                if generic_rx:
                    alt = get_drug_cost_for_plan(
                        conn, plan["formulary_id"], plan["contract_id"], plan["plan_id"], generic_rx,
                        plan["deductible"], months_remaining, drug_name=drug_name, use_mfp=False)
                    if alt.get("covered"):
                        alt["as_generic"] = True
                        plan_cost = alt
            if plan_cost.get("price_unknown"):
                warnings.append({"drug": original_name or drug_name, "normalized_to": "",
                                 "flag": f"no price on file for {carrier} - its cost is left out, verify"})
            
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
                    ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1
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
                    # Only the price TERMS are recorded here. The monthly costs are worked out
                    # for all drugs together (one shared deductible + the yearly out-of-pocket
                    # cap) by drug_year.apply_shared_year() at the end of this function.
                    pharm_monthly = []
                    pharm_terms = None
                    if drug_is_insulin:
                        pharm_terms = {"flat": 35.0}
                    elif cost_row:
                        pharm_terms = pharmacy_fill_terms(
                            conn, plan["contract_id"], plan["plan_id"], tier, unit_cost,
                            pharmacy, drug_name=drug_name, use_mfp=not plan_cost.get("as_generic"))
                    if pharm_terms is not None:
                        pharm_monthly = [{"month": mn, "cost": 0.0} for mn in month_names]

                    if pharm_monthly:
                        pharmacy_costs.append({
                            "name": pharmacy["name"],
                            "address": pharmacy["address"],
                            "city": pharmacy["city"],
                            "distance_miles": pharmacy["distance_miles"],
                            "dist_approximate": pharmacy.get("dist_approximate", False),
                            "preferred": pharmacy["preferred"],
                            "monthly_costs": pharm_monthly,
                            "annual_total": round(sum(m["cost"] for m in pharm_monthly), 2),
                            "_terms": pharm_terms,
                        })
                
                plan_cost["pharmacy_costs"] = pharmacy_costs

                # Calculate mail order costs
                mail_cost_row = conn.execute("""
                    SELECT cost_type_mail_pref, cost_amt_mail_pref, ded_applies
                    FROM beneficiary_cost
                    WHERE contract_id = ? AND plan_id = ? AND tier = ? AND days_supply = 2
                    ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1
                """, (plan["contract_id"], plan["plan_id"].zfill(3), plan_cost.get("tier", 0))).fetchone()

                # CMS days_supply codes: 1 = one month, 2 = three months (mail order is a 90-day fill).
                # cost_type 0 / no row = this tier can't be filled by mail at this plan.
                mail_offered = bool(mail_cost_row) and mail_cost_row[0] in (1, 2)
                if not (mail_offered or drug_is_insulin):
                    plan_cost["mail_order_costs"] = {"available": False, "monthly_costs": [],
                                                     "annual_total": None}
                if mail_offered or drug_is_insulin:
                    # Mail order goes through the same shared deductible + yearly cap
                    # (it used to skip the deductible, which made fake "saves $X/yr" lines).
                    # Costs are per month; a 90-day copay is divided by 3.
                    mail_ded = (mail_cost_row[2] != "N") if mail_cost_row else True
                    if drug_is_insulin:
                        mail_terms = {"flat": 35.0}
                    elif mail_offered:
                        mt, ma = mail_cost_row[0], float(mail_cost_row[1] or 0)
                        mail_price = mfp_value if (is_mfp_drug_flag and not plan_cost.get("as_generic")) else unit_cost
                        if mt == 2:
                            mail_terms = {"ded_applies": mail_ded, "price": mail_price, "fee": 0.0,
                                          "cost_type": 2, "cost_amt": ma}
                        else:
                            mail_terms = {"ded_applies": mail_ded, "price": mail_price, "fee": 0.0,
                                          "cost_type": 1, "cost_amt": ma / 3}
                    else:
                        mail_terms = {"flat": 0.0}
                    plan_cost["mail_order_costs"] = {
                        "monthly_costs": [{"month": mn, "cost": 0.0} for mn in month_names],
                        "annual_total": 0.0,
                        "_terms": mail_terms,
                    }
            
            drug_result["plans"][carrier] = plan_cost
        results.append(drug_result)

    from app.drug_year import apply_shared_year
    apply_shared_year(results, plan_details, month_names)

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
        missing_list = not plan.get("formulary_id")
        plan_summaries[carrier] = {
            "contract_id": plan["contract_id"], "plan_id": plan["plan_id"],
            "plan_name": plan["plan_name"], "plan_type": plan["plan_type"],
            "premium_monthly": plan["premium_monthly"], "premium_remaining_year": premium_annual,
            "deductible": plan["deductible"],
            "total_drug_cost": None if missing_list else round(total_drug_cost, 2),
            "total_drug_plus_premium": None if missing_list else round(total_drug_cost + premium_annual, 2),
            "all_drugs_covered": all_covered,
            "drug_list_missing": missing_list,
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


def build_pdf(*args, plan_year=None, county=None, county_note=None, **kwargs):
    """Internal agent report. Since 2026-10-02 the one-table layout (app/internal_pdf.py).
    Set the environment variable INTERNAL_REPORT_V1=1 to fall back to the previous layout
    (build_pdf_v1) without a code change."""
    if os.environ.get("INTERNAL_REPORT_V1") == "1":
        kwargs["provider_results"] = None          # v1 can't read the 2027 provider format
        return build_pdf_v1(*args, **kwargs)
    from app.internal_pdf import render
    return render(*args, plan_year=plan_year, county=county, county_note=county_note, **kwargs)


def build_pdf_v1(client_name, dob, zip_code, soa_date, plan_summaries, drug_detail, months_remaining, confidence=None, warnings=None, drug_detail_full=None, client_address=None, client_city=None, provider_results=None):
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io

    CHARCOAL   = colors.HexColor("#1e293b")
    TEAL       = colors.HexColor("#0d9488")
    TEAL_LIGHT = colors.HexColor("#ccfbf1")
    LIGHT_GRAY = colors.HexColor("#f8fafc")
    MID_GRAY   = colors.HexColor("#e2e8f0")
    DARK_GRAY  = colors.HexColor("#334155")
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

    def tier_badge(tier, copay=None):
        # If copay is $0 regardless of tier number, treat it as preferred (green)
        is_free = copay is not None and copay == 0.0
        configs = {
            1: ("#166534", "#dcfce7", "Tier 1"),   # green — preferred generic
            2: ("#1e40af", "#dbeafe", "Tier 2"),   # blue — generic
            3: ("#854d0e", "#fef9c3", "Tier 3"),   # amber — preferred brand
            4: ("#991b1b", "#fee2e2", "Tier 4"),   # red — non-preferred brand
            5: ("#6b21a8", "#f3e8ff", "Tier 5"),   # purple — specialty
            6: ("#166534", "#dcfce7", "Tier 6"),   # green by default (usually $0 preferred)
        }
        if is_free and tier not in (1, 6):
            # Override to green if $0 copay on any tier
            text_color, bg_color, label = "#166534", "#dcfce7", f"Tier {tier}"
        else:
            text_color, bg_color, label = configs.get(tier, ("#334155", "#f1f5f9", f"Tier {tier}"))
        return Paragraph(f'<font color="{text_color}">{label}</font>',
                         S(f"t{tier}", fontSize=6, fontName="Helvetica-Bold",
                           alignment=TA_CENTER, textColor=colors.HexColor(text_color), leading=8))

    def tier_bg(tier, copay=None):
        is_free = copay is not None and copay == 0.0
        if is_free:
            return GREEN_BG
        return {1: GREEN_BG, 2: BLUE_BG, 3: AMBER_BG, 4: RED_BG,
                5: colors.HexColor("#f3e8ff"), 6: GREEN_BG}.get(tier, WHITE)

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
                   [Paragraph("DOB: " + (str(dob) if dob else "—") + "  ·  Zip: " + (str(zip_code) if zip_code else "—"), h2)]]
    header_right = [[Paragraph("INTERNAL USE ONLY", badge_txt)],
                    [Paragraph(f"Generated: {datetime.today().strftime('%m/%d/%Y')}", gen_txt)],
                    [Paragraph("Data: " + data_vintage(), gen_txt)],
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
    elements.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=0.3*mm))

    # Warnings banner - compact + grouped (Sept 2026). One row per GROUP, items joined
    # with " · ", instead of one full-width row per warning (18 rows -> ~4 lines).
    #   Medications to verify: engine drug-lookup warnings + extraction flags about drugs
    #   Form notes:            other extraction flags (missing DOB, unclear ZIP, ...)
    #   Plan requests:         agent-requested plans that couldn't be added
    # Extraction flags (folded in by process_soa) arrive with EMPTY normalized_to AND flag;
    # engine warnings always carry a flag - that is how the two are told apart.
    # Flags about SOA-era fields that don't exist on the Client Information Sheet are
    # dropped here (the source fix is the extraction prompt - roadmap #9).
    # Plain words only: the built-in font has no warning-sign or arrow glyphs.
    if warnings:
        import re as _re
        from xml.sax.saxutils import escape as _esc
        _SOA_ERA = ("soa date", "appointment date", "signature", "coverage type")
        _MED_WORDS = ("medication", "medications", "medicine", "drug", "drugs", "dose",
                      "dosage", "pill", "prescription", "tablet", "capsule", "insulin",
                      "inhaler", "injection", "mg", "mcg", "rx")
        _med_names = set()
        for _d in (drug_detail or []):
            for _n in (_d.get("original_name"), _d.get("drug_name")):
                _w = (_n or "").strip().lower().split(" ")[0].strip(".,")
                if len(_w) >= 4:
                    _med_names.add(_w)

        def _is_med(text):
            low = text.lower()
            if any(_re.search(r"\b" + _re.escape(k) + r"\b", low) for k in _MED_WORDS):
                return True
            return any(n in low for n in _med_names)

        med_items, form_items, plan_items = [], [], []
        for w in warnings:
            drug = (w.get("drug") or "").strip()
            normalized = (w.get("normalized_to") or "").strip()
            flag = (w.get("flag") or "").strip()
            if drug == "Plan Request":
                if flag:
                    plan_items.append(flag)
                continue
            if not normalized and not flag:            # extraction flag (free text)
                if not drug or any(k in drug.lower() for k in _SOA_ERA):
                    continue
                (med_items if _is_med(drug) else form_items).append(drug)
                continue
            if "not found in rxnav" in flag.lower():   # engine: drug not recognized
                note = f"{drug}: couldn't identify \u2014 verify name"
            else:
                note = drug
                if normalized and normalized.lower() != drug.lower():
                    note += f" (read as {normalized})"
                if flag:
                    note += f" \u2014 {flag}"
            med_items.append(note)

        def _dedupe(items):
            seen, out = set(), []
            for it in items:
                if it.lower() not in seen:
                    seen.add(it.lower())
                    out.append(it)
            return out

        warn_lbl = S("wl", fontSize=6, fontName="Helvetica-Bold", textColor=WARN_TEXT, leading=8)
        warn_rows = []
        for label, items in (("Medications to verify", _dedupe(med_items)),
                             ("Form notes", _dedupe(form_items)),
                             ("Plan requests", _dedupe(plan_items))):
            if items:
                warn_rows.append([Paragraph(label, warn_lbl),
                                  Paragraph("  \u00b7  ".join(_esc(i) for i in items), warn_s)])
        if warn_rows:
            wt = Table(warn_rows, colWidths=[38*mm, 242*mm])
            wt.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), WARN_BG),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#fed7aa")),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("TOPPADDING", (0,0), (-1,-1), 1.5),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1.5),
                ("LEFTPADDING", (0,0), (-1,-1), 4),
            ]))
            elements.append(wt)
            elements.append(Spacer(1, 0.15*mm))

    # Cost plans (e.g. Medica Prime Solution) are Medicare Advantage-style plans: show them in the
    # MA sections. (Before 2026-10-02 a selected Cost plan was silently left out of every section.)
    ma_plans = {k: v for k, v in plan_summaries.items() if v.get("plan_type") in ("MA", "Cost")}
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
            star = ""
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
                    row.append(Paragraph(val, bold_cell))
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
        if False:  # best-plan column highlight removed
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
        elements.append(Spacer(1, 0.1*mm))
        t, ma_best = make_plan_table(ma_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.15*mm))

    if ma_plans and drug_detail:
        elements.append(Spacer(1, 2.0*mm))
        elements.append(Paragraph("SECTION 2 — DRUG FORMULARY TIERS", sec_title))
        elements.append(Spacer(1, 0.15*mm))
        carriers = list(ma_plans.keys())
        label_w = 50*mm
        col_w = (274*mm - label_w) / len(carriers)
        rows = [[Paragraph("Medication", col_hdr)] +
                [Paragraph(c, col_hdr) for c in carriers]]
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
                if drug.get("error"):
                    # The engine couldn't identify this drug, so coverage was never checked -
                    # don't claim "Not Covered" (matches the banner + client sheet, 2026-10-02).
                    row.append(Paragraph("Not identified", S("nid", fontSize=6, textColor=AMBER_TEXT, alignment=TA_CENTER, leading=8)))
                elif pd.get("injectable"):
                    row.append(Paragraph("Verify coverage", S("inj", fontSize=6, textColor=AMBER_TEXT, alignment=TA_CENTER, leading=8)))
                elif not pd.get("covered", False):
                    row.append(Paragraph("Not Covered", nc_style))
                else:
                    tier = pd.get("tier")
                    copay = pd.get("steady_state_copay")
                    row.append(tier_badge(tier, copay) if tier else Paragraph("—", cell))
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
        if False:  # best-plan column highlight removed (Section 2)
            ci = carriers.index(ma_best) + 1
            ts += [
                ("BACKGROUND", (ci,0), (ci,0), TEAL),
                ("LINEAFTER",  (ci,0), (ci,-1), 1.5, TEAL),
                ("LINEBEFORE", (ci,0), (ci,-1), 1.5, TEAL),
            ]
        t.setStyle(TableStyle(ts))
        elements.append(t)

        # Tier legend
        legend_s = S("leg", fontSize=4.5, textColor=colors.HexColor("#64748b"), leading=6)
        legend_items = [
            ('<font color="#166534">■</font> Tier 1 Preferred Generic ($0–low)',
             '<font color="#1e40af">■</font> Tier 2 Generic',
             '<font color="#854d0e">■</font> Tier 3 Preferred Brand',
             '<font color="#991b1b">■</font> Tier 4 Non-Preferred Brand',
             '<font color="#6b21a8">■</font> Tier 5 Specialty',
             '<font color="#166534">■</font> Tier 6 $0 Preferred'),
        ]
        legend_row = Table([[Paragraph(item, legend_s) for item in legend_items[0]]],
                           colWidths=[47*mm]*6)
        legend_row.setStyle(TableStyle([
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 2),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        elements.append(legend_row)
        elements.append(Spacer(1, 0.15*mm))

    if ma_plans and drug_detail:
        elements.append(Spacer(1, 2.0*mm))
        elements.append(Paragraph("SECTION 3 — PHARMACY COST COMPARISON BY PLAN", sec_title))
        elements.append(Spacer(1, 0.15*mm))

        if client_address and client_city:
            location_label = client_address + ", " + client_city
        else:
            location_label = "ZIP " + zip_code
        elements.append(Paragraph(
            "Nearest in-network pharmacies to " + location_label + "  ·  Costs include deductible phase where applicable",
            S("note", fontSize=5, textColor=colors.HexColor("#64748b"), leading=7)))
        elements.append(Paragraph(
            "Costs shown reflect CMS negotiated rates including pharmacy dispensing fees. Prices may vary by pharmacy.",
            S("disc", fontSize=5, textColor=colors.HexColor("#94a3b8"), leading=7)))
        elements.append(Spacer(1, 0.1*mm))

        def get_plan_pharmacy_summary(carrier):
            pharm_totals = {}
            for drug in drug_detail:
                pc_list = drug.get("plans", {}).get(carrier, {}).get("pharmacy_costs", [])
                for pc in pc_list:
                    name = pc["name"]
                    dist = pc.get("distance_miles", 99)
                    dist_approx = pc.get("dist_approximate", False)
                    preferred = pc.get("preferred", True)
                    annual = pc.get("annual_total", 0) or 0
                    if name not in pharm_totals:
                        pharm_totals[name] = {"annual": 0, "distance": dist, "dist_approximate": dist_approx, "preferred": preferred, "monthly": {}}
                    pharm_totals[name]["annual"] += annual
                    for m in pc.get("monthly_costs", []):
                        mn = m["month"]
                        pharm_totals[name]["monthly"][mn] = pharm_totals[name]["monthly"].get(mn, 0) + (m["cost"] or 0)
            if not pharm_totals:
                return None

            # All pharmacies sorted by distance, each with their own price
            all_pharmacies = sorted(
                [{"name": k, "distance": v["distance"], "dist_approximate": v.get("dist_approximate", False),
                  "preferred": v.get("preferred", True), "annual": v["annual"], "monthly": v["monthly"]}
                 for k, v in pharm_totals.items()],
                key=lambda x: x["distance"]
            )

            min_cost = min(v["annual"] for v in pharm_totals.values())
            cheapest_name = min(pharm_totals, key=lambda k: pharm_totals[k]["annual"])

            mail_annual = sum(
                (drug.get("plans", {}).get(carrier, {}).get("mail_order_costs", {}).get("annual_total", 0) or 0)
                for drug in drug_detail
            )

            # Use cheapest pharmacy for transition display
            cheapest_monthly = pharm_totals[cheapest_name]["monthly"]
            from datetime import datetime as _dt2
            costs_by_month = [(mn, cheapest_monthly.get(mn, 0)) for mn in months_remaining] if months_remaining else []
            transitions = []
            prev_cost = None
            for mn_name, cost in costs_by_month:
                if cost != prev_cost:
                    transitions.append((mn_name[:3], cost))
                    prev_cost = cost

            prices_differ = len(set(round(v["annual"]) for v in pharm_totals.values())) > 1

            return {
                "min_annual": min_cost,
                "cheapest": all_pharmacies,
                "cheapest_name": cheapest_name,
                "mail_annual": mail_annual,
                "transitions": transitions,
                "all_same": not prices_differ
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
        pharm_col_w= 80*mm
        cost_col_w = 70*mm
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
            star = ""

            plan_cell = Table([
                [Paragraph(carrier + star, plan_s)],
                [Paragraph("$" + "{:.2f}".format(plan_data["premium_monthly"]) + " prem · $" + "{:.0f}".format(plan_data["deductible"]) + " ded", sub_s2)]
            ], colWidths=[plan_col_w - 3*mm], style=inner_ts)

            if not summary:
                rows.append([plan_cell, Paragraph("No data", sub_s2), Paragraph("—", cell), Paragraph("—", cell)])
                continue

            pharm_lines = []
            prices_differ = not summary["all_same"]
            cheapest_name = summary.get("cheapest_name", "")
            for p in summary["cheapest"][:4]:
                name = p["name"].split("#")[0].replace(" Pharmacy", "").replace(" Pharm", "").strip()[:18]
                dist_prefix = "~" if p.get("dist_approximate") else ""
                dist = dist_prefix + str(p["distance"]) + " mi"
                pref_label = "" if p.get("preferred", True) else " (non-pref)"
                # Show steady-state monthly cost (last month = post-deductible) per pharmacy
                if prices_differ:
                    pharm_monthly_dict = p.get("monthly", {})
                    # Steady state = last month in the period (post-deductible)
                    if pharm_monthly_dict and months_remaining:
                        from datetime import datetime as _dt3
                        last_month = months_remaining[-1]
                        steady = pharm_monthly_dict.get(last_month, 0)
                    else:
                        steady = p.get("annual", 0) / len(months_remaining) if months_remaining else 0
                    price_str = " $" + "{:.0f}".format(steady)
                    is_cheapest = p["name"] == cheapest_name
                    style = pharm_s if is_cheapest else runner_s
                    pharm_lines.append(Paragraph(name + pref_label + "  (" + dist + ")" + price_str, style))
                else:
                    pharm_lines.append(Paragraph(name + pref_label + "  (" + dist + ")", pharm_s))
            # pair pharmacies two-across to compress Section 3 vertically
            _pl = pharm_lines
            _blank = Paragraph("", pharm_s)
            _prows = [[_pl[i] if i < len(_pl) else _blank, _pl[i+1] if i+1 < len(_pl) else _blank] for i in range(0, max(len(_pl), 1), 2)]
            _pair_ts = TableStyle([("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),1),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(0,-1),6),("RIGHTPADDING",(1,0),(1,-1),0)])
            pharm_cell = Table(_prows, colWidths=[(pharm_col_w - 3*mm)/2.0]*2, style=_pair_ts)

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
                    Paragraph("$" + "{:.2f}".format(steady) + " / mo (cheapest)", cost_s),
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
        if False:  # best-plan row highlight removed (Section 3)
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
        elements.append(Spacer(1, 2.0*mm))
        elements.append(Paragraph("SECTION 4 — PART D STANDALONE PLANS", sec_title))
        elements.append(Spacer(1, 0.1*mm))
        t, _ = make_plan_table(pd_plans, "Plan Feature")
        elements.append(t)
        elements.append(Spacer(1, 0.1*mm))

    elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY, spaceBefore=0.1*mm, spaceAfter=0.1*mm))
    elements.append(Paragraph(
        "Internal use only · Agent reference · " + data_vintage() + " · Verify before presenting", footer))

    # ── Page 2: Provider Network Directory ───────────────────────────────
    if provider_results:
        from reportlab.platypus import PageBreak
        elements.append(PageBreak())

        p2_title = S("p2t", fontSize=9, textColor=CHARCOAL, fontName="Helvetica-Bold", leading=12)
        p2_sub   = S("p2s", fontSize=6, textColor=colors.HexColor("#64748b"), leading=8)

        # Build dynamic title based on which carriers are present
        active_carriers = []
        has_medica_col = any("medica_status" in r for r in provider_results)
        has_bcbs_col   = any("bcbs_status"   in r for r in provider_results)
        has_hp_col     = any("hp_status"     in r for r in provider_results)
        has_humana_col = any("humana_status" in r for r in provider_results)
        has_uhc_col    = any("uhc_status"    in r for r in provider_results)
        has_aetna_col  = any("aetna_status"  in r for r in provider_results)
        if has_medica_col: active_carriers.append("Medica")
        if has_bcbs_col:   active_carriers.append("Blue Cross")
        if has_hp_col:     active_carriers.append("HealthPartners")
        if has_humana_col: active_carriers.append("Humana")
        if has_uhc_col:    active_carriers.append("UHC")
        if has_aetna_col:  active_carriers.append("Aetna")
        carrier_label = " & ".join(active_carriers) if active_carriers else "Medicare Advantage"

        elements.append(Paragraph(
            f"PROVIDER NETWORK DIRECTORY — {carrier_label.upper()} MEDICARE ADVANTAGE",
            p2_title))
        elements.append(Paragraph(
            "Verified against 2026 provider directories  ·  "
            "Confirm network status with carrier before enrollment",
            p2_sub))
        elements.append(HRFlowable(width="100%", thickness=2, color=TEAL,
                                   spaceAfter=1*mm, spaceBefore=0.5*mm))

        # ── Column widths — scale based on number of carrier columns ──────
        # Fixed cols: Provider name + Specialty. Dynamic: one col per carrier.
        num_carrier_cols = (1 if has_medica_col else 0) + (1 if has_bcbs_col else 0) + (1 if has_hp_col else 0) + (1 if has_humana_col else 0) + (1 if has_uhc_col else 0) + (1 if has_aetna_col else 0)
        total_w     = 239*mm   # fits landscape with margins
        name_w      = 58*mm
        spec_w      = 38*mm
        carrier_w   = (total_w - name_w - spec_w) / max(num_carrier_cols, 1)

        hdr_left_s = S("phl", fontSize=6, textColor=WHITE, fontName="Helvetica-Bold",
                        alignment=TA_LEFT, leading=8)
        hdr_ctr_s  = S("phc", fontSize=6, textColor=WHITE, fontName="Helvetica-Bold",
                        alignment=TA_CENTER, leading=8)

        # Build header row
        hdr_row = [
            Paragraph("Provider",  hdr_left_s),
            Paragraph("Specialty", hdr_left_s),
        ]
        col_widths = [name_w, spec_w]
        if has_medica_col:
            hdr_row.append(Paragraph("Medica", hdr_ctr_s))
            col_widths.append(carrier_w)
        if has_bcbs_col:
            hdr_row.append(Paragraph("Blue Cross", hdr_ctr_s))
            col_widths.append(carrier_w)
        if has_hp_col:
            hdr_row.append(Paragraph("HealthPartners", hdr_ctr_s))
            col_widths.append(carrier_w)
        if has_humana_col:
            hdr_row.append(Paragraph("Humana", hdr_ctr_s))
            col_widths.append(carrier_w)
        if has_uhc_col:
            hdr_row.append(Paragraph("UHC", hdr_ctr_s))
            col_widths.append(carrier_w)
        if has_aetna_col:
            hdr_row.append(Paragraph("Aetna", hdr_ctr_s))
            col_widths.append(carrier_w)

        prov_rows = [hdr_row]

        # ── Styles ────────────────────────────────────────────────────────
        name_s   = S("pn2", fontSize=7, textColor=colors.black, fontName="Helvetica-Bold", leading=9)
        raw_s    = S("pr2", fontSize=6, textColor=colors.HexColor("#64748b"), leading=8)
        sub_s    = S("ps2", fontSize=6.5, textColor=CHARCOAL, leading=8)
        spec_s2  = S("ps3", fontSize=6, textColor=DARK_GRAY, leading=8)
        detail_s = S("pd2", fontSize=6, textColor=DARK_GRAY, leading=8, alignment=TA_CENTER)
        in_net_s = S("in2", fontSize=6, textColor=GREEN_TEXT, fontName="Helvetica-Bold",
                     alignment=TA_CENTER, leading=8)
        not_fnd_s= S("nf2", fontSize=6, textColor=colors.HexColor("#dc2626"),
                     fontName="Helvetica-Bold", alignment=TA_CENTER, leading=8)
        na_s     = S("na2", fontSize=6, textColor=colors.HexColor("#94a3b8"),
                     alignment=TA_CENTER, leading=8)

        inner_zero = TableStyle([
            ("TOPPADDING",    (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 0),
            ("LEFTPADDING",   (0,0), (-1,-1), 0), ("RIGHTPADDING",  (0,0), (-1,-1), 0),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ])

        prov_ts = [
            ("BACKGROUND",    (0,0), (-1,0),  CHARCOAL),
            ("GRID",          (0,0), (-1,-1), 0.4, MID_GRAY),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING",   (0,0), (-1,-1), 4),
            ("RIGHTPADDING",  (0,0), (-1,-1), 4),
            ("ALIGN",         (2,0), (-1,-1), "CENTER"),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [WHITE, LIGHT_GRAY]),
        ]

        def make_status_cell(status, detail, accepting):
            """Build a status cell with detail line and accepting note if not accepting."""
            if status == "In Network":
                acc_note = ""
                if accepting == "N":
                    acc_note = " · Not Accepting"
                return Paragraph(f"✓ In Network{acc_note}", in_net_s), "IN"
            elif status == "Not Found":
                return Paragraph("✗ Not Found", not_fnd_s), "NF"
            else:
                return Paragraph("—  N/A", na_s), "NA"

        # Track carrier column indices for background coloring
        medica_col_idx = 2 if has_medica_col else None
        bcbs_col_idx   = (3 if has_medica_col else 2) if has_bcbs_col else None
        hp_col_idx     = (2 + (1 if has_medica_col else 0) + (1 if has_bcbs_col else 0)) if has_hp_col else None
        humana_col_idx = (2 + (1 if has_medica_col else 0) + (1 if has_bcbs_col else 0) + (1 if has_hp_col else 0)) if has_humana_col else None
        uhc_col_idx    = (2 + (1 if has_medica_col else 0) + (1 if has_bcbs_col else 0) + (1 if has_hp_col else 0) + (1 if has_humana_col else 0)) if has_uhc_col else None
        aetna_col_idx  = (2 + (1 if has_medica_col else 0) + (1 if has_bcbs_col else 0) + (1 if has_hp_col else 0) + (1 if has_humana_col else 0) + (1 if has_uhc_col else 0)) if has_aetna_col else None

        for i, r in enumerate(provider_results, start=1):
            last      = r.get("last_name", "")
            first     = r.get("first_name", "")
            creds     = r.get("credentials", "")
            raw       = r.get("raw_text", "")
            spec      = r.get("specialty", "") or ""

            # Build display name — prefer DB-matched name, then clinic from detail, then raw_text
            matched_last  = (r.get("matched_last")  or last  or "").strip()
            matched_first = (r.get("matched_first") or first or "").strip()
            matched_creds = (r.get("credentials")   or creds or "").strip()

            if matched_last or matched_first:
                # Individual provider name
                full_name = (matched_first + " " + matched_last).strip()
                if matched_creds:
                    full_name += f", {matched_creds}"
            else:
                # Clinic lookup — extract clinic name from the detail string
                # Detail format: "Clinic Name · City"
                bcbs_detail   = r.get("bcbs_detail",  "")
                medica_detail = r.get("medica_detail", "")
                hp_detail     = r.get("hp_detail",     "")
                detail_str    = (bcbs_detail if r.get("bcbs_status") == "In Network"
                                 else hp_detail if r.get("hp_status") == "In Network"
                                 else medica_detail)
                if " · " in detail_str:
                    full_name = detail_str.split(" · ")[0].strip()[:45]
                else:
                    full_name = detail_str[:45] or raw[:45]

            # Extract city from whichever carrier found the provider
            bcbs_city   = r.get("bcbs_detail",  "").split(" · ")[1] if r.get("bcbs_status")   == "In Network" and " · " in r.get("bcbs_detail",  "") else ""
            medica_city = r.get("medica_detail", "").split(" · ")[1] if r.get("medica_status") == "In Network" and " · " in r.get("medica_detail", "") else ""
            hp_city     = r.get("hp_detail",     "").split(" · ")[1] if r.get("hp_status")     == "In Network" and " · " in r.get("hp_detail",     "") else ""
            humana_city = r.get("humana_detail", "").split(" · ")[1] if r.get("humana_status") == "In Network" and " · " in r.get("humana_detail", "") else ""
            display_city = (bcbs_city or hp_city or humana_city or medica_city or r.get("city", "") or "").strip()

            name_cell = Table([
                [Paragraph(full_name[:45],       name_s)],
                [Paragraph(display_city[:35],    sub_s)],
            ], colWidths=[name_w - 4*mm], style=inner_zero)

            row_cells = [
                name_cell,
                Paragraph(spec[:35] if spec else "—", spec_s2),
            ]

            if has_medica_col:
                m_status   = r.get("medica_status", "")
                m_detail   = r.get("medica_detail", "")
                m_acc      = r.get("medica_accepting", "")
                m_cell, m_type = make_status_cell(m_status, m_detail, m_acc)
                detail_text = m_detail[:65] if m_detail and m_type == "IN" else ""
                combined = Table([[m_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if m_type == "IN":
                    prov_ts.append(("BACKGROUND", (medica_col_idx, i),
                                    (medica_col_idx, i), GREEN_BG))
                elif m_type == "NF":
                    prov_ts.append(("BACKGROUND", (medica_col_idx, i),
                                    (medica_col_idx, i), RED_BG))

            if has_bcbs_col:
                b_status   = r.get("bcbs_status", "")
                b_detail   = r.get("bcbs_detail", "")
                b_acc      = r.get("bcbs_accepting", "")
                b_cell, b_type = make_status_cell(b_status, b_detail, b_acc)
                detail_text = b_detail[:65] if b_detail and b_type == "IN" else ""
                combined = Table([[b_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if b_type == "IN":
                    prov_ts.append(("BACKGROUND", (bcbs_col_idx, i),
                                    (bcbs_col_idx, i), GREEN_BG))
                elif b_type == "NF":
                    prov_ts.append(("BACKGROUND", (bcbs_col_idx, i),
                                    (bcbs_col_idx, i), RED_BG))

            if has_hp_col:
                h_status   = r.get("hp_status", "")
                h_detail   = r.get("hp_detail", "")
                h_acc      = r.get("hp_accepting", "")
                h_cell, h_type = make_status_cell(h_status, h_detail, h_acc)
                detail_text = h_detail[:65] if h_detail and h_type == "IN" else ""
                combined = Table([[h_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if h_type == "IN":
                    prov_ts.append(("BACKGROUND", (hp_col_idx, i),
                                    (hp_col_idx, i), GREEN_BG))
                elif h_type == "NF":
                    prov_ts.append(("BACKGROUND", (hp_col_idx, i),
                                    (hp_col_idx, i), RED_BG))


            if has_humana_col:
                hu_status  = r.get("humana_status", "")
                hu_detail  = r.get("humana_detail", "")
                hu_acc     = r.get("humana_accepting", "")
                hu_cell, hu_type = make_status_cell(hu_status, hu_detail, hu_acc)
                detail_text = hu_detail[:65] if hu_detail and hu_type == "IN" else ""
                combined = Table([[hu_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if hu_type == "IN":
                    prov_ts.append(("BACKGROUND", (humana_col_idx, i),
                                    (humana_col_idx, i), GREEN_BG))
                elif hu_type == "NF":
                    prov_ts.append(("BACKGROUND", (humana_col_idx, i),
                                    (humana_col_idx, i), RED_BG))

            if has_uhc_col:
                uh_status  = r.get("uhc_status", "")
                uh_detail  = r.get("uhc_detail", "")
                uh_acc     = r.get("uhc_accepting", "")
                uh_cell, uh_type = make_status_cell(uh_status, uh_detail, uh_acc)
                detail_text = uh_detail[:65] if uh_detail and uh_type == "IN" else ""
                combined = Table([[uh_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if uh_type == "IN":
                    prov_ts.append(("BACKGROUND", (uhc_col_idx, i),
                                    (uhc_col_idx, i), GREEN_BG))
                elif uh_type == "NF":
                    prov_ts.append(("BACKGROUND", (uhc_col_idx, i),
                                    (uhc_col_idx, i), RED_BG))

            if has_aetna_col:
                ae_status  = r.get("aetna_status", "")
                ae_detail  = r.get("aetna_detail", "")
                ae_acc     = r.get("aetna_accepting", "")
                ae_cell, ae_type = make_status_cell(ae_status, ae_detail, ae_acc)
                detail_text = ae_detail[:65] if ae_detail and ae_type == "IN" else ""
                combined = Table([[ae_cell],[Paragraph(detail_text, detail_s)]], colWidths=[carrier_w - 8*mm], style=inner_zero, hAlign="CENTER")
                row_cells.append(combined)

                if ae_type == "IN":
                    prov_ts.append(("BACKGROUND", (aetna_col_idx, i),
                                    (aetna_col_idx, i), GREEN_BG))
                elif ae_type == "NF":
                    prov_ts.append(("BACKGROUND", (aetna_col_idx, i),
                                    (aetna_col_idx, i), RED_BG))

            prov_rows.append(row_cells)

        pt = Table(prov_rows, colWidths=col_widths)
        pt.setStyle(TableStyle(prov_ts))
        elements.append(pt)
        elements.append(Spacer(1, 1*mm))

        disc_s = S("d2", fontSize=5, textColor=colors.HexColor("#94a3b8"), leading=7)
        carrier_notes = []
        if has_medica_col:
            carrier_notes.append("Medica: 1-800-952-3455 or medica.com")
        if has_bcbs_col:
            carrier_notes.append("Blue Cross: 1-800-711-9865 or bluecrossmn.com")
        if has_humana_col:
            carrier_notes.append("Humana: 1-800-457-4708 or humana.com")
        if has_uhc_col:
            carrier_notes.append("UHC: 1-844-867-3487 or myAARPMedicare.com")
        if has_aetna_col:
            carrier_notes.append("Aetna: 1-800-307-4830 or AllinaHealthAetnaMedicare.com")
        elements.append(Paragraph(
            "⚠  Network status reflects 2026 provider directories. "
            "Networks change throughout the year — verify directly with carrier before enrollment.  "
            + "  ·  ".join(carrier_notes),
            disc_s))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY,
                                   spaceBefore=0.5*mm, spaceAfter=0.1*mm))
        elements.append(Paragraph(
            f"Internal use only · Agent reference · {carrier_label} 2026 Provider Directories",
            S("fp2", fontSize=6, textColor=colors.HexColor("#94a3b8"),
              alignment=TA_CENTER, leading=8)))

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
        "zip_55309_county": county_55309,
        # Which data is live (2026-10-07): check these after every upload/switch (README_REFRESH.md).
        "data_year": data_meta.data_year(DB_PATH),
        "data_vintage": data_meta.data_vintage(DB_PATH),
        "prices_estimated": data_meta.prices_estimated(DB_PATH),
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


def _parse_selected_plans(data):
    """The optional ordered "selected_plans" list from a request body (None when absent/empty)."""
    sel = data.get("selected_plans")
    if isinstance(sel, str):
        try:
            sel = json.loads(sel)
        except Exception:
            return "invalid"
    if sel in (None, [], ""):
        return None
    return sel


@app.route("/plans-for-zip", methods=["GET"])
def plans_for_zip_route():
    """Every plan a client in this ZIP could choose (agent plan picker, 2026-10-02).
    Same eligibility rule as the reports (eligible_plan_rows). Medicare Advantage first, then
    standalone Part D; each sorted by carrier, then plan name. Contains no client data."""
    from app.client_comparison import carrier_display, official_plan_name, display_plan_name

    zip_code = (request.args.get("zip") or "").strip()
    if len(zip_code) != 5 or not zip_code.isdigit():
        return jsonify({"error": "Provide a 5-digit ZIP, e.g. /plans-for-zip?zip=55441"}), 400
    conn = get_db()
    try:
        choice = county_choice(conn, zip_code, request.args.get("address"), request.args.get("city"),
                               request.args.get("state"))
        county = choice["county"]
        if not county:
            return jsonify({"error": f"No county found for ZIP {zip_code}"}), 404
        all_counties = [c["county"] for c in choice["counties"]] or [county]
        rows, where = {}, {}
        for cty in all_counties:
            ma_rows, pd_rows = eligible_plan_rows(conn, cty)
            for r, ptype in [(r, "MA") for r in ma_rows] + [(r, "PD") for r in pd_rows]:
                k = (r[0], str(r[1]).zfill(3))
                rows.setdefault(k, (r, ptype))
                where.setdefault(k, []).append(cty)

        def entry(r, ptype):
            cid, pid = r[0], str(r[1]).zfill(3)
            carrier = carrier_display(cid, r[3])
            official = (r[2] or official_plan_name(cid, pid) or "").strip()
            name = display_plan_name(official, f"{cid}-{pid}")
            premium, deductible = plan_premium_deductible(conn, cid, pid, r)
            cs = where[(cid, pid)]
            return {"contract_id": cid, "plan_id": pid, "plan_type": ptype,
                    "carrier": carrier, "plan_name": name,
                    "display_name": f"{carrier} \u2014 {name}",
                    # Agent-facing detail for the picker (2026-10-02). Same source as the reports.
                    "plan_number": f"{cid}-{pid}",
                    "official_name": official or name,
                    "premium_monthly": round(premium, 2),
                    "drug_deductible": round(deductible, 2),
                    # ZIPs that cross county lines (2026-10-06): where this plan is sold, and whether
                    # that includes the county we assumed for the client.
                    "counties": cs if len(all_counties) > 1 and ptype != "PD" else [],
                    "in_county": county in cs,
                    # Carrier hasn't published this year's drug list yet (2027 database)
                    "drug_list_loaded": bool(r[7]),
                    "drug_list_note": "" if r[7] else "Drug list not published yet - drug costs can't be shown"}

        def key(p):
            return (not p["in_county"], p["carrier"].lower(), p["plan_name"].lower())
        entries = [entry(r, t) for r, t in rows.values()]
        ma = sorted((e for e in entries if e["plan_type"] == "MA"), key=key)
        pd = sorted((e for e in entries if e["plan_type"] == "PD"), key=key)
    finally:
        conn.close()
    return jsonify({"zip_code": zip_code, "county": county, "county_exact": bool(choice["exact"]),
                    "county_method": choice["method"], "counties": choice["counties"],
                    "county_note": choice["note"],
                    "data_vintage": data_vintage(), "plans": ma + pd})


def _require_zip(data):
    """(zip, None) for a 5-digit ZIP (ZIP+4 accepted), else (None, a plain-English 400).
    Added 2026-10-03: the report endpoints used to fall back to 55441 silently when the ZIP was
    missing, which would price the wrong county. Pasted emails/screenshots make that likely."""
    import re as _re
    m = _re.fullmatch(r"(\d{5})(-\d{4})?", str(data.get("zip_code") or "").strip())
    if m:
        return m.group(1), None
    return None, (jsonify({"error": "A 5-digit ZIP code is required to look up plans and pharmacies."}), 400)


@app.route("/check-drugs", methods=["POST"])
def check_drugs_route():
    """Drug names as typed on the Details tab -> what to tell the agent (2026-10-07, Roundabout Phase 33).
    Body: {"drugs": [{"name": "Atorvastin", "dosage": "40 mg"}, ...]}  (at most 60)
    Reply: {"drugs": [{"name", "dosage", "status", "suggestion", "candidates", "read_as", "note"}, ...]}
      status: ok | spelling (suggestion = corrected name) | strength (note) | unknown (candidates) | empty
    Local RxNorm only - no AI call, no internet, nothing logged."""
    data = request.get_json(silent=True) or {}
    drugs = data.get("drugs")
    if not isinstance(drugs, list) or len(drugs) > 60:
        return jsonify({"error": 'Send {"drugs": [{"name": "...", "dosage": "..."}]} (at most 60)'}), 400
    out = []
    for d in drugs:
        d = d if isinstance(d, dict) else {}
        name, dosage = str(d.get("name") or "")[:200], str(d.get("dosage") or "")[:100]
        out.append({"name": name, "dosage": dosage, **drug_resolver.check(name, dosage)})
    return jsonify({"drugs": out})


@app.route("/process-soa", methods=["POST"])
def process_soa():
    """
    Accepts flat fields from Make. Normalizes drugs via Claude,
    looks up costs, returns PDF binary.
    """
    data = request.get_json(force=True, silent=True) or {}

    client_name = data.get("client_name", "Client")
    dob = data.get("dob", "")
    zip_code, zip_error = _require_zip(data)
    if zip_error:
        return zip_error
    soa_date = data.get("soa_date", datetime.today().strftime("%m/%d/%Y"))
    # Cost period (2026-10-02): for a FUTURE plan year (AEP - coverage starts Jan 1) price the
    # full plan year, exactly like /client-comparison, so both reports show the same yearly cost.
    # For the current year (mid-year enrollment) keep pricing from the SOA month to December.
    cost_start_date = soa_date
    if data.get("plan_year") not in (None, ""):
        try:
            _py = int(data.get("plan_year"))
            try:
                _soa_year = datetime.strptime(soa_date, "%m/%d/%Y").year
            except Exception:
                _soa_year = date.today().year
            if _py > _soa_year:
                cost_start_date = f"01/01/{_py}"
        except Exception:
            pass
    drug_names = data.get("drug_names", "")
    drug_dosages = data.get("drug_dosages", "")
    client_address = data.get("client_address", "")
    client_city = data.get("client_city", "")
    client_state = data.get("client_state", "MN")
    confidence = data.get("confidence")
    custom_plans_str = data.get("custom_plans", "")
    providers_raw = data.get("providers", [])
    if isinstance(providers_raw, str):
        try:
            providers_raw = json.loads(providers_raw)
        except Exception:
            providers_raw = []
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

    # Agent-chosen plans (2026-10-02): validate first so a bad pick is a clear 400, never a
    # silently different report.
    plans_override = None
    selected = _parse_selected_plans(data)
    if selected == "invalid":
        return jsonify({"error": "selected_plans is not valid JSON"}), 400
    if selected is not None:
        _conn = get_db()
        try:
            plans_override, _county, err = resolve_selected_plans(_conn, zip_code, selected, address=client_address,
                                                                  city=client_city, state=client_state)
        finally:
            _conn.close()
        if err:
            return jsonify({"error": err}), 400

    try:
        result = compute_drug_costs(drugs, zip_code, cost_start_date,
                                    client_address=client_address,
                                    client_city=client_city,
                                    client_state=client_state,
                                    custom_plans_str=custom_plans_str,
                                    plans_override=plans_override)
    except Exception as e:
        return jsonify({"error": f"Drug cost computation failed: {str(e)}"}), 500

    # Doctors / clinics: 2027 sources only - agency confirmations, 2027 carrier directories, health-system
    # level (app/providers_2027.py). Each plan column gets its own status.
    provider_results = []
    if providers_raw:
        from app import providers_2027
        plan_rows = [(label, s.get("contract_id"), str(s.get("plan_id") or "").zfill(3), s.get("plan_type"))
                     for label, s in result.get("plan_summaries", {}).items()]
        try:
            provider_results = providers_2027.check(providers_raw, plan_rows)
        except Exception as exc:
            print(f"provider check failed: {exc}")
            provider_results = [dict(p, plans={}) for p in providers_raw if isinstance(p, dict)]

    # -- Extraction flags -> report warnings ----------------------------
    # Fold Claude's extraction flags (sent from n8n) into the warnings the
    # report already renders, so they surface in the "Drug Verification
    # Required" banner. Tolerant of shape: flags may arrive as null, a JSON
    # string, a list of strings, or a list of dicts.
    flags_raw = data.get("flags", [])
    if isinstance(flags_raw, str):
        try:
            flags_raw = json.loads(flags_raw)
        except Exception:
            flags_raw = [flags_raw] if flags_raw.strip() else []
    if not isinstance(flags_raw, list):
        flags_raw = [flags_raw] if flags_raw else []
    extraction_warnings = []
    for f in flags_raw:
        if f is None:
            continue
        if isinstance(f, dict):
            drug_txt = (f.get("drug") or f.get("drug_name") or f.get("medication") or "").strip()
            msg_txt = (f.get("flag") or f.get("message") or f.get("issue")
                       or f.get("note") or f.get("text") or f.get("description")
                       or f.get("reason") or "").strip()
            text = (drug_txt + ": " + msg_txt) if (drug_txt and msg_txt) else (msg_txt or drug_txt)
        else:
            text = str(f).strip()
        if text:
            extraction_warnings.append({"drug": text, "normalized_to": "", "flag": ""})
    merged_warnings = list(result.get("warnings", [])) + extraction_warnings

    # Header facts for the one-table report (2026-10-02)
    report_plan_year = int(cost_start_date[-4:]) if cost_start_date != soa_date else None
    report_county, county_note = None, ""
    try:
        _c = get_db()
        try:
            _choice = county_choice(_c, zip_code, client_address, client_city, client_state)
            report_county, county_note = _choice["county"], _choice["note"]
            if plans_override is not None and _county and _county != report_county:
                # the agent picked plans sold only in another county of this ZIP
                county_note = f"ZIP {zip_code} crosses county lines; the plans picked are in {_county} County."
                report_county = _county
            if plans_override is not None and len(_choice["counties"]) > 1:
                outside = [p["carrier"] for p in plans_override
                           if p.get("type") != "PD" and report_county not in (p.get("counties") or [report_county])]
                if outside:
                    county_note = (f"ZIP {zip_code} crosses county lines. {', '.join(outside)} "
                                   f"{'is' if len(outside) == 1 else 'are'} not sold in {report_county} County "
                                   f"- check which county the client lives in.")
        finally:
            _c.close()
    except Exception:
        report_county, county_note = None, ""

    try:
        pdf_bytes_out = build_pdf(
            client_name, dob, zip_code, soa_date,
            result["plan_summaries"],
            result["drug_detail"],
            result["months_remaining"],
            confidence=confidence,
            warnings=merged_warnings,
            client_address=client_address,
            client_city=client_city,
            provider_results=provider_results,
            plan_year=report_plan_year,
            county=report_county,
            county_note=county_note,
        )
    except Exception as e:
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500

    filename = f"{client_name.replace(' ', '_')}_Drug_Comparison.pdf"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    if report_county:
        headers["X-County"] = report_county
    if county_note:
        headers["X-County-Note"] = county_note.encode("ascii", "replace").decode()
    return Response(pdf_bytes_out, mimetype="application/pdf", headers=headers)


@app.route("/client-comparison", methods=["POST"])
def client_comparison_route():
    """
    Client-facing plan-comparison PDF (Phase 3). Takes the same flat fields as
    /process-soa (already-extracted CIS data), runs a FULL plan-year (Jan-1) cost
    computation, picks plans deterministically, merges real CMS benefits, and
    returns the one-page client PDF. Isolated from /process-soa: separate selection
    and renderer, never handed provider names or drug tiers.
    NOTE: test data only until the engine moves to a BAA host.
    """
    from app.client_comparison import plan_summaries_to_candidates, assemble_renderer_payload
    from app.client_selection import select_plans, NeutralNetworkFit
    from app.client_pdf import render_client_comparison

    data = request.get_json(force=True, silent=True) or {}
    client_name = data.get("client_name", "Client")
    zip_code, zip_error = _require_zip(data)
    if zip_error:
        return zip_error
    drug_names = data.get("drug_names", "")
    drug_dosages = data.get("drug_dosages", "")
    client_address = data.get("client_address", "")
    client_city = data.get("client_city", "")
    client_state = data.get("client_state", "MN")
    county = data.get("county", "")
    try:
        plan_year = int(data.get("plan_year", data_meta.data_year(DB_PATH)))
    except Exception:
        plan_year = data_meta.data_year(DB_PATH)

    providers_raw = data.get("providers", [])
    if isinstance(providers_raw, str):
        try:
            providers_raw = json.loads(providers_raw)
        except Exception:
            providers_raw = []
    num_providers = len(providers_raw) if isinstance(providers_raw, list) else 0

    names = [n.strip() for n in drug_names.split(",") if n.strip()]
    dosages = [d.strip() for d in drug_dosages.split(",")] if drug_dosages else []
    drugs = [{"name": names[i], "dosage": dosages[i] if i < len(dosages) else ""}
             for i in range(len(names))]
    if not drugs:
        return jsonify({"error": "No drugs provided"}), 400

    # Agent-chosen plans (2026-10-02). The app sends the agent's full ordered selection; the
    # client sheet shows the first 3 Medicare Advantage picks + the first Part D pick (rightmost).
    plans_override = None
    selected = _parse_selected_plans(data)
    if selected == "invalid":
        return jsonify({"error": "selected_plans is not valid JSON"}), 400
    if selected is not None:
        _conn = get_db()
        try:
            plans_override, _county, err = resolve_selected_plans(_conn, zip_code, selected, address=client_address,
                                                                  city=client_city, state=client_state)
        finally:
            _conn.close()
        if err:
            return jsonify({"error": err}), 400

    try:
        result = compute_drug_costs(drugs, zip_code, f"01/01/{plan_year}",
                                    client_address=client_address,
                                    client_city=client_city,
                                    client_state=client_state,
                                    plans_override=plans_override)
    except Exception as e:
        return jsonify({"error": f"Cost computation failed: {str(e)}"}), 500

    plan_summaries = result.get("plan_summaries", {})
    drug_detail = result.get("drug_detail", [])

    appointed = data.get("appointed_carriers") or [
        "HealthPartners", "Blue Cross", "Medica", "Humana", "UHC", "Aetna", "UCare", "Align"]
    try:
        max_plans = int(data.get("max_plans", 3))
    except Exception:
        max_plans = 3

    agency_meta = {
        "agency_name": "Twin Cities Health", "agency_sub": "Insurance Solutions",
        "agency_phone": "763-280-8882", "agency_email": "info@tchealthsolutions.com",
    }
    config = {"appointed_carriers": appointed, "max_plans": max_plans}
    client = {"county": county, "systems": [], "num_providers": num_providers}

    try:
        if plans_override:
            from app.client_comparison import agent_selection
            selection = agent_selection(plan_summaries, [p["carrier"] for p in plans_override])
        else:
            candidates = plan_summaries_to_candidates(plan_summaries, county)
            selection = select_plans(candidates, config, client, NeutralNetworkFit())
        if not selection["selected"]:
            return jsonify({"error": "No eligible plans for this client/area"}), 404
        payload = assemble_renderer_payload(selection, plan_summaries, drug_detail,
                                            agency_meta, client, plan_year)
        pdf_bytes = render_client_comparison(payload)
    except Exception as e:
        return jsonify({"error": f"Client comparison failed: {str(e)}"}), 500

    filename = f"{client_name.replace(' ', '_')}_Plan_Comparison.pdf"
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


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

# Last updated: 2026-05-22 21:12:26

# Updated: 2026-05-22 21:22:46
# deployed 09/22/2026 23:04:40
