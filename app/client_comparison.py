"""
client_comparison.py — glue between the live engine and the client sheet.

Chain:  compute_drug_costs() output  ->  candidates (adapter, carries plan IDs)
        ->  select_plans()  ->  assemble_renderer_payload() (merges real PBP benefits)
        ->  render_client_comparison()  ->  PDF bytes

Isolation: reads only the read-only cost output + the benefits DB. Never touches
build_pdf or /process-soa. The client renderer receives no provider names or drug
tiers - by construction.
"""
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
        plan_name = key.replace(carrier, "").strip() or key

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
            "plan_premium": f"${s['premium_monthly']:.0f}",
            "part_d_premium": "Included",
            "est_annual_drug_cost": f"${s['total_drug_cost']:.0f}",
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
