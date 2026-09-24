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
]


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
    name; no official name -> fallback (the old behavior)."""
    if not official:
        return fallback
    official = official.strip()
    for prefix, repl in _NAME_PREFIXES:
        if official.startswith(prefix):
            rest = (repl + official[len(prefix):]).strip()
            return rest or official
    return official


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

        covered, not_covered = 0, []
        for d in drug_detail:
            pc = d["plans"].get(key, {})
            if pc.get("covered") or pc.get("annual_total") is not None:
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
            "rx": {"covered": covered, "total": total_drugs, "not_covered": not_covered},
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
        plans.append(plan)

    meta = dict(agency_meta)
    meta.update({"plan_year": plan_year, "num_drugs": total_drugs,
                 "num_providers": client.get("num_providers", 0)})
    return {"meta": meta, "plans": plans}
