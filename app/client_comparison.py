"""
client_comparison.py — glue between the live engine and the client sheet.

Chain:  compute_drug_costs() output  ->  candidates (adapter, carries plan IDs)
        ->  select_plans()  ->  assemble_renderer_payload() (merges real PBP benefits)
        ->  render_client_comparison()  ->  PDF bytes

Isolation: reads only the read-only cost output + the benefits DB. Never touches
build_pdf or /process-soa. The client renderer receives no provider names or drug
tiers - by construction.
"""
import os
import re
import sqlite3

try:
    from app.client_benefits import lookup_benefits, format_benefits
except ImportError:                      # allows flat-dir imports for local testing
    from client_benefits import lookup_benefits, format_benefits


_CARRIER_FAMILIES = [
    ("HealthPartners", ("healthpartners", "health partners")),
    ("Blue Cross",     ("blue cross", "freedom blue", "medicareblue")),
    ("Medica",         ("medica",)),
    ("Humana",         ("humana",)),
    ("UHC",            ("uhc", "aarp", "united")),
    ("Aetna",          ("aetna", "allina")),
    ("UCare",          ("ucare",)),
    ("Align",          ("align",)),
]


def carrier_family(plan_key):
    k = (plan_key or "").lower()
    for family, needles in _CARRIER_FAMILIES:
        if any(n in k for n in needles):
            return family
    return plan_key


# ---- Client-facing plan names (display only; the internal drug-run is untouched) ----
# The engine's short labels are built for the internal report (e.g. every UHC plan is
# just "UHC"). The client sheet shows CMS's official plan name instead, minus the
# carrier prefix (the carrier already has its own line), keeping the plan type:
#   "AARP Medicare Advantage from UHC MN-0001 (PPO)" -> "UHC MN-0001 (PPO)"
_MAIN_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn.db")

# Ordered: most specific prefix first. (prefix, replacement)
_NAME_PREFIXES = [
    ("AARP Medicare Advantage from ", ""),          # -> "UHC MN-0001 (PPO)"
    ("AARP Medicare Advantage ", ""),               # -> "Patriot No Rx FG-MA01 (PPO)"
    ("Allina Health Aetna Medicare ", ""),          # -> "Signature (PPO)"
    ("Blue Cross Medicare Advantage ", ""),         # -> "Choice (PPO)"
    ("HealthPartners ", ""),                        # -> "Journey Pace (PPO)"
    ("HumanaChoice ", "Choice "),                   # -> "Choice H5216-275 (PPO)"
    ("Humana ", ""),                                # -> "Gold Choice H8145-006 (PFFS)"
    ("Medica ", ""),                                # -> "Advantage Solution H8889-005 (PPO)"
    ("Gundersen MN Quartz Med Advantage ", ""),     # -> "Elite D (w/Rx) (HMO)"
    ("Align ", ""),                                 # -> "ChoiceElite (PPO)"
    # standalone Part D (PDP) names (2026-10-02)
    ("Wellcare ", ""),                              # -> "Classic (PDP)"
    ("HealthSpring ", ""),                          # -> "Assurance Rx (PDP)"
    ("MedicareBlue Rx ", ""),                       # -> "Standard (PDP)"
]

# Client-facing carrier by CMS contract number (2026-10-02). Contract-based on purpose: matching
# words in an official plan name is unreliable ("Allina Health Aetna MEDICAre" contains "medica").
CONTRACT_CARRIER = {
    "H4882": "HealthPartners", "H6309": "HealthPartners",
    "H5959": "Blue Cross",
    "H6154": "Medica", "H8889": "Medica", "H2450": "Medica",
    "H5216": "Humana", "H8145": "Humana", "S5884": "Humana",
    "H3219": "Aetna", "S5601": "Aetna",
    "H2001": "UnitedHealthcare", "S5921": "UnitedHealthcare",
    "H3186": "Align",
    "H9834": "Quartz",
    "S4802": "Wellcare",
    "S5617": "HealthSpring",
    "S5743": "MedicareBlue Rx",
}


def carrier_display(contract_id, org_name=None):
    """Carrier name a client/agent recognizes, by contract number; falls back to CMS org name."""
    cid = (contract_id or "").strip().upper()
    if cid in CONTRACT_CARRIER:
        return CONTRACT_CARRIER[cid]
    return (org_name or cid or "").strip()


def official_plan_name(contract_id, plan_id, db_path=None):
    """CMS's official plan name from medicare_mn.db service_area, or None.
    Never raises: any problem -> None, and the caller falls back to the old label."""
    db_path = db_path or _MAIN_DB
    if not contract_id or plan_id is None or not os.path.exists(db_path):
        return None
    pid = str(plan_id).strip()
    variants = sorted({pid, pid.zfill(3), pid.lstrip("0") or "0"})   # stored padded or not
    marks = ",".join("?" * len(variants))
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT plan_name FROM service_area WHERE contract_id=? AND plan_id IN (" + marks + ") "
                "AND plan_name IS NOT NULL LIMIT 1",
                (str(contract_id).strip(), *variants)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def display_plan_name(official, fallback):
    """Official name minus the carrier prefix; unknown carriers keep the full official
    name; no official name -> fallback (the old behavior). A contract-plan number inside
    the name ("Advantage Solution H6154-001 (HMO-POS)") is dropped: it is shown separately."""
    if not official:
        return fallback
    official = re.sub(r"\s*\b[HRS]\d{4}-\d{3}\b", "", official).strip()
    for prefix, repl in _NAME_PREFIXES:
        if official.startswith(prefix):
            rest = (repl + official[len(prefix):]).strip()
            return (rest or official).replace(" from UHC (", " (")
    return official.replace(" from UHC (", " (")   # "AARP Medicare Rx Saver from UHC (PDP)" -> "... Saver (PDP)"


def plan_summaries_to_candidates(plan_summaries, county):
    candidates = []
    for key, s in plan_summaries.items():
        if (s.get("plan_type") or "").upper() == "PD":        # drop standalone drug plans
            continue
        candidates.append({
            "carrier": carrier_family(key),
            "plan_label": key,
            "contract_id": s.get("contract_id"),              # from patched plan_summaries
            "plan_id": s.get("plan_id"),
            "plan_name": s["plan_name"],
            "plan_type": s.get("plan_type"),
            "counties": [county],
            "monthly_premium": s.get("premium_monthly", 0),
            "part_d_premium_separate": 0,
            "est_annual_drug_cost": s.get("total_drug_cost", 0),   # full-year via Jan-1 window
            "part_b_giveback_monthly": 0,
            "health_systems": [],
        })
    return candidates


CLIENT_SHEET_MAX_MA = 3   # client sheet = first 3 Medicare Advantage picks ...
CLIENT_SHEET_MAX_PD = 1   # ... + the first Part D pick, as the rightmost column


def agent_selection(plan_summaries, labels_in_pick_order):
    """Agent-chosen plans -> the same selection shape select_plans() returns, so the rest of the
    pipeline is shared. No ranking: the first 3 MA/Cost picks in pick order, then the first PD
    pick as the rightmost column."""
    ma, pd = [], []
    for label in labels_in_pick_order:
        s = plan_summaries.get(label)
        if not s:
            continue
        ptype = (s.get("plan_type") or "").upper()
        cand = {
            "carrier": carrier_display(s.get("contract_id")),
            "plan_label": label,
            "contract_id": s.get("contract_id"),
            "plan_id": s.get("plan_id"),
            "plan_name": s.get("plan_name"),
            "plan_type": s.get("plan_type"),
            "drug_plan_only": ptype == "PD",
        }
        if ptype == "PD":
            if len(pd) < CLIENT_SHEET_MAX_PD:
                pd.append((cand, None, None))
        elif len(ma) < CLIENT_SHEET_MAX_MA:
            ma.append((cand, None, None))
    return {"selected": ma + pd}


NOT_INCLUDED = "Not included"


def assemble_renderer_payload(selection, plan_summaries, drug_detail, agency_meta, client, plan_year):
    """Selected plans + engine data + real PBP benefits -> the renderer's input."""
    total_drugs = len(drug_detail)
    plans = []
    for cand, verdict, cost in selection["selected"]:
        key = cand["plan_label"]
        s = plan_summaries[key]
        carrier = cand["carrier"]
        plan_name = display_plan_name(
            official_plan_name(cand.get("contract_id"), cand.get("plan_id")),
            key.replace(carrier, "").strip() or key)

        # Three honest buckets per plan (never claim "not covered" for a drug we didn't check):
        #   covered      - checked, on this plan's formulary
        #   not_covered  - checked, explicitly NOT on this plan's formulary (named on the sheet)
        #   unverified   - couldn't identify it (engine error), injectable (agency rule skips
        #                  the coverage check), or no entry for this plan -> "confirm with agent"
        covered, not_covered, unverified = 0, [], 0
        for d in drug_detail:
            pc = d["plans"].get(key, {})
            if d.get("error") or pc.get("injectable") or not pc:
                unverified += 1
            elif pc.get("covered") or pc.get("annual_total") is not None:
                covered += 1
            else:
                not_covered.append(d.get("original_name") or d.get("drug_name"))

        plan = {
            "carrier": carrier,
            "plan_name": plan_name,
            # ---- from the drug engine ----
            "plan_premium": f"${s['premium_monthly']:,.0f}",
            "part_d_premium": "Included",
            "est_annual_drug_cost": f"${s['total_drug_cost']:,.0f}",
            "rx": {"covered": covered, "total": total_drugs, "not_covered": not_covered,
                   "unverified": unverified},
            # ---- interim states ----
            "star_rating": None,                 # gated: 2027 CMS star ratings
            "providers": {"status": "verify"},   # no provider chart yet
            # ---- benefit rows: safe stubs, overlaid by real PBP data below ----
            "deductible": "\u2014", "part_b_giveback": None,
            "oop_max": "\u2014", "pcp_visit": "\u2014", "specialist_visit": "\u2014",
            "hospital_per_day": {"amount": "\u2014", "note": None}, "emergency_room": "\u2014",
            "dental": "\u2014", "vision": "\u2014", "hearing": "\u2014",
            "otc": "\u2014", "fitness": "\u2014",
        }
        # overlay real CMS benefits (keyed by plan ID); no-op if the plan has no row
        plan.update(format_benefits(lookup_benefits(cand.get("contract_id"), cand.get("plan_id"))))
        if cand.get("drug_plan_only"):
            # Standalone Part D: drug coverage only. Medical + extra-benefit rows say so plainly.
            plan["drug_plan_only"] = True
            for field in ("deductible", "oop_max", "pcp_visit", "specialist_visit",
                          "emergency_room", "dental", "vision", "hearing", "otc", "fitness"):
                plan[field] = NOT_INCLUDED
            plan["hospital_per_day"] = {"amount": NOT_INCLUDED, "note": None}
            plan["part_b_giveback"] = None
        plans.append(plan)

    meta = dict(agency_meta)
    meta.update({"plan_year": plan_year, "num_drugs": total_drugs,
                 "num_providers": client.get("num_providers", 0)})
    return {"meta": meta, "plans": plans}
