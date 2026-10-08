"""POST /compare-plans (2026-10-08): EVERY plan for one client, priced - the Roundabout Plans page.

The agent-facing research view: every plan sold in the client's ZIP (all its counties, flagged
like the plan picker), each priced for the client's drugs, with the same numbers the reports show.
Nothing here re-implements the reports' rules - it calls the same code:
  * plan list          -> main.plan_list_for_zip           (same as /plans-for-zip)
  * pricing            -> main.compute_drug_costs          (same as both PDFs)
  * cheapest pharmacy  -> internal_pdf.pharmacy_summary     (same as the internal report)
  * benefits + stars   -> client_benefits                   (same as the client sheet)
  * doctors            -> providers_2027.check              (same as the internal report)
  * a drug's status    -> drug_status() below mirrors the internal report's cell rules exactly:
      not identified > drug list not out > injectable (verify) > not covered > covered (tier)
The client's details are used for this response only; nothing is stored or logged.
"""
import re
import time
from datetime import date

MAX_DRUGS = 25
MAX_PROVIDERS = 10
_ZIP = re.compile(r"(\d{5})(-\d{4})?")


class BadRequest(ValueError):
    """A plain-English problem with the request (HTTP 400)."""


def parse_drugs(data):
    """[{"name", "dosage"}] from `drugs` (a list of objects or strings) or the reports' flat
    `drug_names` / `drug_dosages` comma strings. Blank names are dropped."""
    drugs = []
    raw = data.get("drugs")
    if isinstance(raw, list):
        for d in raw:
            if isinstance(d, dict):
                name, dose = str(d.get("name") or "").strip(), str(d.get("dosage") or "").strip()
            else:
                name, dose = str(d or "").strip(), ""
            if name:
                drugs.append({"name": name, "dosage": dose})
    elif raw not in (None, ""):
        raise BadRequest("drugs must be a list, e.g. [{\"name\": \"Eliquis\", \"dosage\": \"5 mg\"}]")
    else:
        names = [n.strip() for n in str(data.get("drug_names") or "").split(",")]
        doses = [d.strip() for d in str(data.get("drug_dosages") or "").split(",")]
        drugs = [{"name": n, "dosage": doses[i] if i < len(doses) else ""} for i, n in enumerate(names) if n]
    if len(drugs) > MAX_DRUGS:
        raise BadRequest(f"Up to {MAX_DRUGS} medications can be compared at once.")
    return drugs


def parse_providers(data):
    raw = data.get("providers") or []
    if not isinstance(raw, list):
        raise BadRequest("providers must be a list")
    return [p for p in raw if isinstance(p, dict)][:MAX_PROVIDERS]


def cost_start(plan_year, today=None):
    """A future plan year (AEP) is priced January-December, like both reports; the current year from
    this month to December (mid-year enrollment)."""
    today = today or date.today()
    if plan_year and int(plan_year) > today.year:
        return f"01/01/{int(plan_year)}"
    return today.strftime("%m/%d/%Y")


def drug_status(drug, cell):
    """The one-word status the report would print in this plan's cell for this drug."""
    if drug.get("error"):
        return "not_identified"
    if cell.get("drug_list_missing"):
        return "list_not_out"
    if cell.get("injectable") or drug.get("is_injectable"):
        return "verify_injectable"
    if not cell.get("covered", False):
        return "not_covered"          # compare() turns this into "not_on_list" while the list isn't CMS's
    return "covered"


_NO_FLAGS = {"prior_auth": False, "step_therapy": False, "quantity_limit": False}
_UNKNOWN_FLAGS = {"prior_auth": None, "step_therapy": None, "quantity_limit": None}


def _restrictions(conn, formulary_id, ndc, have_columns=True):
    """Prior auth / step therapy / quantity limit for the exact package the plan was priced on.
    None = unknown (a database without these columns, e.g. the 2026 rollback copy, or no package):
    never reported as "no restrictions"."""
    if not have_columns or not (formulary_id and ndc):
        return dict(_UNKNOWN_FLAGS)
    row = conn.execute("SELECT prior_auth, step_therapy, quantity_limit FROM formulary "
                       "WHERE formulary_id = ? AND ndc = ? LIMIT 1", (formulary_id, ndc)).fetchone()
    if not row:
        return dict(_UNKNOWN_FLAGS)
    yes = lambda v: str(v or "").strip().upper() in ("Y", "1", "TRUE", "YES")
    return {"prior_auth": yes(row[0]), "step_therapy": yes(row[1]), "quantity_limit": yes(row[2])}


def _money(v):
    return None if v is None else round(float(v), 2)


def compare(data):
    """The /compare-plans response for a request body (dict). Raises BadRequest for a 400;
    returns (payload, http_status)."""
    from app import main as M
    from app import data_meta
    from app import availability as AV
    from app.internal_pdf import pharmacy_summary, _written_name

    started = time.perf_counter()
    m = _ZIP.fullmatch(str(data.get("zip_code") or "").strip())
    if not m:
        raise BadRequest("A 5-digit ZIP code is required to look up plans.")
    zip_code = m.group(1)
    address = (data.get("client_address") or "").strip() or None
    city = (data.get("client_city") or "").strip() or None
    state = (data.get("client_state") or "").strip() or "MN"
    drugs = parse_drugs(data)
    providers = parse_providers(data)
    plan_year = data.get("plan_year") or data_meta.data_year(M.DB_PATH)
    try:
        plan_year = int(plan_year)
    except (TypeError, ValueError):
        raise BadRequest("plan_year must be a year, e.g. 2027")

    conn = M.get_db()
    try:
        choice, listed = M.plan_list_for_zip(conn, zip_code, address, city, state)
        if not choice["county"]:
            return {"error": f"No county found for ZIP {zip_code}"}, 404
        keys = [{"contract_id": p["contract_id"], "plan_id": p["plan_id"]} for p in listed]
        resolved, _county, err = M.resolve_selected_plans(conn, zip_code, keys, max_ma=10_000, max_pd=10_000,
                                                          address=address, city=city, state=state)
        if err:                                      # can't happen: the list IS the eligible set
            raise RuntimeError(f"plan list and eligibility disagree: {err}")
        label_of = {(p["contract_id"], str(p["plan_id"]).zfill(3)): p["carrier"] for p in resolved}
        type_of = {(p["contract_id"], str(p["plan_id"]).zfill(3)): p.get("type") or p.get("plan_type")
                   for p in resolved}
        have_flags = {"prior_auth", "step_therapy", "quantity_limit"} <= set(M._table_cols(conn, "formulary"))
        # Official-data gates (2026-10-08): nothing estimated or carried over is shown as fact.
        prices_ok, networks_ok, stars_ok = AV.prices_official(M.DB_PATH), AV.networks_official(M.DB_PATH), AV.stars_official()
        formulary_of = {(c, str(p).zfill(3)): f for c, p, f in
                        conn.execute("SELECT contract_id, plan_id, formulary_id FROM plans")}

        result = {"plan_summaries": {}, "drug_detail": [], "warnings": [], "months_remaining": []}
        if drugs:
            result = M.compute_drug_costs(drugs, zip_code, cost_start(plan_year), address, city, state,
                                          plans_override=resolved)
        months = result.get("months_remaining") or []
        detail = result.get("drug_detail") or []

        doctors = []
        if providers:
            from app import providers_2027
            rows = [(label_of[k], k[0], k[1], "PD" if p["plan_type"] == "PD" else type_of.get(k))
                    for p in listed for k in [(p["contract_id"], p["plan_id"])]]
            try:
                doctors = providers_2027.check(providers, rows)
            except Exception as exc:             # never fail the comparison over the doctor check
                print(f"compare-plans: provider check failed: {type(exc).__name__}")
                doctors = []

        from app.client_benefits import lookup_benefits, format_benefits
        out_plans = []
        for p in listed:
            k = (p["contract_id"], p["plan_id"])
            label = label_of[k]
            summ = result["plan_summaries"].get(label, {})
            priced = bool(drugs) and p["drug_list_loaded"] and summ.get("total_drug_cost") is not None
            list_ok = AV.list_official(conn, formulary_of.get(k))
            costs_ok = priced and prices_ok
            try:
                benefits = format_benefits(lookup_benefits(*k)) if p["plan_type"] != "PD" else {}
            except Exception:
                benefits = {}
            pharm = pharmacy_summary(label, detail, months) if (costs_ok and networks_ok) else None
            cells, counts = [], {"covered": 0, "not_covered": 0, "list_not_out": 0, "not_identified": 0,
                                 "verify_injectable": 0, "price_unknown": 0}
            ded_met = cap_reached = None
            for d in detail:
                cell = d.get("plans", {}).get(label, {}) or {}
                status = drug_status(d, cell)
                if status == "not_covered" and not list_ok:      # our reading of a carrier PDF, not CMS's list
                    status = "not_on_list"
                counts[status] = counts.get(status, 0) + 1
                if cell.get("price_unknown") and prices_ok:
                    counts["price_unknown"] += 1
                ded_met = ded_met or cell.get("ded_met")
                cap_reached = cap_reached or cell.get("cap_reached")
                cells.append({
                    "written": d.get("original_name") or d.get("drug_name") or "",
                    "drug": d.get("drug_name") or "", "dosage": d.get("dosage") or "",
                    "status": status, "tier": cell.get("tier") if status == "covered" else None,
                    "as_generic": bool(cell.get("as_generic")),
                    **(_restrictions(conn, formulary_of.get(k), cell.get("ndc"), have_flags) if status == "covered"
                       else dict(_NO_FLAGS)),
                    "price_unknown": bool(cell.get("price_unknown")) and prices_ok,
                    "insulin_cap": bool(cell.get("insulin_cap")),
                    # The plan's OFFICIAL 2027 copay / coinsurance for this drug's tier (CMS plan benefit files).
                    "cost_share": AV.cost_share(conn, k[0], k[1], cell.get("tier"), insulin=bool(cell.get("insulin_cap")))
                                  if status == "covered" else None,
                    # Dollar amounts only from official prices; no price on file -> blank, never $0.
                    "est_year": _money(cell.get("annual_total"))
                                if costs_ok and status == "covered" and not cell.get("price_unknown") else None,
                    "monthly": [_money(x["cost"]) for x in cell.get("monthly_costs") or []]
                               if costs_ok and status == "covered" and not cell.get("price_unknown") else [],
                })
            out_plans.append({
                **{f: p[f] for f in ("contract_id", "plan_id", "plan_number", "carrier", "plan_name",
                                     "display_name", "official_name", "premium_monthly", "drug_deductible",
                                     "counties", "in_county", "drug_list_loaded", "drug_list_note")},
                "plan_type": "PD" if p["plan_type"] == "PD" else (type_of.get(k) or "MA"),
                "priced": priced,                         # the drugs were checked against this plan's list
                "prices_available": bool(costs_ok),       # official prices -> dollar estimates below are real
                "drug_list_official": list_ok,            # CMS's own 2027 list (else our reading of the carrier PDF)
                "est_drug_cost_year": _money(summ.get("total_drug_cost")) if costs_ok else None,
                "est_total_year": _money(summ.get("total_drug_plus_premium")) if costs_ok else None,
                "premium_year": _money(summ.get("premium_remaining_year")) if priced
                                else round(p["premium_monthly"] * 12, 2),
                "deductible_met": ded_met if costs_ok else None,
                "cap_reached": cap_reached if costs_ok else None,
                "coverage": dict({"not_on_list": 0, **counts}, total=len(detail)),
                "drugs": cells,
                "pharmacy": None if not pharm else {
                    "name": pharm["name"], "distance": pharm["distance"], "est_year": _money(pharm["annual"]),
                    "typical_month": _money(pharm["steady"]),
                    "mail_order_year": _money(pharm["mail_annual"]),
                    "mail_order_saves": _money(pharm["mail_saves"]) if pharm["mail_annual"] is not None else None},
                "star_rating": benefits.get("star_rating") if stars_ok else None,
                "benefits": {k2: v for k2, v in benefits.items() if k2 != "star_rating"} or None,
                "doctors": [{"name": _written_name(doc), "system": doc.get("system") or "",
                             **(doc.get("plans", {}).get(label) or {"status": "not_applicable", "detail": "",
                                                                     "accepting": ""})}
                            for doc in doctors],
            })
    finally:
        conn.close()

    return {
        "zip_code": zip_code,
        "county": choice["county"], "county_exact": bool(choice["exact"]),
        "county_method": choice["method"], "counties": choice["counties"], "county_note": choice["note"],
        "plan_year": plan_year,
        "data_vintage": M.data_vintage(),
        "prices_are_estimates": bool(data_meta.prices_estimated(M.DB_PATH)),
        # What is official right now (2026-10-08). False = that data is held back, with the note to show.
        "availability": {
            "prices": prices_ok, "prices_note": "" if prices_ok else AV.PRICES_NOTE,
            "networks": networks_ok, "networks_note": "" if networks_ok else AV.NETWORK_NOTE,
            "stars": stars_ok, "stars_note": "" if stars_ok else AV.STARS_NOTE,
            "not_on_list_label": AV.NOT_ON_LIST, "not_on_list_detail": AV.NOT_ON_LIST_DETAIL,
        },
        "months": months,
        "drugs": [{"written": d.get("original_name") or "", "read_as": d.get("drug_name") or "",
                   "dosage": d.get("dosage") or "", "identified": not d.get("error"),
                   "injectable": bool(d.get("is_injectable")), "flag": d.get("flag") or ""} for d in detail],
        "warnings": result.get("warnings") or [],
        "plans": out_plans,
        "plan_count": len(out_plans),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }, 200
